import unittest

from algotrader.brokers.fyers import FyersBroker
from algotrader.brokers.routed import RoutedBroker
from algotrader.config import AppSettings
from algotrader.models import OptionChainEntry


def settings() -> AppSettings:
    return AppSettings(
        zerodha_api_key="kite-key",
        zerodha_api_secret="kite-secret",
        zerodha_access_token="",
        zerodha_token_file="access_token.txt",
        fyers_client_id="FYERSAPP-100",
        fyers_secret_key="fyers-secret",
        fyers_redirect_uri="http://localhost:8000",
        fyers_access_token="fyers-token",
        fyers_token_file="fyers_access_token.txt",
        fyers_data_base_url="https://api-t1.fyers.in/data",
        fyers_auth_base_url="https://api-t1.fyers.in/api/v3",
        market_data_provider="auto",
        default_exchange="NSE",
        default_product="MIS",
        default_variety="regular",
        default_order_type="MARKET",
        capital=500000,
        log_level="INFO",
    )


class PrimaryBrokerStub:
    def get_ltp(self, exchange: str, symbol: str) -> float:
        raise RuntimeError("primary quote blocked")

    def supports_historical_data(self) -> bool:
        return False

    def get_historical_data(self, *args, **kwargs):
        raise RuntimeError("no historical data")

    def place_market_order(self, *args, **kwargs):
        return "paper"

    def get_option_chain(self, underlying_symbol: str, contract_exchange: str, spot_price: float):
        return [
            OptionChainEntry(
                tradingsymbol="SBIN26MAY800CE",
                exchange="NFO",
                underlying_symbol=underlying_symbol,
                option_side="CE",
                expiry="2026-05-26",
                strike=800,
                dte_days=21,
                lot_size=750,
            )
        ]


class FallbackQuoteStub:
    def get_ltp(self, exchange: str, symbol: str) -> float:
        return 12.5

    def get_quotes(self, instruments):
        return {
            "NFO:SBIN26MAY800CE": {
                "last_price": 12.5,
                "bid": 12.45,
                "ask": 12.55,
                "volume": 10000,
                "oi": 500000,
                "spread_pct": 0.8,
            }
        }


class TestBrokerRouting(unittest.TestCase):
    def test_fyers_symbol_mapping(self) -> None:
        broker = FyersBroker(settings())

        self.assertEqual(broker._to_fyers_symbol("NSE", "SBIN"), "NSE:SBIN-EQ")
        self.assertEqual(broker._to_fyers_symbol("NFO", "SBIN26MAY800CE"), "NSE:SBIN26MAY800CE")
        self.assertEqual(broker._to_fyers_symbol("BFO", "SENSEX26MAY80000CE"), "BSE:SENSEX26MAY80000CE")
        self.assertEqual(broker._to_fyers_symbol("NSE", "NIFTY"), "NSE:NIFTY50-INDEX")

    def test_routed_broker_falls_back_to_fyers_ltp(self) -> None:
        broker = RoutedBroker(PrimaryBrokerStub(), FallbackQuoteStub())

        self.assertEqual(broker.get_ltp("NFO", "SBIN26MAY800CE"), 12.5)

    def test_routed_broker_enriches_option_chain_quotes(self) -> None:
        broker = RoutedBroker(PrimaryBrokerStub(), FallbackQuoteStub())

        rows = broker.get_option_chain("SBIN", "NFO", 800)

        self.assertEqual(rows[0].ltp, 12.5)
        self.assertEqual(rows[0].bid, 12.45)
        self.assertEqual(rows[0].ask, 12.55)
        self.assertEqual(rows[0].volume, 10000)
        self.assertEqual(rows[0].oi, 500000)


if __name__ == "__main__":
    unittest.main()
