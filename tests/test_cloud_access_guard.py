import os
import unittest
from unittest.mock import patch

from algotrader.dashboard import _cloud_access_auth_error


class CloudAccessGuardTests(unittest.TestCase):
    def test_guard_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(_cloud_access_auth_error({"Host": "paper.example.com"}))

    def test_localhost_bypasses_guard_when_enabled(self):
        with patch.dict(os.environ, {"OPTIONTRADER_CLOUD_ACCESS_REQUIRED": "1"}, clear=True):
            self.assertIsNone(_cloud_access_auth_error({"Host": "127.0.0.1:8877"}))
            self.assertIsNone(_cloud_access_auth_error({"Host": "localhost:8877"}))

    def test_cloud_host_requires_cloudflare_access_email(self):
        with patch.dict(os.environ, {"OPTIONTRADER_CLOUD_ACCESS_REQUIRED": "1"}, clear=True):
            error = _cloud_access_auth_error({"Host": "paper.example.com"})

        self.assertIn("Cloudflare Access login is required", error)

    def test_cloudflare_access_email_is_allowed_when_no_allowlist_is_set(self):
        with patch.dict(os.environ, {"OPTIONTRADER_CLOUD_ACCESS_REQUIRED": "1"}, clear=True):
            self.assertIsNone(
                _cloud_access_auth_error(
                    {
                        "Host": "paper.example.com",
                        "Cf-Access-Authenticated-User-Email": "friend@example.com",
                    }
                )
            )

    def test_cloudflare_access_email_allowlist_blocks_unknown_users(self):
        with patch.dict(
            os.environ,
            {
                "OPTIONTRADER_CLOUD_ACCESS_REQUIRED": "1",
                "OPTIONTRADER_CLOUD_ALLOWED_EMAILS": "owner@example.com,friend@example.com",
            },
            clear=True,
        ):
            error = _cloud_access_auth_error(
                {
                    "Host": "paper.example.com",
                    "Cf-Access-Authenticated-User-Email": "stranger@example.com",
                }
            )

        self.assertIn("not allowed", error)

    def test_cloudflare_access_email_allowlist_allows_known_users(self):
        with patch.dict(
            os.environ,
            {
                "OPTIONTRADER_CLOUD_ACCESS_REQUIRED": "1",
                "OPTIONTRADER_CLOUD_ALLOWED_EMAILS": "owner@example.com,friend@example.com",
            },
            clear=True,
        ):
            self.assertIsNone(
                _cloud_access_auth_error(
                    {
                        "Host": "paper.example.com",
                        "Cf-Access-Authenticated-User-Email": "Friend@Example.com",
                    }
                )
            )


if __name__ == "__main__":
    unittest.main()
