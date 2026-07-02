import unittest

from algotrader.brokers.zerodha import ZerodhaBroker
from algotrader.config import AppSettings


def settings() -> AppSettings:
    return AppSettings(
        zerodha_api_key="kite-key",
        zerodha_api_secret="kite-secret",
        zerodha_access_token="",
        zerodha_token_file="access_token.txt",
        fyers_client_id="",
        fyers_secret_key="",
        fyers_redirect_uri="",
        fyers_access_token="",
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


class TestZerodhaMarketSymbols(unittest.TestCase):
    def test_index_aliases_match_kite_quote_symbols(self) -> None:
        broker = ZerodhaBroker(settings())

        self.assertEqual(broker._normalize_market_instrument("NSE", "NIFTY"), ("NSE", "NIFTY 50"))
        self.assertEqual(broker._normalize_market_instrument("NSE", "BANKNIFTY"), ("NSE", "NIFTY BANK"))
        self.assertEqual(broker._normalize_market_instrument("NSE", "SENSEX"), ("BSE", "SENSEX"))

    def test_stock_symbol_normalization_keeps_nse_equity_symbol(self) -> None:
        broker = ZerodhaBroker(settings())

        self.assertEqual(broker._normalize_market_instrument("NSE", "sbin"), ("NSE", "SBIN"))


if __name__ == "__main__":
    unittest.main()
