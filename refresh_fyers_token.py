from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from algotrader.config import AppSettings


def extract_auth_code(raw_input: str) -> str:
    text = raw_input.strip()
    if "://" not in text:
        return text
    parsed = urlparse(text)
    query = parse_qs(parsed.query)
    for key in ("auth_code", "code"):
        token = query.get(key, [""])[0].strip()
        if token:
            return token
    raise ValueError("Could not extract FYERS auth_code from the provided URL.")


def main() -> int:
    settings = AppSettings.from_env()
    if not settings.fyers_client_id or not settings.fyers_secret_key or not settings.fyers_redirect_uri:
        raise ValueError(
            "FYERS_CLIENT_ID, FYERS_SECRET_KEY, and FYERS_REDIRECT_URI must be set in .env"
        )

    auth_base_url = settings.fyers_auth_base_url
    login_url = (
        f"{auth_base_url}/generate-authcode?"
        + urlencode(
            {
                "client_id": settings.fyers_client_id,
                "redirect_uri": settings.fyers_redirect_uri,
                "response_type": "code",
                "state": "optiontrader",
            }
        )
    )
    print("Open this FYERS login URL in your browser:")
    print(login_url)
    print()
    raw = input("Paste the redirected URL or just the auth_code: ").strip()
    auth_code = extract_auth_code(raw)
    app_id_hash = hashlib.sha256(
        f"{settings.fyers_client_id}:{settings.fyers_secret_key}".encode("utf-8")
    ).hexdigest()
    response = requests.post(
        f"{auth_base_url}/validate-authcode",
        json={
            "grant_type": "authorization_code",
            "appIdHash": app_id_hash,
            "code": auth_code,
        },
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    payload = response.json()
    if response.status_code >= 400 or not isinstance(payload, dict) or "access_token" not in payload:
        raise RuntimeError(f"FYERS token generation failed: {payload}")

    token_path = Path(settings.fyers_token_file)
    token_path.write_text(
        f"{settings.fyers_client_id}:{payload['access_token']}",
        encoding="utf-8",
    )
    print()
    print(f"FYERS access token saved to: {token_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
