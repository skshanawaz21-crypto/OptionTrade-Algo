import unittest
from datetime import datetime, timedelta

from algotrader.dashboard import (
    BROKER_HEALTH_CACHE_TTL,
    EngineSupervisor,
    _extract_request_token,
)


class TestTokenRefreshHelpers(unittest.TestCase):
    def test_extract_request_token_from_redirect_url(self) -> None:
        token = _extract_request_token(
            "http://localhost:8000/?action=login&type=login&status=success&request_token=abc123XYZ"
        )

        self.assertEqual(token, "abc123XYZ")

    def test_extract_request_token_accepts_raw_token(self) -> None:
        self.assertEqual(_extract_request_token("abc123XYZ"), "abc123XYZ")

    def test_extract_request_token_rejects_empty_input(self) -> None:
        with self.assertRaises(ValueError):
            _extract_request_token("   ")

    def test_broker_health_cache_remains_valid_for_one_hour(self) -> None:
        supervisor = EngineSupervisor()
        cached = {
            "status": "ok",
            "label": "OK",
            "message": "cached",
        }
        supervisor._broker_health_cache = cached
        supervisor._broker_health_checked_at = datetime.now() - timedelta(minutes=59)

        self.assertEqual(BROKER_HEALTH_CACHE_TTL, timedelta(hours=1))
        self.assertIs(supervisor.broker_health_status(quick=True), cached)

    def test_quick_status_does_not_validate_after_hourly_cache_expires(self) -> None:
        supervisor = EngineSupervisor()
        supervisor._broker_health_cache = {
            "status": "ok",
            "label": "OK",
            "message": "stale",
        }
        supervisor._broker_health_checked_at = datetime.now() - timedelta(minutes=61)

        status = supervisor.broker_health_status(quick=True)

        self.assertEqual(status["status"], "checking")
        self.assertIn("once per hour", status["message"])


if __name__ == "__main__":
    unittest.main()
