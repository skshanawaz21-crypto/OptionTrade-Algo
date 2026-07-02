# Roadmap

## Phase 1: Platform Foundation

Status: Completed

- Build modular project structure
- Add Zerodha adapter
- Add paper/live execution switch
- Add risk manager
- Add option analytics
- Add token refresh flow
- Add fallback local candle builder

## Phase 2: Strategy Definition

Status: In progress

- Capture exact market to trade
- Capture timeframe and session timing
- Capture entry rules
- Capture exit rules
- Capture stop-loss and target logic
- Capture quantity and capital rules
- Capture no-trade filters

## Phase 3: Paper Trading Validation

Status: Pending

- Run long enough to warm local candle history
- Verify signals are generated at expected times
- Review false signals and noisy behavior
- Tune thresholds and watchlist
- Add trade journal output

## Phase 4: Broker Execution Hardening

Status: Pending

- Move from personal Zerodha app to Connect app if required
- Validate order placement and exit logic end to end
- Add restart-safe position persistence
- Add kill switch and max-daily-loss enforcement logs

## Phase 5: Production Readiness

Status: Pending

- Add monitoring and health checks
- Add reporting dashboard or summary logs
- Add automated startup/resume workflow
- Add deployment option for fixed-IP VPS if needed
