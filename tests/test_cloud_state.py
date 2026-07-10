import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from algotrader.cloud_state import CloudStateStore


class CloudStateStoreTests(unittest.TestCase):
    def test_default_context_strategy_settings_and_paper_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "optiontrader.db"
            state_path = Path(tmp) / "paper_state.json"
            store = CloudStateStore(db_path)
            context = store.ensure_default_context(
                capital=500000,
                max_daily_loss=10000,
                max_open_positions=4,
            )
            store.seed_default_strategies(scanner_enabled=True, index_scanner_enabled=True)
            store.ensure_default_strategy_settings(context)

            settings = store.list_strategy_settings(context)
            self.assertGreaterEqual(len(settings), 4)
            self.assertTrue(
                next(row for row in settings if row["strategy_slug"] == "index_options_scanner")[
                    "enabled"
                ]
            )

            store.set_strategy_enabled(context, "index_options_scanner", False)
            settings = store.list_strategy_settings(context)
            self.assertFalse(
                next(row for row in settings if row["strategy_slug"] == "index_options_scanner")[
                    "enabled"
                ]
            )

            payload = {
                "saved_at": "2026-07-09T10:15:00+05:30",
                "account": {
                    "starting_capital": 500000,
                    "realized_pnl": 250,
                    "capital_committed": 10000,
                    "available_balance": 490250,
                },
                "open_trades": [
                    {
                        "symbol": "SENSEX",
                        "exchange": "BFO",
                        "direction": "BUY",
                        "tradingsymbol": "SENSEX2670976000PE",
                        "entry_price": 100.0,
                        "current_price": 110.0,
                        "stop_loss": 80.0,
                        "target_price": 140.0,
                        "quantity": 20,
                        "instrument_type": "index_option",
                        "option_side": "PE",
                        "entry_reason": "index_options_scanner interval=5minute score=80.00",
                        "opened_at": "2026-07-09T10:10:00+05:30",
                    }
                ],
                "closed_trades": [
                    {
                        "symbol": "TCS",
                        "exchange": "NFO",
                        "direction": "BUY",
                        "tradingsymbol": "TCS26MAY2300CE",
                        "entry_price": 50.0,
                        "exit_price": 60.0,
                        "stop_loss": 40.0,
                        "target_price": 70.0,
                        "quantity": 100,
                        "instrument_type": "stock_option",
                        "option_side": "CE",
                        "gross_pnl": 1000.0,
                        "total_charges": 50.0,
                        "pnl": 950.0,
                        "exit_reason": "Target hit",
                        "entry_reason": "watchlist directional",
                        "opened_at": "2026-07-09T09:30:00+05:30",
                        "closed_at": "2026-07-09T10:00:00+05:30",
                    }
                ],
            }
            state_path.write_text(json.dumps(payload), encoding="utf-8")

            summary = store.migrate_json_state(context, state_path)

            self.assertEqual(summary["open_trades"], 1)
            self.assertEqual(summary["closed_trades"], 1)
            restored = store.load_paper_state(context)
            self.assertEqual(restored["account"]["realized_pnl"], 250)

            conn = sqlite3.connect(db_path)
            try:
                open_rows = conn.execute("SELECT strategy_bucket FROM paper_open_trades").fetchall()
                closed_rows = conn.execute("SELECT pnl FROM paper_closed_trades").fetchall()
            finally:
                conn.close()
            self.assertEqual(open_rows[0][0], "Index Options Scanner")
            self.assertEqual(closed_rows[0][0], 950.0)

    def test_market_data_cache_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CloudStateStore(Path(tmp) / "optiontrader.db")
            store.upsert_market_data_cache(
                provider="kite_quote",
                exchange="NSE",
                symbol="NIFTY 50",
                data_type="quote",
                payload={"last_price": 24000.5},
                last_price=24000.5,
                as_of="2026-07-09T10:00:00+05:30",
            )

            cached = store.get_market_data_cache(
                provider="kite_quote",
                exchange="NSE",
                symbol="NIFTY 50",
                data_type="quote",
            )

            self.assertIsNotNone(cached)
            self.assertEqual(cached["last_price"], 24000.5)
            self.assertEqual(cached["payload"]["last_price"], 24000.5)

    def test_explicit_user_contexts_have_separate_strategy_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CloudStateStore(Path(tmp) / "optiontrader.db")
            owner = store.ensure_user_context(
                email="owner@example.com",
                display_name="Owner",
                account_name="Default Paper Account",
                capital=500000,
                role="admin",
            )
            friend = store.ensure_user_context(
                email="friend@example.com",
                display_name="Friend",
                account_name="Default Paper Account",
                capital=250000,
                role="user",
            )
            store.seed_default_strategies(scanner_enabled=True, index_scanner_enabled=True)
            store.ensure_default_strategy_settings(owner)
            store.ensure_default_strategy_settings(friend)

            store.set_strategy_enabled(friend, "index_options_scanner", False)

            owner_settings = {
                row["strategy_slug"]: row["enabled"]
                for row in store.list_strategy_settings(owner)
            }
            friend_settings = {
                row["strategy_slug"]: row["enabled"]
                for row in store.list_strategy_settings(friend)
            }

            self.assertNotEqual(owner.user_id, friend.user_id)
            self.assertNotEqual(owner.paper_account_id, friend.paper_account_id)
            self.assertTrue(owner_settings["index_options_scanner"])
            self.assertFalse(friend_settings["index_options_scanner"])

    def test_user_broker_profile_is_account_scoped_and_hides_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "optiontrader.db"
            store = CloudStateStore(db_path)
            owner = store.ensure_user_context(
                email="owner@example.com",
                display_name="Owner",
                account_name="Default Paper Account",
                capital=500000,
                role="admin",
            )
            friend = store.ensure_user_context(
                email="friend@example.com",
                display_name="Friend",
                account_name="Default Paper Account",
                capital=250000,
                role="user",
            )

            owner_summary = store.set_broker_profile(
                owner,
                provider="zerodha",
                api_key="owner_api_key_123",
                api_secret="owner_secret_456",
                access_token="owner_access_token_789",
            )
            friend_summary = store.set_broker_profile(
                friend,
                provider="dhan",
                client_id="friend_client_123",
                client_secret="friend_secret_456",
            )

            self.assertEqual(owner_summary["active_profile"]["provider"], "zerodha")
            self.assertEqual(friend_summary["active_profile"]["provider"], "dhan")
            self.assertNotIn("owner_secret_456", json.dumps(owner_summary))
            self.assertNotIn("owner_access_token_789", json.dumps(owner_summary))
            self.assertNotIn("friend_secret_456", json.dumps(friend_summary))

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT secret_payload, token_payload FROM user_broker_profiles WHERE provider = 'zerodha'"
                ).fetchone()
            finally:
                conn.close()
            self.assertIsNotNone(row)
            self.assertNotIn("owner_secret_456", row["secret_payload"])
            self.assertNotIn("owner_access_token_789", row["token_payload"])


if __name__ == "__main__":
    unittest.main()
