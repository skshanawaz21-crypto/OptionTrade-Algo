# OptionTrader Strategy Specification

Status: paper-only, not approved for live trading.

This document describes the strategies currently implemented in the repository.
It is intentionally strict: if a rule is not implemented in code, it is not part
of the strategy.

## Market

- Instrument class: long-only listed Indian options.
- Execution broker target: Zerodha Kite.
- Default mode: paper.
- Live status: blocked by validation and execution-safety gaps.
- Capital base: INR 500,000 in `config/strategy.v1.nifty250_scanner_options.json`.

## Shared Session Rules

- Trading start: 09:20 IST.
- Trading end: 15:20 IST.
- No new trades after: 15:05 IST.
- Square-off time: 15:18 IST.
- Opening blackout: 09:15-09:25 IST.
- Expiry day behavior: no new trades.

## Shared Risk Rules

- Global max open positions: 4.
- Stock-option max open positions: 2.
- Index-option max open positions: 2.
- Max daily loss: INR 10,000.
- Daily loss kill switch: enabled.
- Per-strategy daily loss limit: INR 5,000.
- Min option premium: INR 5.00.
- Max premium per trade: INR 70,000.
- Max exposure per underlying: INR 90,000.
- Same-symbol cooldown after a loss: 60 minutes.
- Same-side strategy cooldown: after 2 losses on the same CE/PE side within 45 minutes.
- Profit protection:
  - 30% progress: move stop near breakeven.
  - 50% progress: lock profit.
  - 70% progress: extend target and tighten stop, capped by `max_target_extensions`.

## Strategy 1: Watchlist Directional Options

Type: trend-following breakout/momentum option buying.

Universe:
- Enabled watchlist items from the config.
- Mostly stock options.
- Index watchlist item can exist but is disabled by config.

Data:
- Broker historical candles when available.
- Local candle cache/public fallback when broker historical API fails.

Entry:
- Compute EMA20, EMA50, RSI14, momentum, previous high/low.
- Bullish regime when EMA20 > EMA50 and RSI14 >= 55.
- Bearish regime when EMA20 < EMA50 and RSI14 <= 45.
- Bullish entry requires bullish regime, breakout above previous high, and positive momentum.
- Bearish entry requires bearish regime, breakdown below previous low, and negative momentum.
- Bullish maps to CE.
- Bearish maps to PE.

Exit:
- Stop and target are based on option premium percentages.
- Active positions are managed every engine cycle and during the sleep loop.
- Manual exit and session square-off can close trades.
- Profit-protection can move stops and extend targets.

Known limitations:
- No market breadth filter.
- No higher-timeframe confirmation.
- No explicit sideways/chop filter beyond EMA/RSI neutrality.
- No proof of positive expectancy.

## Strategy 2: NIFTY250 2m Engulfing Scanner

Type: short-term two-candle engulfing/reversal scanner implemented as long option buying.

Universe:
- NIFTY LargeMidcap 250 symbol list fetched from NSE.
- Fallback popular symbols if NSE symbol fetch fails.

Data:
- Currently downloaded with `yfinance` inside `algotrader/nifty250_strategy.py`.
- This is not yet unified with the Zerodha broker data path.

Entry:
- Use latest two candles.
- Bullish engulfing:
  - previous candle red,
  - current candle green,
  - current body engulfs previous body.
- Bearish engulfing:
  - previous candle green,
  - current candle red,
  - current body engulfs previous body.
- Bullish maps to CE.
- Bearish maps to PE.
- Score is computed from body size, range size, volume ratio, and engulfing status.

Current safety filters:
- Minimum score: 60.
- Minimum candle body size: 0.10%.
- Minimum volume ratio: 1.20.
- Sideways market tightening:
  - reference indexes: NIFTY and SENSEX,
  - if enough references are sideways/choppy, scanner trades must meet score >= 80 and volume ratio >= 1.50.

Exit:
- Initial stop is at least 20% below option premium.
- Initial target is at least 40% above option premium.
- Profit protection and session exits are shared with other strategies.

Known limitations:
- Score is still heuristic, not statistically validated.
- No full option-chain historical backtest exists yet.
- `yfinance` data path must be replaced with broker-grade data before live use.

## Strategy 3: Index Options Scanner

Type: 5-minute index momentum/trend option buying.

Universe:
- NIFTY: NSE underlying, NFO options.
- BANKNIFTY: NSE underlying, NFO options.
- SENSEX: BSE underlying, BFO options.

Entry:
- Analyze 5-minute index candles.
- Bullish requires bullish regime, RSI >= 55, positive momentum, EMA gap, and breakout confirmation.
- Bearish requires bearish regime, RSI <= 45, negative momentum, EMA gap, and breakdown confirmation.
- Bullish maps to CE.
- Bearish maps to PE.
- Minimum score: 75.

Exit:
- Stop and target use the shared option premium risk configuration.
- Profit protection, manual exit, and session square-off apply.

Current status:
- Enabled but tightened.
- Historical paper performance is negative.
- Not approved for live trading until it produces positive out-of-sample paper results.

## Validation Requirements Before Live

Live trading is blocked until all of the following are true:

- At least 4-6 weeks of paper trading under the same rules.
- Positive profit factor after charges and realistic slippage.
- No dependency on one or two outlier trades.
- Strategy-level daily loss and cooldown behavior verified in paper.
- Broker order reconciliation implemented.
- Partial fill and rejected order handling implemented.
- Broker-side protective exits or equivalent live safety implemented.
- Historical option quote/backtest dataset available.
- Walk-forward report produced.
- Strategy rules frozen in this document.

## Explicit Non-Goals For Current Version

- No option selling.
- No overnight carry by design.
- No real-money deployment.
- No assumption that paper fills equal live fills.
