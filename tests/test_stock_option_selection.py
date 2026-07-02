import unittest

from algotrader.config import ContractSelectionConfig, WatchItem
from algotrader.contract_selector import ContractSelector
from algotrader.models import OptionChainEntry
from algotrader.option_chain import OptionChainService


class OptionChainBrokerStub:
    def __init__(self, rows):
        self.rows = rows

    def get_option_chain(self, underlying_symbol: str, contract_exchange: str, spot_price: float):
        return self.rows


class TestStockOptionSelection(unittest.TestCase):
    def test_stock_options_keep_monthly_rows_beyond_index_weekly_dte_window(self) -> None:
        item = WatchItem(
            symbol="TATAPOWER",
            exchange="NSE",
            instrument_type="stock_option",
            interval="5minute",
        )
        policy = ContractSelectionConfig(
            expiry_type="weekly",
            min_dte=1,
            max_dte=14,
            min_oi=100,
            min_volume=100,
        )
        rows = [
            OptionChainEntry(
                tradingsymbol="TATAPOWER26MAY440CE",
                exchange="NFO",
                underlying_symbol="TATAPOWER",
                option_side="CE",
                expiry="2026-05-26",
                strike=440,
                dte_days=20,
                lot_size=3375,
                ltp=14.5,
                oi=100000,
                volume=50000,
                spread_pct=0.8,
            )
        ]

        snapshot = OptionChainService(OptionChainBrokerStub(rows)).load(item, 442, policy)
        selected = ContractSelector().select(snapshot.rows, item, policy, 442, "bullish")

        self.assertEqual(len(snapshot.rows), 1)
        self.assertEqual(selected.contract.tradingsymbol, "TATAPOWER26MAY440CE")


if __name__ == "__main__":
    unittest.main()
