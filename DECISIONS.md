# Decisions

## 2026-04-21 / 2026-04-22

- Chosen architecture: modular Python package under `algotrader/`
- Execution modes: `paper` and `live`
- Broker integration target: Zerodha Kite Connect
- Risk approach: per-trade risk sizing plus drawdown and VaR-style checks
- Options analytics included: Black-Scholes, IV, Greeks
- Current fallback design: use public market data for analysis and build local
  candles when Zerodha quote/historical permissions are unavailable
- Persistence chosen for local warm-up: CSV files under `data/candles/`
- Token handling moved out of hardcoded source and into `.env` plus
  `access_token.txt`

## Open Decisions

- Final instrument universe
- Final timeframe
- Final strategy rules
- Whether to keep fallback public data or migrate fully to Zerodha Connect app
- Whether to deploy on local machine or fixed-IP remote server later
