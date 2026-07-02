from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

import pandas as pd
from algotrader.models import OptionChainEntry


class BaseBroker(ABC):
    @abstractmethod
    def get_ltp(self, exchange: str, symbol: str) -> float:
        raise NotImplementedError

    @abstractmethod
    def supports_historical_data(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def get_historical_data(
        self,
        exchange: str,
        symbol: str,
        interval: str,
        from_dt: datetime,
        to_dt: datetime,
    ) -> pd.DataFrame:
        raise NotImplementedError

    @abstractmethod
    def place_market_order(
        self,
        exchange: str,
        tradingsymbol: str,
        transaction_type: str,
        quantity: int,
    ) -> str:
        raise NotImplementedError

    @abstractmethod
    def get_option_chain(
        self,
        underlying_symbol: str,
        contract_exchange: str,
        spot_price: float,
    ) -> list[OptionChainEntry]:
        raise NotImplementedError
