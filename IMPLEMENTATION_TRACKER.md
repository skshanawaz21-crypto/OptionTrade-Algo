# OptionTrader Missing Features Tracker

This tracker is the single source of truth for closing the feature gaps identified on April 23, 2026.

Status legend:
- `TODO`: not started
- `IN_PROGRESS`: currently being implemented
- `DONE`: implemented and verified
- `BLOCKED`: waiting on prerequisite or decision

## Execution Plan

1. Foundation updates: config model expansion and data model readiness.
2. Option chain and contract selection engine.
3. Risk and session gating enforcement.
4. Engine exits, journaling, and persistence hardening.
5. Dashboard controls and signal-quality analytics.
6. Test coverage and README updates.

## Tracker Items

| ID | Item | Status | Depends On | Acceptance Criteria |
|---|---|---|---|---|
| T01 | Expand config schema for contract policy, risk policy, session rules | DONE | - | New config fields load/validate without breaking current config defaults |
| T02 | Add option chain ingestion module (expiry/strike/bid/ask/LTP/OI/volume/IV/delta) | DONE | T01 | Engine can fetch normalized option chain rows for each underlying |
| T03 | Implement contract selection policy (weekly/monthly, DTE range, ATM/OTM/ITM offsets, min OI/volume, max spread%) | DONE | T01, T02 | Selected contract always satisfies policy filters or no-trade reason is logged |
| T04 | Add risk gates: max premium per trade, max exposure by underlying, spread/slippage thresholds | DONE | T01, T03 | Entries blocked with explicit reason when any gate fails |
| T05 | Add session rules: trading window, no-new-trade cutoff, expiry-day behavior, event blackout windows | DONE | T01 | Engine respects all session constraints for new trades |
| T06 | Add immediate manual exit path (engine + dashboard API/UI) | DONE | - | User can force-close any active position from dashboard |
| T07 | Add trade journal CSV (alongside JSON persistence) | TODO | - | Every open/close event written to append-only CSV journal |
| T08 | Add signal quality analytics + filters (today / last3d / all-time) | TODO | T07 | Dashboard shows metrics by selected range |
| T09 | Harden process handling to avoid orphan engine processes | DONE | - | Dashboard shutdown/stop leaves no stray engine process |
| T10 | Improve broker auth/token failure handling with safe fallbacks and clear state reporting | DONE | - | Graceful degradation in paper mode, explicit errors in live mode |
| T11 | Add tests: contract selection, risk gating, manual exit path | TODO | T03, T04, T06 | Automated tests pass and cover required scenarios |
| T12 | Update README with paper-first workflow and new operational controls | TODO | T01-T11 | README reflects end-to-end usage and safety behavior |
| T13 | Add NIFTY250 2-minute scanner strategy with scoring, ATM option entry, and candle-length-based SL/target | DONE | T01-T06 | Scanner runs on NIFTY250, scores signals, and executes ATM option with target=1x candle length and SL=0.5x candle length |
| T14 | Improve active position readability + reject micro-candle scanner entries | DONE | T13 | Active position symbol/contract text is readable on light theme and scanner skips tiny candles that create impractical 1-paise targets |
| T15 | Make engulfing strategy explicitly visible in dashboard UI and set scanner defaults to NIFTY250/2m | DONE | T13 | Dashboard shows dedicated engulfing scanner section, clear labels, and default 2m/NIFTY250 controls |
| T16 | Fix critical execution logic defects found in audit (scanner API resilience, manual-exit targeting, timezone/session handling, state continuity, futures charge mapping) | DONE | T05, T06, T13 | Engine and dashboard avoid false failures and mismatched exits, preserve restart continuity, and apply correct charge path for futures |
| T17 | Remove fake option LTP fallback, widen scanner SL/target floors, add multi-interval engulfing responses, and fix candle timestamp handling | DONE | T13, T16 | Dashboard no longer shows synthetic option prices, scanner returns responsive actionable/watch rows, candle progress uses normalized timestamps, and New Session clears stale paper state |
| T18 | Split broker login health from quote/LTP permission health | DONE | T10, T17 | Dashboard no longer shows green Broker OK when profile works but Kite quote/LTP permission is blocked |
| T19 | Add optional FYERS market-data fallback for paper option quotes | DONE | T10, T17 | OptionTrader can keep Zerodha-first contract discovery while using FYERS quotes/LTP when configured, and skips option entries when no real quote is available |
| T20 | Add Kite-powered one-second live index/position ticks and historical-data normalization | DONE | T17, T18 | Engine panel shows NIFTY 50, NIFTY BANK, and SENSEX with LTP/change/change%, live LTP refreshes every second, and Kite historical candles are preferred/normalized when available |
| T21 | Fix no-entry blockers from stock-option DTE/expiry mismatch and scanner candle-size floor | DONE | T13, T20 | Stock watchlist items use stock-option routing, monthly stock-option contracts survive selection while index weekly policy remains, and scanner can act on >=0.10% engulfing candles without micro SL/target |
| T22 | Stabilize Kite health/tick refresh and IST candle display | DONE | T20 | Slow Kite profile/quote calls do not falsely degrade the dashboard, last good index ticks are retained during temporary timeouts, and Kite candle timestamps display in IST |
| T23 | Keep dashboard candle progress in sync with Kite historical analysis data | DONE | T20, T22 | Successful Kite historical fetches update the local candle cache used by the dashboard, so Candle Progress reflects the same candles the engine analyzed |
| T24 | Ensure daily watchlist symbols fetch enough candles | DONE | T23 | Daily interval symbols request enough calendar lookback to satisfy the minimum candle count instead of staying stuck below readiness |
| T25 | Manage scanner-created open positions outside the watchlist | DONE | T13, T24 | Every open paper trade is checked for SL/target on each engine cycle and during the sleep loop, even when the underlying is not in the configured watchlist |
| T26 | Add browser-based Zerodha token refresh workflow | DONE | T10, T25 | Dashboard shows token active/expired state, provides a Zerodha login link, accepts redirected URL/request_token, persists the refreshed token, and restarts the paper engine safely when needed |
| T27 | Add session square-off so paper option trades cannot carry forward accidentally | DONE | T25 | Open paper trades are force-closed at configured square-off time using live quotes, with retry on temporary quote failure |
| T28 | Explain risk-manager no-entry blocks in logs | DONE | T04 | Watchlist and scanner logs include the exact risk gate that blocked a new entry |
| T29 | Tune VaR entry gate for OptionTrader scanner | DONE | T28 | `max_var_pct` raised to 4.0 so normal intraday volatility does not block every scanner/watchlist entry |
| T30 | Wire profit-protection trailing SL/target management | DONE | T25 | Open trades move SL to breakeven/profit-lock stages and can extend targets based on favorable progress |
| T31 | Populate Strategy Leaderboard with real performance groups | DONE | T07, T30 | Dashboard ranks strategy buckets by net P&L, win rate, profit factor, live entries, and best/worst symbols |
| T32 | Normalize dashboard and paper-state timestamps to IST | DONE | T06, T08 | Dashboard displays open/closed trade times in IST, date filters use IST, and new paper-state timestamps are saved with IST timezone |
| T33 | Make dashboard refresh, manual exit, and token refresh responsive | DONE | T26, T32 | Full status refresh avoids synchronous quote checks, manual exit returns after queueing command, and token refresh avoids false error alerts after successful login |
| T34 | Suppress stale token-refresh failure alerts when token is already active | DONE | T26, T33 | If a token submit errors after a successful/active token state, the UI verifies token status and shows an informational message instead of a false failure popup |
| T35 | Stop routine dashboard refresh from showing token-check failure states | DONE | T26, T33 | Routine 3-second status refresh shows saved token as present unless a real broker validation marks it invalid |
| T36 | Improve Strategy Leaderboard readability on light theme | DONE | T31 | Leaderboard cards use light high-contrast styling with readable metric text and clear score badges |
| T37 | Add separate index-options scanner for NIFTY, BANKNIFTY, and SENSEX | DONE | T20, T30 | Engine scans the three indexes independently of existing strategies, maps bullish/bearish signals to CE/PE, applies existing risk/session gates, and records trades under a separate leaderboard bucket |
| T38 | Add dashboard visibility for index scanner and underlying LTP on active trades | DONE | T37 | Dashboard shows per-index regime/score/CE-PE bias/readiness and active option positions show underlying LTP plus today change/change% |
| T39 | Stop active-trade dashboard refresh from wiping live LTP values | DONE | T38 | Full status refresh reuses cached live option/underlying quotes and browser rendering preserves last live values instead of flashing back to stale state |
| T40 | Fix index scanner momentum normalization to prevent false no-signal suppression | DONE | T37, T38 | Index scanner now treats momentum as pct-change correctly in engine and dashboard, so score/readiness reflect real momentum instead of near-zero values |
| T41 | Split open-position risk pools for stock options vs index options | DONE | T40 | Added separate stock-option and index-option max-open-position gates (2 each) with global cap 4 so index scanner is not blocked by stock-option slots alone |
| T42 | Add exact index scanner no-entry diagnostics and fix zero-looking dashboard values | DONE | T40 | Dashboard now shows computed score/momentum/EMA-gap diagnostics for every index and engine logs exact no-entry reasons instead of generic zero-looking states |
| T43 | Allow BANKNIFTY index scanner to use monthly option expiry | DONE | T37, T42 | Index scanner can override contract-selection expiry/DTE per index, with BANKNIFTY using monthly options while NIFTY/SENSEX keep the global weekly policy |
| T44 | Add exact position-sizing skip reason for index scanner entries | DONE | T43 | Index scanner logs now include entry, stop, lot size, and the exact risk/capital reason when a ready signal resolves to zero quantity |
| T45 | Make max_daily_loss enforce actual IST daily realized P&L | DONE | T28, T44 | New-entry risk checks now compare max_daily_loss against today's closed-trade P&L instead of cumulative account P&L, so a bad day stops new entries even if prior days were profitable |
| T46 | Throttle Zerodha broker/token validation to once per hour | DONE | T22, T35 | Fast dashboard and live-tick refreshes reuse cached broker health for one hour, token refresh clears the cache immediately, and hourly validation uses one Kite LTP request without a redundant profile call |
| T47 | Prevent index scanner entries from incomplete intraday candles | DONE | T37, T42 | Engine and dashboard discard the still-forming intraday candle before index signal analysis, so breakout, RSI, momentum, and EMA diagnostics use only closed candles |
| T48 | Resume paper entries with a 10% portfolio drawdown ceiling | DONE | T47 | Preserve paper account history while raising `max_portfolio_drawdown_pct` from 8% to 10%, allowing entries below the new ceiling while all daily, strategy, exposure, and position gates remain active |
| T49 | Make the portfolio drawdown gate optional | DONE | T48 | A configured `max_portfolio_drawdown_pct` of `0` disables cumulative drawdown entry blocking while retaining daily-loss, stop-loss, exposure, and position controls |
| T50 | Add safe sharing package workflow | DONE | T49 | A repeatable package command creates a clean zip that preserves strategy/config code while excluding local credentials, tokens, paper state, logs, archived logs, and candle cache data |
| T51 | Publish share-safe OptionTrader snapshot to GitHub | DONE | T50 | Initialized the local Git repository, committed only share-safe tracked files, configured GitHub remote, and pushed `main` to `skshanawaz21-crypto/OptionTrade-Algo` |
| T52 | Document GitHub clone and VS Code startup workflow | DONE | T51 | README now gives clone/setup/run instructions for new users and the repo includes VS Code launch/settings files for dashboard and unittest execution |
| T53 | Add Desktop launcher shortcut for OptionTrader dashboard | DONE | T52 | Added a PowerShell launcher that starts only `C:\OptionTrader`, waits for dashboard readiness, opens `http://127.0.0.1:8877/`, and created a Desktop shortcut to run it |
| T54 | Create cloud paper platform blueprint and UI spec | DONE | T53 | Added durable Phase 1 cloud paper trading architecture and Figma-ready UI planning docs covering hosting, market data constraints, multi-user database design, strategy rollout, and user/admin screens |
| T55 | Add self-hosted private-beta cloud path | DONE | T54 | Extended the cloud paper blueprint with an always-on-machine Phase 1A path using Cloudflare Tunnel/Access, local PostgreSQL/Redis, central market-data cache guidance, 5-20 user capacity expectations, and data-licensing cautions |
| T56 | Add self-hosted beta launcher and Cloudflare Access guard | DONE | T55 | Added an optional dashboard Cloudflare Access header guard, tests, a self-hosted launcher, a separate OptionTrader Cloudflare config generator/template, and a runbook without modifying `C:\AlgoTrader` |
| T57 | Generate local OptionTrader Cloudflare tunnel config | DONE | T56 | Created `%USERPROFILE%\.cloudflared\optiontrader.yml` for the existing Cloudflare tunnel/hostname to route to OptionTrader port `8877`, leaving the original AlgoTrader config on port `8765` unchanged |
| T58 | Add database-backed paper foundation | DONE | T57 | Added SQLite-backed users, paper accounts, strategy definitions/settings, paper state mirroring, normalized open/closed paper trade tables, strategy toggle enforcement, migration tooling, central quote cache table, dashboard APIs, and regression tests |

## Change Log

| Date | Update |
|---|---|
| 2026-04-23 | Tracker created with implementation plan and itemized backlog (T01-T12). |
| 2026-04-23 | T01 completed: added contract selection + session rule schema, added validation, updated example config, and verified backward compatibility. |
| 2026-04-23 | T02 completed: added normalized option chain model, broker option-chain ingestion, and option-chain service layer. |
| 2026-04-23 | T03 completed: added contract selector with expiry type, DTE, strike mode/offset, and side decision policy. |
| 2026-04-23 | T04 completed: added entry risk gates for premium cap, per-underlying exposure, spread, and slippage thresholds. |
| 2026-04-23 | T05 completed: added session guard for trading window, no-new-trade cutoff, blackout windows, and expiry-day policy. |
| 2026-04-23 | T06 completed: added manual exit command path, engine command processing, dashboard API endpoint, and UI action button. |
| 2026-04-30 | T13 completed: added NIFTY250 2-minute pattern scanner strategy, scoring, ATM option execution, and candle-length-derived SL/target rules. |
| 2026-04-30 | T14 completed: boosted active-position contrast (dark text) and added `scanner_2m_nifty250.min_candle_length_pct` guard to skip tiny-candle entries. |
| 2026-04-30 | T15 completed: renamed scanner section to engulfing strategy, added explicit strategy subtitle, and set default controls to NIFTY250 + 2m. |
| 2026-05-04 | T16 completed: fixed scanner endpoint error handling, manual-exit tradingsymbol routing, session timezone handling, restart-state continuity, and futures charge mapping. |
| 2026-05-05 | T17 completed: disabled inaccurate option quote fallback, made active LTP quote status explicit, added fast multi-interval engulfing scanner responses, widened scanner option SL/target floors, fixed candle timestamp normalization, and made New Session archive/clear paper state. |
| 2026-05-05 | T18 completed: broker health now verifies a real Kite LTP call after profile auth, and the dashboard labels quote permission failures as `QUOTES BLOCKED` instead of `OK`. |
| 2026-05-05 | T09 completed: dashboard stop now terminates the Windows process tree, dashboard shutdown attempts a clean engine stop, and engine processes self-exit if their dashboard parent disappears. |
| 2026-05-05 | T19/T10 completed: added FYERS quote fallback adapter, broker router, FYERS token helper, dashboard `FYERS DATA` health state, tests for routing, and removed remaining fake option-entry fallback so missing option quotes block new paper entries safely. |
| 2026-05-06 | T20 completed: added top-of-engine Kite quote tiles for NIFTY 50, NIFTY BANK, and SENSEX with LTP/change/change% color coding, added `/api/live-ticks` for one-second UI refreshes, reduced active position LTP cache to sub-second, and normalized Kite historical candle timestamps. |
| 2026-05-06 | T21 completed: fixed saved stock watchlist instrument types, allowed monthly stock-option contracts beyond the index weekly DTE window, lowered scanner candle-size floor to 0.10% now that option premium SL/target floors prevent 1-paise trades, and added regression coverage. |
| 2026-05-06 | T22 completed: made broker health quote-first, increased Kite timeout tolerance, kept the previous good index tick during temporary quote failures, improved empty timeout error messages, and converted Kite candle timestamps to IST for dashboard display. |
| 2026-05-06 | T23 completed: engine now persists successful Kite historical candles into the local candle cache, which fixes stale Candle Progress counts/timestamps and aligns the dashboard with the strategy's actual analysis source. |
| 2026-05-06 | T24 completed: daily-interval watchlist items now fetch at least three times the minimum candle count in calendar days, preventing 20-candle strategies from getting stuck with only about 14 trading sessions. |
| 2026-05-06 | T25 completed: fixed scanner-created trades not exiting when their symbols were absent from the watchlist, added periodic open-position management during the engine sleep loop, and added regression coverage using the KPITTECH target-hit scenario. |
| 2026-05-08 | T26 completed: added a top-right Zerodha token control with active/expired state, login URL, request-token paste flow, persistent token update, cache reset, safe paper-engine restart, and token parsing tests. |
| 2026-05-11 | T27/T28 completed: added configurable session square-off at 15:18 IST to prevent accidental carry-forward, added exact risk-block reasons for scanner/watchlist entries, and verified with regression tests. |
| 2026-05-12 | T29 completed: raised OptionTrader `max_var_pct` from 2.0 to 4.0 after logs showed all entries blocked by 2.84%-3.26% VaR during normal market hours. |
| 2026-05-12 | T30 completed: wired profit-protection into active trade management, explicitly configured 30/50/70 progress rules, and added TCS-style regression coverage for moving SL to breakeven. |
| 2026-05-12 | T31 completed: replaced the empty Strategy Leaderboard placeholder with real closed/open trade grouping, added entry-reason persistence for future trades, and verified dashboard rows are generated. |
| 2026-05-14 | T32 completed: converted legacy naive paper timestamps from UTC to IST for display/filtering, switched new open/close/saved timestamps to IST, and made engine logs explicitly emit IST. |
| 2026-05-14 | T33 completed: changed `/api/status` to use cached/state prices instead of blocking on live quotes, made manual exit return as soon as the command is queued, and made token refresh return success without waiting on secondary validation. |
| 2026-05-14 | T34 completed: token-submit errors now re-check `/api/token-status`; if the token is already active, the dashboard suppresses the false failure alert and keeps the token active state visible. |
| 2026-05-14 | T35 completed: routine `/api/status` no longer maps quick broker-health `checking` to `Token Check Failed`; explicit `/api/token-status` still validates live token health. |
| 2026-05-14 | T36 completed: changed Strategy Leaderboard from dark low-contrast cards to light readable cards with stronger typography and score badges. |
| 2026-05-14 | T37 completed: added an independent index-options scanner for NIFTY, BANKNIFTY, and SENSEX with CE/PE signal scoring, NFO/BFO contract routing, shared paper-risk execution, dashboard leaderboard grouping, config parsing, and regression tests. |
| 2026-05-14 | T38 completed: added a dedicated Index Options Scanner dashboard box with per-index status/score/side diagnostics and added underlying LTP/change/change% to active paper option cards. |
| 2026-05-15 | T39 completed: fixed active-position quote flicker by preventing `/api/status` from overwriting live option/underlying LTP with stale paper-state values and added a browser-side last-live-value guard. |
| 2026-05-15 | T40 completed: fixed index scanner momentum normalization in engine/dashboard (removed double division by close on an already pct-change momentum field), updated regression inputs, and restored realistic scanner score/no-entry behavior. |
| 2026-05-15 | T41 completed: split open-position gates by instrument pool (`max_stock_option_positions`, `max_index_option_positions`), switched strategy pre-entry checks to instrument-aware gate evaluation, and updated runtime config to 4 total open positions with 2-per-pool limits. |
| 2026-05-15 | T42 completed: changed the Index Options Scanner dashboard to show computed no-entry diagnostics (score, momentum%, EMA gap%, side bias, and exact failed gates), fixed null numeric rendering that could show misleading `0.0`, and updated engine no-signal logs with detailed gate reasons. |
| 2026-05-15 | T43 completed: added per-index contract policy overrides for the Index Options Scanner and configured BANKNIFTY to select monthly contracts with a wider DTE window after logs showed an entry-ready BANKNIFTY CE signal blocked by the global weekly-expiry policy. |
| 2026-05-15 | T44 completed: added exact index-scanner position-sizing skip logs so ready signals that cannot form one lot explain whether the blocker is risk budget, capital, entry/stop distance, or lot value. |
| 2026-05-21 | T45 completed: fixed the daily-loss risk gate to use realized P&L from trades closed today in IST instead of cumulative account P&L, and added regression coverage for a day that breaches max_daily_loss despite prior profits. |
| 2026-06-08 | T46 completed: increased broker-health/token validation cache from 30 seconds to one hour, removed the redundant Kite profile request, retained one-second live quote refreshes, and added hourly-cache regression tests. |
| 2026-06-11 | T47 completed: fixed the Index Options Scanner evaluating still-forming 5-minute candles, aligned dashboard diagnostics with completed candles, and added boundary regression tests for candle close time. |
| 2026-06-12 | T48 completed: raised the paper portfolio drawdown ceiling from 8% to 10% at the user's explicit request, preserving all state and retaining the remaining risk controls. |
| 2026-06-12 | T49 completed: disabled the cumulative portfolio drawdown entry gate at the user's explicit request by defining `0%` as disabled; daily and trade-level safeguards remain active. |
| 2026-07-02 | T50 completed: added `tools/prepare_share_package.py`, share-safe ignore rules, and `SHARING.md` so OptionTrader can be shared without exposing local credentials, tokens, paper state, logs, or cached market data. |
| 2026-07-02 | T51 completed: initialized Git for OptionTrader, committed the share-safe project snapshot, configured the GitHub remote, and pushed `main` to `skshanawaz21-crypto/OptionTrade-Algo`. |
| 2026-07-02 | T52 completed: expanded README with GitHub clone, Zerodha setup, fixed dashboard URL, terminal run, and VS Code run/debug instructions; added VS Code launch/settings files. |
| 2026-07-06 | T53 completed: added a local PowerShell launcher and Desktop shortcut for starting the OptionTrader dashboard and opening `http://127.0.0.1:8877/`. |
| 2026-07-09 | T54 completed: added Phase 1 cloud paper platform blueprint and UI spec docs, including cPanel/Cloudflare/VPS deployment guidance, market-data licensing constraints, database schema, strategy rollout model, and Figma-ready screen plans. |
| 2026-07-09 | T55 completed: added the self-hosted private-beta path to the cloud paper blueprint, covering Cloudflare Tunnel/Access routing, local database/cache services, 5-20 user assumptions, and the distinction between API-limit caching and market-data redistribution rights. |
| 2026-07-09 | T56 completed: implemented the first self-hosted beta foundation with optional Cloudflare Access enforcement, PowerShell startup/config scripts, a separate OptionTrader tunnel template, runbook docs, and guard regression tests. |
| 2026-07-09 | T57 completed: generated the local OptionTrader-specific Cloudflare tunnel config under the Windows user profile and validated that it routes the existing hostname to `http://127.0.0.1:8877` without changing the original AlgoTrader tunnel config. |
| 2026-07-09 | T58 completed: added the database-backed paper foundation with default local user/account, JSON-to-DB mirroring/migration, per-account strategy settings honored by the engine, central quote cache storage, dashboard APIs, and DB runbook/tests. |

## Working Rules

- Only mark an item `DONE` after implementation + basic verification.
- Every completion updates:
  - this tracker status row
  - this tracker change log
  - related docs/tests where applicable
- If scope changes, add a new item ID instead of rewriting history.
