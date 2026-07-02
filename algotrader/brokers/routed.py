from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from algotrader.brokers.base import BaseBroker
from algotrader.models import OptionChainEntry


class RoutedBroker(BaseBroker):
    """Routes trading/contract discovery to Zerodha and optional market data to FYERS."""

    def __init__(self, primary: BaseBroker, market_data: Any | None = None) -> None:
        self.primary = primary
        self.market_data = market_data

    def get_ltp(self, exchange: str, symbol: str) -> float:
        try:
            return self.primary.get_ltp(exchange, symbol)
        except Exception as primary_exc:
            if self.market_data is None:
                raise
            try:
                return float(self.market_data.get_ltp(exchange, symbol))
            except Exception as fallback_exc:
                raise RuntimeError(
                    f"Quote unavailable for {exchange}:{symbol}. "
                    f"Primary failed: {primary_exc}; FYERS fallback failed: {fallback_exc}"
                ) from fallback_exc

    def supports_historical_data(self) -> bool:
        try:
            if self.primary.supports_historical_data():
                return True
        except Exception:
            pass
        if self.market_data is None:
            return False
        try:
            return bool(self.market_data.supports_historical_data())
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
        try:
            return self.primary.get_historical_data(exchange, symbol, interval, from_dt, to_dt)
        except Exception as primary_exc:
            if self.market_data is None:
                raise
            try:
                return self.market_data.get_historical_data(exchange, symbol, interval, from_dt, to_dt)
            except Exception as fallback_exc:
                raise RuntimeError(
                    f"Historical data unavailable for {exchange}:{symbol}. "
                    f"Primary failed: {primary_exc}; FYERS fallback failed: {fallback_exc}"
                ) from fallback_exc

    def get_public_historical_data(self, symbol: str, interval: str, period: str = "") -> pd.DataFrame:
        public_fetcher = getattr(self.primary, "get_public_historical_data", None)
        if public_fetcher is None:
            return pd.DataFrame()
        return public_fetcher(symbol, interval, period)

    def place_market_order(
        self,
        exchange: str,
        tradingsymbol: str,
        transaction_type: str,
        quantity: int,
    ) -> str:
        return self.primary.place_market_order(exchange, tradingsymbol, transaction_type, quantity)

    def get_option_chain(
        self,
        underlying_symbol: str,
        contract_exchange: str,
        spot_price: float,
    ) -> list[OptionChainEntry]:
        rows = self.primary.get_option_chain(underlying_symbol, contract_exchange, spot_price)
        if self.market_data is None or not rows:
            return rows
        quote_getter = getattr(self.market_data, "get_quotes", None)
        if quote_getter is None:
            return rows
        try:
            quotes = quote_getter([(row.exchange, row.tradingsymbol) for row in rows])
        except Exception:
            return rows
        for row in rows:
            quote = quotes.get(f"{row.exchange.upper()}:{row.tradingsymbol.upper()}")
            if not quote:
                continue
            row.ltp = self._prefer_float(quote.get("last_price"), row.ltp)
            row.bid = self._prefer_float(quote.get("bid"), row.bid)
            row.ask = self._prefer_float(quote.get("ask"), row.ask)
            row.volume = self._prefer_int(quote.get("volume"), row.volume)
            row.oi = self._prefer_int(quote.get("oi"), row.oi)
            row.spread_pct = self._prefer_float(quote.get("spread_pct"), row.spread_pct)
        return rows

    def ensure_authenticated(self) -> None:
        ensure = getattr(self.primary, "ensure_authenticated", None)
        if ensure is None:
            return
        ensure()

    def find_option_contract_details(self, *args, **kwargs) -> dict:
        return getattr(self.primary, "find_option_contract_details")(*args, **kwargs)

    def find_future_contract_details(self, *args, **kwargs) -> dict:
        return getattr(self.primary, "find_future_contract_details")(*args, **kwargs)

    def _prefer_float(self, new_value: object, old_value: float | None) -> float | None:
        if new_value is None:
            return old_value
        try:
            return float(new_value)
        except (TypeError, ValueError):
            return old_value

    def _prefer_int(self, new_value: object, old_value: int | None) -> int | None:
        if new_value is None:
            return old_value
        try:
            return int(new_value)
        except (TypeError, ValueError):
            return old_value
