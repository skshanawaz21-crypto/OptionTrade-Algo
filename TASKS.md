# Tasks

## Active

- [ ] Finalize instrument universe
- [ ] Finalize execution timeframe
- [ ] Capture exact entry rules in plain language
- [ ] Capture exact exit rules in plain language
- [ ] Convert the final strategy rules into config/code
- [ ] Run an extended paper-trading session for candle warm-up

## Next Up

- [ ] Add trade journal output
- [ ] Review signal quality after warm-up
- [ ] Tune thresholds and watchlist selection
- [ ] Add restart-safe position persistence
- [ ] Add kill-switch and max-daily-loss reporting

## Blocked

- [ ] Direct Zerodha historical/LTP usage with the current Personal app

Reason:
Current broker app permissions do not yet support the desired market-data flow.

## Done

- [x] Create Python virtual environment
- [x] Implement modular package structure
- [x] Add Zerodha authentication flow
- [x] Add paper-mode execution path
- [x] Add local candle persistence
- [x] Add public market-data fallback for analysis
- [x] Add options math coverage and passing tests
