# Multi-User Paper DB Runbook

Status: Compatibility foundation implemented  
Date: 2026-07-09  
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
OPTIONTRADER_DEFAULT_USER_NAME=Local Owner
OPTIONTRADER_DEFAULT_ACCOUNT_NAME=Default Paper Account
```

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

This is the first piece of the central market-data architecture:

```text
one data collector -> shared cache -> strategy workers/users
```

Important:

Caching reduces duplicate API calls. It does not grant market-data redistribution rights.

## What Is Not Yet Complete

This is not yet full independent multi-user paper trading.

Still required:

- Browser login/signup inside OptionTrader.
- Session cookies.
- Per-browser user routing.
- Per-user dashboard filtering.
- Per-user paper engine scheduler.
- User creation/admin UI.
- Strategy selection UI.
- PostgreSQL migration for production scale.
- Redis-based real-time quote/job cache.

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

