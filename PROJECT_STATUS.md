# Project Status

## Project

AlgoTrader for Indian markets with Zerodha integration, paper/live execution
paths, local candle building, and configurable strategy/risk layers.

## Current Stage

Stage: Foundation complete, strategy customization pending

## What Is Working

- Python 3.14 virtual environment created in `.venv`
- Core package structure implemented
- Zerodha authentication flow implemented through `refresh_token.py`
- Access token refresh tested successfully
- Paper-mode execution path works
- Local candle persistence works under `data/candles/`
- Public market-data fallback works for analysis when Zerodha quote/historical
  permissions are unavailable
- Options math tests pass

## Current Blockers

- Current Zerodha app is a `Personal` app, so it does not provide the market
  data access required for direct historical/LTP usage through Kite
- Local candle mode needs warm-up time before indicators can produce signals
- Strategy logic is still generic and not yet aligned to the user's exact entry
  and exit rules

## Important Paths

- Main project: `C:\AlgoTrader`
- Main entry point: `C:\AlgoTrader\main.py`
- Token refresh: `C:\AlgoTrader\refresh_token.py`
- Example config: `C:\AlgoTrader\config\strategy.example.json`
- Local candle cache: `C:\AlgoTrader\data\candles`

## Last Verified State

- `python -m unittest discover -s tests`: passed
- `main.py --mode paper --once`: ran successfully in local-candle warm-up mode
- Candle files present for `RELIANCE` and `SBIN`

## Next Recommended Step

Create a strategy-specific config and implementation for the user's actual
trading rules, then run a longer paper-trading session to collect enough local
candles for signal generation.
