# Changelog

All notable project-level changes should be recorded here.

## 2026-04-22

### Added

- Core project tracking files:
  - `PROJECT_STATUS.md`
  - `ROADMAP.md`
  - `NEXT_STEPS.md`
  - `DECISIONS.md`
- Project operations docs:
  - `TASKS.md`
  - `docs/README.md`
  - `docs/strategy-spec.md`
- Local dashboard entry point:
  - `run_dashboard.py`
  - `algotrader/dashboard.py`

### Notes

- Foundation work is in place for paper/live trading modes, Zerodha
  integration, local candle persistence, and project planning.
- Strategy-specific trading rules are still pending definition.
- Paper-trading test config now uses `1minute` candles with a shorter analysis
  warm-up to make debugging and observation faster.
