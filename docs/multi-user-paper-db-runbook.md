# Multi-User Paper DB Runbook

Status: Session-aware compatibility foundation implemented
Date: 2026-07-10
Scope: Self-hosted Phase 1A paper database foundation

## What Is Implemented

OptionTrader now has a local SQLite database foundation for the self-hosted paper platform.

Default DB path:

```text
data/optiontrader.db
```

This file is local runtime state and is ignored by Git.

Implemented tables:

- `users`
- `paper_accounts`
- `user_broker_profiles`
- `strategy_definitions`
- `strategy_versions`
- `user_strategy_settings`
- `paper_account_state`
- `paper_open_trades`
- `paper_closed_trades`
- `market_data_cache`
- `audit_events`

## Current Compatibility Mode

The existing engine still writes:

```text
data/paper_state.json
```

For safety, the engine now also mirrors every paper save into:

```text
data/optiontrader.db
```

This means:

- Current local dashboard behavior remains intact.
- Existing paper state is not deleted.
- Existing trade history is preserved.
- DB-backed paper state is available for cloud/multi-user migration.
- If JSON is absent, the engine can restore the default paper account from DB.

## Browser User Routing

The dashboard now resolves a paper user for each browser request:

- Localhost requests fall back to the configured local owner account.
- Cloudflare Access requests use:

```text
Cf-Access-Authenticated-User-Email
```

Each Cloudflare Access email gets:

- A row in `users`.
- A separate row in `paper_accounts`.
- Separate `user_strategy_settings`.
- Separate DB-backed paper account summary/trade shell.

Important privacy behavior:

- Owner/local requests can see the live engine JSON state, live engine log, active paper positions, completed trades, and owner controls.
- Non-owner Cloudflare users do not receive owner JSON trade history.
- Non-owner Cloudflare users do not receive the owner engine log.
- Non-owner Cloudflare users cannot start/stop/reset the global engine, edit the global watchlist, refresh the owner's Zerodha token, run JSON migration, or manual-exit owner trades.
- Non-owner Cloudflare users can save their own broker profile. For Zerodha profiles, the token panel uses that user's saved API key/secret and stores the refreshed token against that user's paper account.

Important architecture note:

- This is still not one independent engine per user.
- Viewer strategy settings are saved now and will be used by the future per-user worker scheduler.
- The current live paper worker remains the owner/local compatibility worker.

## Default User And Account

Until full login is built, the self-hosted app uses one default local account:

```text
User: local-owner@optiontrader.local
Account: Default Paper Account
```

These can be changed in `.env`:

```text
OPTIONTRADER_DB_PATH=data/optiontrader.db
OPTIONTRADER_DEFAULT_USER_EMAIL=local-owner@optiontrader.local
OPTIONTRADER_OWNER_EMAILS=
OPTIONTRADER_DEFAULT_USER_NAME=Local Owner
OPTIONTRADER_DEFAULT_ACCOUNT_NAME=Default Paper Account
```

If the owner will use the public Cloudflare hostname or mobile browser, set:

```text
OPTIONTRADER_OWNER_EMAILS=your-cloudflare-login-email@example.com
```

You may also set `OPTIONTRADER_DEFAULT_USER_EMAIL` to the same email, but `OPTIONTRADER_OWNER_EMAILS` is preferred because it lets the local default account remain stable while still granting owner controls to the owner's public/mobile Cloudflare Access session.

If this is not configured, the public/mobile owner browser will be treated as a separate paper user account. It can configure its own broker profile, but it will not control the local owner engine or see the local owner paper trades/logs.

Owner email behavior:

- Emails in `OPTIONTRADER_OWNER_EMAILS` are aliases for the local owner paper worker.
- Owner aliases can refresh Zerodha token, start/stop the engine, edit the global watchlist, and change strategy toggles that the current engine reads.
- Friend/viewer emails still get separate DB paper accounts and cannot access owner controls or owner logs/trades.

## Initialize Or Migrate DB

From `C:\OptionTrader`:

```powershell
.\.venv\Scripts\python.exe tools\init_cloud_paper_db.py
```

This will:

- Create the DB schema.
- Create the default user.
- Create the default paper account.
- Seed strategy definitions.
- Seed per-account strategy settings.
- Copy `data/paper_state.json` into DB if it exists.

It does not delete or reset JSON state.

## Strategy Selection

Per-account strategy settings are stored in:

```text
user_strategy_settings
```

The engine now honors these strategy toggles for:

- `nifty250_2m_engulfing_scanner`
- `index_options_scanner`
- `watchlist_directional_stock_options`
- `watchlist_directional_index_options`

Dashboard/API endpoints:

```text
GET  /api/strategy-settings
POST /api/strategy-settings
```

Example POST body:

```json
{
  "strategy_slug": "index_options_scanner",
  "enabled": false
}
```

Engine note:

- Strategy settings are cached for about 15 seconds inside the engine.
- Existing open trades are still managed even if the strategy is disabled.
- Disabling a strategy blocks new entries only.
- Owner settings affect the current local paper worker.
- Viewer settings are persisted for the future per-user worker layer.

Dashboard UI:

- The `Cloud Paper Control` panel shows the current browser user, paper account, DB trade counts, scope, and per-account strategy cards.
- Strategy cards can be toggled from the browser.
- Owner-only controls are disabled for viewer accounts.

## Per-User Broker Profile

Each paper user can now choose a broker profile from the dashboard:

- Zerodha Kite
- Dhan
- Upstox

The selected broker is stored per user/paper account in:

```text
user_broker_profiles
```

Secrets are encrypted before being written to SQLite. The local encryption key is loaded from:

```text
OPTIONTRADER_SECRET_KEY
```

or generated/stored at:

```text
OPTIONTRADER_SECRET_KEY_FILE=data/optiontrader_secret.key
```

The key file is local runtime state and must not be committed.

Dashboard/API endpoints:

```text
GET  /api/user-broker
POST /api/user-broker
```

Important:

- Broker API keys/tokens are never sent back to the browser after saving.
- Dashboard summaries only show masked identifiers.
- Zerodha login URLs are generated from the current paper account's saved Zerodha API key, not from another user's `.env` settings.
- Successful Zerodha token refresh stores the access token encrypted in the current paper account's broker profile.
- Owner/admin sessions also mirror a successful Zerodha refresh into the local compatibility token path and `.env` keys so the current local owner engine keeps working.
- Current working adapter support is still Zerodha-first in the owner/local engine.
- Dhan and Upstox profiles can be saved now, but their market-data adapters and per-user worker execution still need to be implemented before they can drive independent paper trading.
- Per-user paper trades/P&L are already represented in the DB model, but independent trading requires the user-worker scheduler layer.

## Cloud Paper Summary API

Dashboard/API endpoints:

```text
GET  /api/cloud-paper
POST /api/cloud-paper/migrate
```

`/api/status` also includes:

- `cloud_paper`
- `strategy_settings`

## Central Market Data Cache

The database now includes:

```text
market_data_cache
```

The dashboard's top index ticks are mirrored into this cache as quote snapshots.

This is the first piece of the market-data cache architecture:

```text
broker data collector -> quote cache -> strategy workers
```

Important:

Caching reduces duplicate API calls inside the system. It does not grant market-data redistribution rights. The intended external-user direction is per-user broker credentials/workers, not redistributing the owner's paid broker feed.

## What Is Not Yet Complete

This is not yet full independent multi-user paper trading.

Still required:

- Per-user dashboard filtering.
- Per-user paper engine scheduler.
- Per-user broker adapter runtime for Zerodha/Dhan/Upstox.
- Per-user Zerodha worker execution using the saved encrypted profile.
- Dhan/Upstox token/OAuth refresh flows and market-data adapters.
- User creation/admin UI.
- PostgreSQL migration for production scale.
- Redis-based real-time quote/job cache.
- Optional first-party login/signup and session cookies if Cloudflare Access is not the long-term identity layer.

## Verification Commands

```powershell
.\.venv\Scripts\python.exe -m py_compile algotrader\dashboard.py algotrader\engine.py algotrader\cloud_state.py
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe tools\init_cloud_paper_db.py
```

## Safety Rules

- Do not delete `data/paper_state.json` unless explicitly requested.
- Do not commit `data/optiontrader.db`.
- Do not print tokens or `.env`.
- Keep live trading disabled for this phase.
- Existing open trades must continue to be managed regardless of strategy toggles.
