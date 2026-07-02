from __future__ import annotations

from algotrader.brokers.fyers import FyersBroker
from algotrader.brokers.routed import RoutedBroker
from algotrader.brokers.zerodha import ZerodhaBroker
from algotrader.config import AppSettings


def create_broker(settings: AppSettings):
    primary = ZerodhaBroker(settings)
    provider = (settings.market_data_provider or "auto").lower().strip()
    if provider in {"auto", "fyers", "fyers_fallback"} and FyersBroker.is_configured(settings):
        return RoutedBroker(primary=primary, market_data=FyersBroker(settings))
    return primary
