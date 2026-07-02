import unittest

from algotrader.performance_report import infer_option_side, strategy_bucket, summarize


class TestPerformanceReport(unittest.TestCase):
    def test_infer_option_side_prefers_contract_suffix(self) -> None:
        self.assertEqual(infer_option_side("PERSISTENT26MAY4700CE", "PE"), "CE")

    def test_strategy_bucket_classifies_scanner_reason(self) -> None:
        trade = {
            "symbol": "BIOCON",
            "instrument_type": "stock_option",
            "entry_reason": "NIFTY250_2m_scanner score=62.43 vol_ratio=0.76",
        }

        self.assertEqual(strategy_bucket(trade), "NIFTY250 2m Scanner")

    def test_summarize_reports_profit_factor_and_loss_streak(self) -> None:
        rows = [{"pnl": 100.0}, {"pnl": -50.0}, {"pnl": -25.0}, {"pnl": 75.0}]

        summary = summarize(rows)

        self.assertEqual(summary.trades, 4)
        self.assertEqual(summary.net_pnl, 100.0)
        self.assertEqual(summary.profit_factor, 2.333)
        self.assertEqual(summary.max_consecutive_losses, 2)


if __name__ == "__main__":
    unittest.main()
