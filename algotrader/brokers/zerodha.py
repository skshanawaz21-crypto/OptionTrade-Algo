from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from kiteconnect import KiteConnect
from kiteconnect.exceptions import InputException
from kiteconnect.exceptions import PermissionException
from kiteconnect.exceptions import TokenException
import yfinance as yf

from algotrader.brokers.base import BaseBroker
from algotrader.config import AppSettings
from algotrader.models import OptionChainEntry


class ZerodhaBroker(BaseBroker):
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.kite = KiteConnect(api_key=settings.zerodha_api_key)
        self._instrument_cache: dict[tuple[str, str], dict] = {}
        self._all_instruments: list[dict] | None = None
        self._set_access_token()

    def _set_access_token(self) -> None:
        access_token = self.settings.zerodha_access_token.strip()
        if not access_token:
            token_path = Path(self.settings.zerodha_token_file)
            if token_path.exists():
                access_token = token_path.read_text(encoding="utf-8").strip()
        if access_token:
            self.kite.set_access_token(access_token)

    def ensure_authenticated(self) -> None:
        profile = self.kite.profile()
        if not profile:
            raise RuntimeError("Zerodha authentication failed.")

    def _load_instruments(self) -> list[dict]:
        if self._all_instruments is None:
            self._all_instruments = self.kite.instruments()
        return self._all_instruments

    def lookup_instrument(self, exchange: str, symbol: str) -> dict:
        exchange, symbol = self._normalize_market_instrument(exchange, symbol)
        key = (exchange, symbol)
        if key in self._instrument_cache:
            return self._instrument_cache[key]

        matches = [
            item
            for item in self._load_instruments()
            if item["exchange"] == exchange and item["tradingsymbol"] == symbol
        ]
        if not matches:
            raise KeyError(f"Instrument not found for {exchange}:{symbol}")
        self._instrument_cache[key] = matches[0]
        return matches[0]

    def get_ltp(self, exchange: str, symbol: str) -> float:
        exchange, symbol = self._normalize_market_instrument(exchange, symbol)
        try:
            quote = self.kite.ltp(f"{exchange}:{symbol}")
            return float(quote[f"{exchange}:{symbol}"]["last_price"])
        except (PermissionException, TokenException, InputException) as exc:
            if self._looks_like_option_symbol(symbol):
                raise RuntimeError(
                    f"Option quote unavailable for {exchange}:{symbol}. "
                    "Refresh Zerodha access token for accurate option LTP."
                ) from exc
            return self._get_public_ltp(symbol)

    def supports_historical_data(self) -> bool:
        try:
            self.kite.profile()
            instrument = self.lookup_instrument(self.settings.default_exchange, "SBIN")
            rows = self.kite.historical_data(
                instrument["instrument_token"],
                datetime.now() - pd.Timedelta(days=5),
                datetime.now(),
                "5minute",
            )
            return bool(rows)
        except PermissionException:
            return False
        except Exception:
            return False

    def get_historical_data(
        self,
        exchange: str,
        symbol: str,
        interval: str,
        from_dt: datetime,
        to_dt: datetime,
    ) -> pd.DataFrame:
        exchange, symbol = self._normalize_market_instrument(exchange, symbol)
        instrument = self.lookup_instrument(exchange, symbol)
        rows = self.kite.historical_data(
            instrument["instrument_token"],
            from_dt,
            to_dt,
            interval,
        )
        output = pd.DataFrame(rows)
        if not output.empty and "date" in output.columns:
            output["date"] = (
                pd.to_datetime(output["date"], errors="coerce", utc=True)
                .dt.tz_convert("Asia/Kolkata")
                .dt.tz_localize(None)
            )
        return output

    def get_public_historical_data(
        self,
        symbol: str,
        interval: str,
        period: str = "",
    ) -> pd.DataFrame:
        market_symbol = self._to_public_market_symbol(symbol)
        yf_interval = self._to_yfinance_interval(interval)
        yf_period = period or self._default_public_period(yf_interval)
        history = yf.Ticker(market_symbol).history(
            period=yf_period,
            interval=yf_interval,
            auto_adjust=False,
        )
        if history.empty:
            return pd.DataFrame()
        history = history.reset_index()
        date_column = "Datetime" if "Datetime" in history.columns else "Date"
        output = history.rename(
            columns={
                date_column: "date",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            }
        )
        required = ["date", "open", "high", "low", "close", "volume"]
        for column in required:
            if column not in output.columns:
                return pd.DataFrame()
        output["date"] = (
            pd.to_datetime(output["date"], errors="coerce", utc=True)
            .dt.tz_convert("Asia/Kolkata")
            .dt.tz_localize(None)
        )
        return output[required].dropna().reset_index(drop=True)

    def place_market_order(
        self,
        exchange: str,
        tradingsymbol: str,
        transaction_type: str,
        quantity: int,
    ) -> str:
        return str(
            self.kite.place_order(
                tradingsymbol=tradingsymbol,
                exchange=exchange,
                transaction_type=transaction_type,
                quantity=quantity,
                order_type=self.settings.default_order_type,
                product=self.settings.default_product,
                variety=self.settings.default_variety,
            )
        )

    def get_option_chain(
        self,
        underlying_symbol: str,
        contract_exchange: str,
        spot_price: float,
    ) -> list[OptionChainEntry]:
        underlying_name = self._normalize_option_underlying_name(underlying_symbol)
        target_segment = self._segment_for_contract_exchange(contract_exchange)
        chain_candidates = [
            item
            for item in self._load_instruments()
            if item["segment"] == target_segment
            and item["exchange"] == contract_exchange
            and str(item.get("name", "")).upper() == underlying_name
        ]
        if not chain_candidates:
            return []

        quote_map = self._fetch_option_quotes(chain_candidates)
        rows: list[OptionChainEntry] = []
        for item in chain_candidates:
            expiry_date = self._as_date(item.get("expiry"))
            if expiry_date is None:
                continue
            dte_days = (expiry_date - date.today()).days
            if dte_days < 0:
                continue

            instrument_key = f"{item['exchange']}:{item['tradingsymbol']}"
            quote = quote_map.get(instrument_key, {})
            ltp = self._to_float(quote.get("last_price"))
            depth = quote.get("depth", {}) if isinstance(quote, dict) else {}
            bid = self._depth_price(depth, "buy")
            ask = self._depth_price(depth, "sell")
            spread_pct = self._spread_pct(bid, ask)
            rows.append(
                OptionChainEntry(
                    tradingsymbol=str(item["tradingsymbol"]),
                    exchange=str(item["exchange"]),
                    underlying_symbol=underlying_symbol,
                    option_side="CE" if "CE" in str(item["tradingsymbol"]) else "PE",
                    expiry=str(expiry_date),
                    strike=float(item["strike"]),
                    dte_days=dte_days,
                    lot_size=int(item.get("lot_size") or 1),
                    ltp=ltp,
                    bid=bid,
                    ask=ask,
                    oi=self._to_int(quote.get("oi") if isinstance(quote, dict) else None),
                    volume=self._to_int(
                        quote.get("volume") if isinstance(quote, dict) else None
                    ),
                    spread_pct=spread_pct,
                )
            )
        return rows

    def find_option_contract_details(
        self,
        underlying_symbol: str,
        spot_price: float,
        option_side: str,
        contract_exchange: str = "NFO",
        expiry_hint: str = "",
    ) -> dict:
        target_side = option_side.upper()
        if target_side == "AUTO":
            target_side = "CE"
        underlying_name = self._normalize_option_underlying_name(underlying_symbol)

        candidates = []
        target_exchange = contract_exchange.strip().upper()
        target_segment = self._segment_for_contract_exchange(target_exchange)
        for item in self._load_instruments():
            if item["segment"] != target_segment:
                continue
            if str(item.get("exchange", "")).upper() != target_exchange:
                continue
            if str(item.get("name", "")).upper() != underlying_name:
                continue
            if target_side not in str(item["tradingsymbol"]).upper():
                continue
            if expiry_hint and expiry_hint not in str(item["expiry"]):
                continue
            candidates.append(item)

        if not candidates:
            raise KeyError(f"No option contracts found for {underlying_symbol}")

        def sort_key(item: dict) -> tuple[float, str]:
            return (
                abs(float(item["strike"]) - spot_price),
                str(item["expiry"]),
            )

        best = sorted(candidates, key=sort_key)[0]
        return {
            "tradingsymbol": str(best["tradingsymbol"]),
            "exchange": str(best["exchange"]),
            "strike": float(best["strike"]),
            "expiry": str(best["expiry"]),
            "lot_size": int(best.get("lot_size") or 1),
            "option_side": target_side,
            "bid": None,
            "ask": None,
            "spread_pct": None,
        }

    def find_option_contract(
        self,
        underlying_symbol: str,
        spot_price: float,
        option_side: str,
        contract_exchange: str = "NFO",
        expiry_hint: str = "",
    ) -> str:
        return self.find_option_contract_details(
            underlying_symbol=underlying_symbol,
            spot_price=spot_price,
            option_side=option_side,
            contract_exchange=contract_exchange,
            expiry_hint=expiry_hint,
        )["tradingsymbol"]

    def find_future_contract_details(
        self,
        underlying_symbol: str,
        contract_exchange: str = "NFO",
        min_dte: int = 0,
        max_dte: int = 45,
        expiry_type: str = "any",
    ) -> dict:
        underlying_name = self._normalize_option_underlying_name(underlying_symbol)
        target_exchange = contract_exchange.strip().upper()
        target_segment = self._segment_for_future_exchange(target_exchange)
        candidates: list[dict] = []
        for item in self._load_instruments():
            if str(item.get("segment", "")).upper() != target_segment:
                continue
            if str(item.get("exchange", "")).upper() != target_exchange:
                continue
            if str(item.get("name", "")).upper() != underlying_name:
                continue
            expiry_date = self._as_date(item.get("expiry"))
            if expiry_date is None:
                continue
            dte_days = (expiry_date - date.today()).days
            if dte_days < min_dte or dte_days > max_dte:
                continue
            row = dict(item)
            row["_expiry_date"] = expiry_date
            row["_dte_days"] = dte_days
            candidates.append(row)

        if not candidates:
            raise KeyError(f"No futures contracts found for {underlying_symbol}")

        if expiry_type.lower().strip() == "monthly":
            monthly_expiries: dict[tuple[int, int], date] = {}
            for row in candidates:
                expiry = row["_expiry_date"]
                key = (expiry.year, expiry.month)
                if key not in monthly_expiries or expiry > monthly_expiries[key]:
                    monthly_expiries[key] = expiry
            allowed = {value for value in monthly_expiries.values()}
            candidates = [row for row in candidates if row["_expiry_date"] in allowed]

        best = sorted(
            candidates,
            key=lambda row: (int(row["_dte_days"]), str(row.get("expiry", ""))),
        )[0]
        return {
            "tradingsymbol": str(best["tradingsymbol"]),
            "exchange": str(best["exchange"]),
            "expiry": str(best["expiry"]),
            "lot_size": int(best.get("lot_size") or 1),
        }

    def _get_public_ltp(self, symbol: str) -> float:
        market_symbol = self._to_public_market_symbol(symbol)
        ticker = yf.Ticker(market_symbol)
        history = ticker.history(period="5d", interval="1m", auto_adjust=False)
        if history.empty:
            history = ticker.history(period="1mo", interval="1d", auto_adjust=False)
        if history.empty:
            raise RuntimeError(f"Could not fetch public market data for {symbol}")
        return float(history["Close"].dropna().iloc[-1])

    def _to_public_market_symbol(self, symbol: str) -> str:
        aliases = {
            "NIFTY 50": "^NSEI",
            "NIFTY": "^NSEI",
            "BANKNIFTY": "^NSEBANK",
            "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
            "SENSEX": "^BSESN",
        }
        if symbol in aliases:
            return aliases[symbol]
        return f"{symbol}.NS"

    def _normalize_market_instrument(self, exchange: str, symbol: str) -> tuple[str, str]:
        normalized_exchange = exchange.strip().upper()
        normalized_symbol = symbol.strip().upper()
        if normalized_exchange == "NSE" and normalized_symbol in {"NIFTY", "NIFTY50", "NIFTY 50"}:
            return "NSE", "NIFTY 50"
        if normalized_exchange == "NSE" and normalized_symbol in {"BANKNIFTY", "NIFTYBANK", "NIFTY BANK"}:
            return "NSE", "NIFTY BANK"
        if normalized_symbol == "SENSEX" and normalized_exchange in {"NSE", "BSE"}:
            return "BSE", "SENSEX"
        return normalized_exchange, symbol.strip().upper()

    def _normalize_option_underlying_name(self, symbol: str) -> str:
        aliases = {
            "NIFTY 50": "NIFTY",
            "NIFTY": "NIFTY",
            "BANKNIFTY": "BANKNIFTY",
            "FINNIFTY": "FINNIFTY",
            "SENSEX": "SENSEX",
        }
        return aliases.get(symbol.upper(), symbol.upper())

    def _segment_for_contract_exchange(self, contract_exchange: str) -> str:
        exchange = contract_exchange.strip().upper()
        if exchange == "BFO":
            return "BFO-OPT"
        return "NFO-OPT"

    def _segment_for_future_exchange(self, contract_exchange: str) -> str:
        exchange = contract_exchange.strip().upper()
        if exchange == "BFO":
            return "BFO-FUT"
        return "NFO-FUT"

    def _looks_like_option_symbol(self, symbol: str) -> bool:
        upper = symbol.upper()
        return "CE" in upper or "PE" in upper

    def _fallback_option_ltp(self, option_symbol: str) -> float:
        upper = option_symbol.upper()
        side = "CE" if "CE" in upper else "PE"
        strike = self._extract_strike(upper)
        underlying = self._extract_underlying_from_option_symbol(upper)
        spot = self._get_public_ltp(underlying)
        intrinsic = max(spot - strike, 0.0) if side == "CE" else max(strike - spot, 0.0)
        time_value = max(spot * 0.01, 1.0)
        return intrinsic + time_value

    def _extract_underlying_from_option_symbol(self, option_symbol: str) -> str:
        chars: list[str] = []
        for ch in option_symbol:
            if ch.isdigit():
                break
            chars.append(ch)
        raw = "".join(chars).replace("NIFTYBANK", "BANKNIFTY")
        if raw in {"NIFTY", "BANKNIFTY", "FINNIFTY"}:
            return raw
        return raw or "NIFTY"

    def _extract_strike(self, option_symbol: str) -> float:
        cleaned = option_symbol.upper().replace("CE", "").replace("PE", "")
        match = re.search(r"(\d+(?:\.\d+)?)$", cleaned)
        if not match:
            return 0.0
        try:
            return float(match.group(1))
        except ValueError:
            return 0.0

    def _to_yfinance_interval(self, interval: str) -> str:
        normalized = interval.lower().strip()
        mapping = {
            "1minute": "1m",
            "2minute": "2m",
            "5minute": "5m",
            "15minute": "15m",
            "30minute": "30m",
            "60minute": "60m",
            "day": "1d",
        }
        return mapping.get(normalized, "5m")

    def _default_public_period(self, yf_interval: str) -> str:
        if yf_interval in {"1m", "2m", "5m", "15m", "30m"}:
            return "5d"
        if yf_interval == "60m":
            return "3mo"
        return "1y"

    def _fetch_option_quotes(self, instruments: list[dict]) -> dict[str, dict]:
        keys = [f"{item['exchange']}:{item['tradingsymbol']}" for item in instruments]
        if not keys:
            return {}
        quotes: dict[str, dict] = {}
        chunk_size = 250
        for start in range(0, len(keys), chunk_size):
            chunk = keys[start : start + chunk_size]
            try:
                chunk_quotes = self.kite.quote(chunk)
            except (PermissionException, TokenException, InputException):
                return {}
            except Exception:
                continue
            for key, value in chunk_quotes.items():
                quotes[key] = value
        return quotes

    def _as_date(self, value: object) -> date | None:
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value).date()
            except ValueError:
                return None
        return None

    def _to_float(self, value: object) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _to_int(self, value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _depth_price(self, depth: object, side: str) -> float | None:
        if not isinstance(depth, dict):
            return None
        side_levels = depth.get(side)
        if not isinstance(side_levels, list) or not side_levels:
            return None
        top = side_levels[0]
        if not isinstance(top, dict):
            return None
        return self._to_float(top.get("price"))

    def _spread_pct(self, bid: float | None, ask: float | None) -> float | None:
        if bid is None or ask is None:
            return None
        if bid <= 0 or ask <= 0:
            return None
        mid = (bid + ask) / 2.0
        if mid <= 0:
            return None
        return ((ask - bid) / mid) * 100.0
