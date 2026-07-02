from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from algotrader.brokers.base import BaseBroker
from algotrader.config import AppSettings
from algotrader.models import OptionChainEntry


class FyersBroker(BaseBroker):
    """FYERS market-data adapter used as an optional quote fallback."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.client_id = settings.fyers_client_id.strip()
        self.access_token = self._load_access_token()
        self.data_base_url = settings.fyers_data_base_url

    @classmethod
    def is_configured(cls, settings: AppSettings) -> bool:
        if not settings.fyers_client_id.strip():
            return False
        if settings.fyers_access_token.strip():
            return True
        token_path = Path(settings.fyers_token_file)
        return token_path.exists() and bool(token_path.read_text(encoding="utf-8").strip())

    def get_ltp(self, exchange: str, symbol: str) -> float:
        quotes = self.get_quotes([(exchange, symbol)])
        key = self._quote_key(exchange, symbol)
        quote = quotes.get(key)
        if not quote or quote.get("last_price") is None:
            raise RuntimeError(f"FYERS quote unavailable for {exchange}:{symbol}")
        return float(quote["last_price"])

    def get_quotes(self, instruments: list[tuple[str, str]]) -> dict[str, dict[str, Any]]:
        if not instruments:
            return {}
        output: dict[str, dict[str, Any]] = {}
        chunk_size = 50
        for start in range(0, len(instruments), chunk_size):
            chunk = instruments[start : start + chunk_size]
            symbol_map = {
                self._to_fyers_symbol(exchange, symbol): self._quote_key(exchange, symbol)
                for exchange, symbol in chunk
            }
            response = self._get("/quotes", {"symbols": ",".join(symbol_map)})
            if not isinstance(response, dict) or response.get("s") != "ok":
                raise RuntimeError(f"FYERS quote request failed: {response}")
            for row in response.get("d", []):
                if not isinstance(row, dict):
                    continue
                fyers_symbol = str(row.get("n") or row.get("symbol") or "")
                key = symbol_map.get(fyers_symbol)
                if not key:
                    continue
                values = row.get("v", {}) if isinstance(row.get("v"), dict) else {}
                bid = self._to_float(values.get("bid"))
                ask = self._to_float(values.get("ask"))
                output[key] = {
                    "last_price": self._to_float(values.get("lp")),
                    "bid": bid,
                    "ask": ask,
                    "volume": self._to_int(
                        values.get("volume")
                        or values.get("vol_traded_today")
                        or values.get("traded_qty")
                    ),
                    "oi": self._to_int(values.get("oi") or values.get("open_interest")),
                    "spread_pct": self._spread_pct(bid, ask),
                    "raw": row,
                }
        return output

    def supports_historical_data(self) -> bool:
        return bool(self.client_id and self.access_token)

    def get_historical_data(
        self,
        exchange: str,
        symbol: str,
        interval: str,
        from_dt: datetime,
        to_dt: datetime,
    ) -> pd.DataFrame:
        response = self._get(
            "/history",
            {
                "symbol": self._to_fyers_symbol(exchange, symbol),
                "resolution": self._to_fyers_resolution(interval),
                "date_format": "0",
                "range_from": str(int(from_dt.timestamp())),
                "range_to": str(int(to_dt.timestamp())),
                "cont_flag": "1",
            }
        )
        if not isinstance(response, dict) or response.get("s") != "ok":
            raise RuntimeError(f"FYERS historical request failed: {response}")
        rows = response.get("candles", [])
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
        df["date"] = pd.to_datetime(df["date"], unit="s", errors="coerce", utc=True).dt.tz_convert(None)
        return df.dropna().reset_index(drop=True)

    def place_market_order(
        self,
        exchange: str,
        tradingsymbol: str,
        transaction_type: str,
        quantity: int,
    ) -> str:
        raise NotImplementedError("FYERS live order placement is not enabled in OptionTrader yet.")

    def get_option_chain(
        self,
        underlying_symbol: str,
        contract_exchange: str,
        spot_price: float,
    ) -> list[OptionChainEntry]:
        return []

    def ensure_authenticated(self) -> None:
        self.get_ltp("NSE", "SBIN")

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        if not self.client_id:
            raise RuntimeError("FYERS_CLIENT_ID is not configured.")
        if not self.access_token:
            raise RuntimeError("FYERS access token is not configured.")
        response = requests.get(
            f"{self.data_base_url}{path}",
            params=params,
            headers={
                "Authorization": self._authorization_token(),
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=7,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"FYERS returned non-JSON response: {response.text[:200]}") from exc
        if response.status_code >= 400:
            raise RuntimeError(f"FYERS HTTP {response.status_code}: {payload}")
        return payload

    def _load_access_token(self) -> str:
        token = self.settings.fyers_access_token.strip()
        if token:
            return token
        token_path = Path(self.settings.fyers_token_file)
        if token_path.exists():
            return token_path.read_text(encoding="utf-8").strip()
        return ""

    def _authorization_token(self) -> str:
        if ":" in self.access_token:
            return self.access_token
        return f"{self.client_id}:{self.access_token}"

    def _to_fyers_symbol(self, exchange: str, symbol: str) -> str:
        if ":" in symbol:
            return symbol
        normalized_exchange = exchange.strip().upper()
        normalized_symbol = symbol.strip().upper()
        index_aliases = {
            "NIFTY": "NSE:NIFTY50-INDEX",
            "NIFTY 50": "NSE:NIFTY50-INDEX",
            "NIFTY50": "NSE:NIFTY50-INDEX",
            "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
            "FINNIFTY": "NSE:FINNIFTY-INDEX",
            "SENSEX": "BSE:SENSEX-INDEX",
        }
        if normalized_symbol in index_aliases:
            return index_aliases[normalized_symbol]
        if normalized_exchange in {"NFO", "NSE_FO"}:
            return f"NSE:{normalized_symbol}"
        if normalized_exchange in {"BFO", "BSE_FO"}:
            return f"BSE:{normalized_symbol}"
        if normalized_exchange == "BSE":
            return f"BSE:{normalized_symbol}-EQ"
        return f"NSE:{normalized_symbol}-EQ"

    def _quote_key(self, exchange: str, symbol: str) -> str:
        return f"{exchange.strip().upper()}:{symbol.strip().upper()}"

    def _to_fyers_resolution(self, interval: str) -> str:
        mapping = {
            "1minute": "1",
            "2minute": "2",
            "5minute": "5",
            "15minute": "15",
            "30minute": "30",
            "60minute": "60",
            "day": "D",
        }
        return mapping.get(interval.lower().strip(), "5")

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

    def _spread_pct(self, bid: float | None, ask: float | None) -> float | None:
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return None
        mid = (bid + ask) / 2.0
        if mid <= 0:
            return None
        return ((ask - bid) / mid) * 100.0
