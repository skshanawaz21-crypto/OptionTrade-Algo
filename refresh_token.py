from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

from kiteconnect import KiteConnect

from algotrader.config import AppSettings


def extract_request_token(raw_input: str) -> str:
    text = raw_input.strip()
    if "request_token=" not in text:
        return text

    parsed = urlparse(text)
    token = parse_qs(parsed.query).get("request_token", [""])[0].strip()
    if not token:
        raise ValueError("Could not extract request_token from the provided URL.")
    return token


def main() -> int:
    settings = AppSettings.from_env()
    if not settings.zerodha_api_key or not settings.zerodha_api_secret:
        raise ValueError("ZERODHA_API_KEY and ZERODHA_API_SECRET must be set in .env")

    kite = KiteConnect(api_key=settings.zerodha_api_key)
    print("Open this Zerodha login URL in your browser:")
    print(kite.login_url())
    print()
    raw = input("Paste the full redirected URL or just the request_token: ").strip()
    request_token = extract_request_token(raw)

    session = kite.generate_session(
        request_token,
        api_secret=settings.zerodha_api_secret,
    )
    access_token = session["access_token"]

    token_path = Path(settings.zerodha_token_file)
    token_path.write_text(access_token, encoding="utf-8")
    print()
    print(f"Access token saved to: {token_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
