import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from algotrader.dashboard import (
    EngineSupervisor,
    _cloud_access_auth_error,
    _is_owner_email,
    _owner_emails,
    _request_cloud_identity,
)


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

    def test_request_identity_uses_cloudflare_email_when_present(self):
        identity = _request_cloud_identity(
            {
                "Host": "paper.example.com",
                "Cf-Access-Authenticated-User-Email": "Friend.User@example.com",
            }
        )

        self.assertEqual(identity["email"], "friend.user@example.com")
        self.assertEqual(identity["display_name"], "Friend User")
        self.assertEqual(identity["source"], "cloudflare_access")

    def test_request_identity_falls_back_to_default_owner(self):
        with patch.dict(
            os.environ,
            {"OPTIONTRADER_DEFAULT_USER_EMAIL": "owner@example.com"},
            clear=True,
        ):
            identity = _request_cloud_identity({"Host": "127.0.0.1:8877"})

        self.assertEqual(identity["email"], "owner@example.com")
        self.assertEqual(identity["source"], "local_owner")

    def test_public_host_without_cloudflare_email_is_not_owner_identity(self):
        with patch.dict(
            os.environ,
            {"OPTIONTRADER_DEFAULT_USER_EMAIL": "owner@example.com"},
            clear=True,
        ):
            identity = _request_cloud_identity({"Host": "bot.example.com"})

        self.assertEqual(identity["email"], "public-guest@optiontrader.local")
        self.assertEqual(identity["source"], "public_unauthenticated")

    def test_owner_emails_include_default_and_explicit_cloud_owners(self):
        with patch.dict(
            os.environ,
            {
                "OPTIONTRADER_DEFAULT_USER_EMAIL": "local-owner@optiontrader.local",
                "OPTIONTRADER_OWNER_EMAILS": "Owner@Example.com, second@example.com",
            },
            clear=True,
        ):
            self.assertEqual(
                _owner_emails(),
                {
                    "local-owner@optiontrader.local",
                    "owner@example.com",
                    "second@example.com",
                },
            )

    def test_is_owner_email_accepts_explicit_cloud_owner_alias(self):
        with patch.dict(
            os.environ,
            {
                "OPTIONTRADER_DEFAULT_USER_EMAIL": "local-owner@optiontrader.local",
                "OPTIONTRADER_OWNER_EMAILS": "Owner@Example.com",
            },
            clear=True,
        ):
            self.assertTrue(_is_owner_email("owner@example.com"))
            self.assertFalse(_is_owner_email("friend@example.com"))

    def test_owner_emails_are_allowed_by_app_guard(self):
        with patch.dict(
            os.environ,
            {
                "OPTIONTRADER_CLOUD_ACCESS_REQUIRED": "1",
                "OPTIONTRADER_CLOUD_ALLOWED_EMAILS": "friend@example.com",
                "OPTIONTRADER_OWNER_EMAILS": "owner@example.com",
            },
            clear=True,
        ):
            self.assertIsNone(
                _cloud_access_auth_error(
                    {
                        "Host": "paper.example.com",
                        "Cf-Access-Authenticated-User-Email": "owner@example.com",
                    }
                )
            )

    def test_owner_cloudflare_alias_uses_default_paper_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {
                    "OPTIONTRADER_DB_PATH": str(Path(tmp) / "optiontrader.db"),
                    "OPTIONTRADER_DEFAULT_USER_EMAIL": "local-owner@optiontrader.local",
                    "OPTIONTRADER_OWNER_EMAILS": "owner@example.com",
                },
                clear=True,
            ):
                supervisor = EngineSupervisor()
                context = supervisor._cloud_context(
                    {
                        "Host": "paper.example.com",
                        "Cf-Access-Authenticated-User-Email": "owner@example.com",
                    }
                )

            self.assertEqual(context.user_email, "local-owner@optiontrader.local")

    def test_public_host_without_cloudflare_email_does_not_use_env_token_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            headers = {"Host": "bot.example.com"}
            with patch.dict(
                os.environ,
                {
                    "OPTIONTRADER_DB_PATH": str(Path(tmp) / "optiontrader.db"),
                    "OPTIONTRADER_SECRET_KEY_FILE": str(Path(tmp) / "secret.key"),
                    "OPTIONTRADER_DEFAULT_USER_EMAIL": "local-owner@optiontrader.local",
                    "ZERODHA_API_KEY": "owner_env_key_should_not_be_used",
                    "ZERODHA_API_SECRET": "owner_env_secret_should_not_be_used",
                },
                clear=True,
            ):
                supervisor = EngineSupervisor()
                token_status = supervisor.zerodha_token_status(headers=headers)

            self.assertEqual(token_status["status"], "not_configured")
            self.assertEqual(token_status["login_url"], "")
            self.assertIn("Cloudflare Access login is required", token_status["message"])

    def test_public_host_without_cloudflare_email_cannot_save_broker_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            headers = {"Host": "bot.example.com"}
            with patch.dict(
                os.environ,
                {
                    "OPTIONTRADER_DB_PATH": str(Path(tmp) / "optiontrader.db"),
                    "OPTIONTRADER_SECRET_KEY_FILE": str(Path(tmp) / "secret.key"),
                    "OPTIONTRADER_DEFAULT_USER_EMAIL": "local-owner@optiontrader.local",
                },
                clear=True,
            ):
                supervisor = EngineSupervisor()
                with self.assertRaises(RuntimeError) as ctx:
                    supervisor.set_user_broker_profile(
                        {
                            "provider": "zerodha",
                            "api_key": "profile_key",
                            "api_secret": "profile_secret",
                        },
                        headers,
                    )

            self.assertIn("Cloudflare Access login is required", str(ctx.exception))

    def test_user_zerodha_token_status_uses_saved_profile_api_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            headers = {
                "Host": "paper.example.com",
                "Cf-Access-Authenticated-User-Email": "friend@example.com",
            }
            with patch.dict(
                os.environ,
                {
                    "OPTIONTRADER_DB_PATH": str(Path(tmp) / "optiontrader.db"),
                    "OPTIONTRADER_SECRET_KEY_FILE": str(Path(tmp) / "secret.key"),
                    "OPTIONTRADER_DEFAULT_USER_EMAIL": "local-owner@optiontrader.local",
                    "ZERODHA_API_KEY": "wrong_owner_env_key",
                    "ZERODHA_API_SECRET": "wrong_owner_env_secret",
                },
                clear=True,
            ):
                supervisor = EngineSupervisor()
                context = supervisor._cloud_context(headers)
                supervisor._cloud_state.set_broker_profile(
                    context,
                    provider="zerodha",
                    api_key="profile_key_123",
                    api_secret="profile_secret_456",
                )

                token_status = supervisor.zerodha_token_status(headers=headers)

            self.assertEqual(token_status["status"], "required")
            self.assertIn("profile_key_123", token_status["login_url"])
            self.assertNotIn("wrong_owner_env_key", token_status["login_url"])
            self.assertIn("this paper account", token_status["message"])

    def test_user_non_zerodha_profile_does_not_show_zerodha_login_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            headers = {
                "Host": "paper.example.com",
                "Cf-Access-Authenticated-User-Email": "friend@example.com",
            }
            with patch.dict(
                os.environ,
                {
                    "OPTIONTRADER_DB_PATH": str(Path(tmp) / "optiontrader.db"),
                    "OPTIONTRADER_SECRET_KEY_FILE": str(Path(tmp) / "secret.key"),
                    "OPTIONTRADER_DEFAULT_USER_EMAIL": "local-owner@optiontrader.local",
                },
                clear=True,
            ):
                supervisor = EngineSupervisor()
                context = supervisor._cloud_context(headers)
                supervisor._cloud_state.set_broker_profile(
                    context,
                    provider="dhan",
                    client_id="dhan_client_id",
                    client_secret="dhan_secret",
                )

                token_status = supervisor.zerodha_token_status(headers=headers)

            self.assertEqual(token_status["status"], "not_configured")
            self.assertEqual(token_status["login_url"], "")
            self.assertIn("different broker", token_status["message"])


if __name__ == "__main__":
    unittest.main()
