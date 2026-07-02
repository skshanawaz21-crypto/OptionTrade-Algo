# Sharing OptionTrader Safely

Use the share package workflow when giving OptionTrader to another person. Do not send your working folder directly.

## What The Package Keeps

- Strategy code and dashboard code
- Broker adapters and Zerodha token-refresh workflow
- Strategy config files under `config/`
- Tests and documentation
- `.env.example` so your friend can add their own credentials

## What The Package Excludes

- `.env`
- `access_token.txt`
- `fyers_access_token.txt`
- `data/paper_state.json`
- `data/engine_commands.jsonl`
- `data/candles/`
- `data/log_archive/`
- `logs.txt`
- Dashboard logs
- Virtual environments, Python caches, and local installer files

## Create A Clean Zip

From `C:\OptionTrader`:

```powershell
.\.venv\Scripts\python.exe tools\prepare_share_package.py
```

The zip is written under `dist\`.

To preview the file list first:

```powershell
.\.venv\Scripts\python.exe tools\prepare_share_package.py --dry-run
```

## Friend Setup

Your friend should unzip the package, then run:

```powershell
cd C:\OptionTrader
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

They must edit `.env` and set their own Zerodha values:

```text
ZERODHA_API_KEY=their_api_key
ZERODHA_API_SECRET=their_api_secret
ZERODHA_ACCESS_TOKEN=
ZERODHA_TOKEN_FILE=access_token.txt
```

Then start the dashboard:

```powershell
.\.venv\Scripts\python.exe run_dashboard.py
```

Open:

```text
http://127.0.0.1:8877/
```

The top-right Zerodha token control will show token status. They should open the Zerodha login URL, login with their own Zerodha account, then paste the redirected URL or request token into the dashboard. The dashboard will create their own `access_token.txt` and update their local `.env`.

## Important

- The shared package starts with no paper-trade history.
- Paper mode remains the default operating assumption.
- Existing strategy configuration is preserved.
- Your friend's paper state, logs, candles, and tokens will be generated on their machine only.
