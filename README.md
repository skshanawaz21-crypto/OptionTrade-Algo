# OptionTrader

OptionTrader is an options-first Python trading system for the Indian market.
It keeps the reusable Zerodha, dashboard, logging, and paper-trading framework
from the earlier project, but the default behavior is now focused on buying
call or put option contracts from an underlying-based signal.

## What Changed From AlgoTrader

- Project identity and dashboard language now point to options trading.
- The default sample strategy is `NIFTY` options, not equity names.
- Position sizing is lot-based, not raw share-based.
- Paper trades persist CE or PE metadata such as strike, expiry, lot size, and lots.
- Paper P&L uses option-buying style charges instead of equity intraday charges.

## Current Strategy Shape

- Underlying candles are built on the configured symbol, such as `NIFTY`.
- Trend and breakout logic runs on the underlying.
- A bullish signal buys a `CE`.
- A bearish signal buys a `PE`.
- Stop loss and target are set on option premium percentages.
- Quantity is rounded to valid lot sizes and capped by `max_trade_value`.

## Project Layout

- `main.py`: CLI entry point
- `run_dashboard.py`: local dashboard entry point
- `config/strategy.example.json`: sample options-first strategy file
- `algotrader/config.py`: settings and strategy config loader
- `algotrader/brokers/zerodha.py`: Zerodha broker and option contract discovery
- `algotrader/brokers/fyers.py`: optional FYERS market-data fallback for paper quotes
- `refresh_fyers_token.py`: FYERS token helper
- `algotrader/strategy.py`: underlying analysis and signal generation
- `algotrader/engine.py`: execution loop and paper or live trade lifecycle
- `algotrader/options.py`: Black-Scholes helpers, IV, and strike estimation
- `algotrader/risk.py`: risk checks and lot-based sizing
- `algotrader/dashboard.py`: browser dashboard

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Copy `.env.example` to `.env` and fill in your Zerodha values.
4. Keep `access_token.txt` in the project root, or point `ZERODHA_TOKEN_FILE` to your token file.
5. Start with the sample config in `config/strategy.example.json`.

## Optional FYERS Market Data Fallback

FYERS can be used for paper-mode LTP/quote data when Zerodha profile/order APIs
work but Kite quote/LTP permission is blocked.

1. Create a FYERS API app and set these values in `.env`:

```bash
MARKET_DATA_PROVIDER=auto
FYERS_CLIENT_ID=your_fyers_app_id
FYERS_SECRET_KEY=your_fyers_secret
FYERS_REDIRECT_URI=your_registered_redirect_uri
FYERS_ACCESS_TOKEN=
FYERS_TOKEN_FILE=fyers_access_token.txt
FYERS_DATA_BASE_URL=https://api-t1.fyers.in/data
FYERS_AUTH_BASE_URL=https://api-t1.fyers.in/api/v3
```

2. Generate the FYERS token:

```bash
python refresh_fyers_token.py
```

3. Restart the dashboard. Broker health should show `FYERS DATA` when Zerodha
quote permission is blocked but FYERS quotes are available.

OptionTrader still uses Zerodha-first contract discovery and live execution.
FYERS is currently wired as a paper-mode market-data fallback, not as the live
order execution broker.

## Run

One cycle in paper mode:

```bash
python main.py --config config/strategy.example.json --mode paper --once
```

Continuous paper mode:

```bash
python main.py --config config/strategy.example.json --mode paper
```

Dashboard:

```bash
python run_dashboard.py
```

Then open `http://127.0.0.1:8877`.

## Runtime Data

- `data/paper_state.json`: persisted paper option trades
- `data/candles/`: underlying candle history used for warm-up and analysis
- `logs.txt`: current engine log
- `data/log_archive/`: archived log sessions

For a fresh options-only start after copying the old project, it is best to
clear `paper_state.json`, `logs.txt`, and stale candle files from unrelated
equity symbols before running the engine.

## Sharing With Another User

Do not share your working folder directly because it contains local tokens,
paper-trade history, logs, and candle cache files. Create a clean package:

```bash
python tools/prepare_share_package.py
```

The package keeps the strategy and dashboard code but excludes `.env`,
tokens, `data/paper_state.json`, logs, and cached market data. See
`SHARING.md` for the full workflow your friend should use with their own
Zerodha API login.
