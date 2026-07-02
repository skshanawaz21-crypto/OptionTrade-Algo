from __future__ import annotations

import json
import os
import time
import ctypes
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from algotrader.brokers.factory import create_broker
from algotrader.charges import equity_intraday_charges, option_buy_charges
from algotrader.contract_selector import ContractSelector
from algotrader.config import AppSettings, ContractSelectionConfig, StrategyConfig, WatchItem
from algotrader.logger import setup_logger
from algotrader.marketdata import LocalCandleStore, completed_intraday_candles
from algotrader.models import ClosedTrade, OpenTrade, Signal, now_ist
from algotrader.nifty250_strategy import scan_nifty250_2m
from algotrader.option_chain import OptionChainService
from algotrader.risk import RiskManager
from algotrader.session import SessionGuard
from algotrader.strategy import analyze_market, build_signal, should_exit_trade


class TradingEngine:
    def __init__(
        self,
        settings: AppSettings,
        strategy_config: StrategyConfig,
        execution_mode: str,
    ) -> None:
        self.settings = settings
        self.strategy_config = strategy_config
        self.execution_mode = execution_mode
        self.logger = setup_logger(settings.log_level)
        self.risk_manager = RiskManager(
            risk_config=strategy_config.risk,
            capital=strategy_config.capital or settings.capital,
        )
        self.broker = create_broker(settings)
        self.local_candle_store = LocalCandleStore(Path("data") / "candles")
        self.state_path = Path("data") / "paper_state.json"
        self.command_path = Path("data") / "engine_commands.jsonl"
        self.open_trades: dict[str, OpenTrade] = {}
        self.closed_trades: list[ClosedTrade] = []
        self.option_chain_service = OptionChainService(self.broker)
        self.contract_selector = ContractSelector()
        self.session_guard = SessionGuard(strategy_config.session_rules)
        self.use_historical_api = self.broker.supports_historical_data()
        self.dashboard_parent_pid = self._dashboard_parent_pid()
        self._last_open_trade_check: datetime | None = None
        self._open_trade_check_seconds = 5
        self._load_state()

    def run_forever(self) -> None:
        data_mode = "historical-api" if self.use_historical_api else "local-candles"
        self.logger.info("Starting OptionTrader in %s mode using %s", self.execution_mode, data_mode)
        while self._dashboard_parent_alive():
            self.run_once()
            sleep_remaining = max(1, int(self.strategy_config.poll_seconds))
            while sleep_remaining > 0 and self._dashboard_parent_alive():
                step = min(1, sleep_remaining)
                time.sleep(step)
                self._process_commands()
                self._manage_open_trades_if_due()
                self._daily_loss_kill_switch_if_needed()
                self._square_off_open_trades_if_due()
                sleep_remaining -= step
        if self.dashboard_parent_pid:
            self.logger.warning(
                "Dashboard parent PID %s is no longer alive; stopping engine to avoid an orphan process",
                self.dashboard_parent_pid,
            )

    @staticmethod
    def _dashboard_parent_pid() -> int | None:
        raw_pid = os.getenv("OPTIONTRADER_DASHBOARD_PID", "").strip()
        if not raw_pid:
            return None
        try:
            pid = int(raw_pid)
        except ValueError:
            return None
        return pid if pid > 0 else None

    def _dashboard_parent_alive(self) -> bool:
        if not self.dashboard_parent_pid:
            return True
        if os.name == "nt":
            return self._windows_process_exists(self.dashboard_parent_pid)
        try:
            os.kill(self.dashboard_parent_pid, 0)
        except OSError:
            return False
        return True

    @staticmethod
    def _windows_process_exists(pid: int) -> bool:
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True

    def run_once(self) -> None:
        self.logger.info("Running evaluation cycle")
        self._process_commands()
        self._manage_all_open_trades(force=True)
        if self._daily_loss_kill_switch_if_needed():
            return
        self._square_off_open_trades_if_due()
        if self._scanner_enabled():
            self._run_nifty250_scanner_strategy()
        if self._index_options_scanner_enabled():
            self._run_index_options_scanner_strategy()
        for item in self.strategy_config.watchlist:
            if not item.enabled:
                continue
            try:
                self._evaluate_symbol(item)
            except Exception as exc:
                self.logger.exception("Failed on %s: %s", item.symbol, exc)

    def _evaluate_symbol(self, item: WatchItem) -> None:
        to_dt = datetime.now()
        interval = item.interval or self.strategy_config.default_interval
        df = self._get_analysis_data(item, interval, to_dt)
        if df.empty:
            self.logger.warning("No data for %s", item.symbol)
            return
        if len(df) < self.strategy_config.min_candles_for_analysis:
            self.logger.info(
                "Waiting for enough local history on %s before analysis: %s/%s candles",
                item.symbol,
                len(df),
                self.strategy_config.min_candles_for_analysis,
            )
            return

        snapshot = analyze_market(df, item)
        self.risk_manager.register_price_change(snapshot.previous_close, snapshot.close)
        self.logger.info(
            "%s regime=%s close=%.2f ema20=%.2f ema50=%.2f rsi=%.2f atr=%.2f drawdown=%.2f%% var=%.2f%%",
            item.symbol,
            snapshot.regime,
            snapshot.close,
            snapshot.ema20,
            snapshot.ema50,
            snapshot.rsi14,
            snapshot.atr14,
            self.risk_manager.current_drawdown_pct(),
            self.risk_manager.last_var_pct,
        )

        if self.open_trades.get(self._trade_key(item.symbol, item.instrument_type)):
            return

        session_decision = self.session_guard.can_open_new_positions(datetime.now())
        if not session_decision.allowed:
            self.logger.info("Session gate blocks %s: %s", item.symbol, session_decision.reason)
            return

        risk_block_reason = self._new_position_block_reason(item.instrument_type)
        if risk_block_reason:
            self.logger.warning("Risk limits block new trades for %s: %s", item.symbol, risk_block_reason)
            return

        contract = self._resolve_contract(item, snapshot.close, snapshot.regime)
        if contract.get("rejected_reason"):
            self.logger.info("Contract selection blocked for %s: %s", item.symbol, contract["rejected_reason"])
            return

        expiry_decision = self.session_guard.can_trade_contract_expiry(str(contract.get("expiry", "")))
        if not expiry_decision.allowed:
            self.logger.info("Session expiry policy blocks %s: %s", item.symbol, expiry_decision.reason)
            return

        signal = build_signal(
            snapshot=snapshot,
            item=item,
            risk_config=self.strategy_config.risk,
            risk_manager=self.risk_manager,
            contract=contract,
            available_capital=self._available_capital(),
        )
        if not signal:
            self.logger.info("No signal for %s", item.symbol)
            return

        risk_gate_reason = self._entry_risk_gate_reason(signal)
        if risk_gate_reason:
            self.logger.warning("Risk gate blocked %s: %s", item.symbol, risk_gate_reason)
            return

        self._execute_entry(signal)

    def _get_analysis_data(self, item: WatchItem, interval: str, timestamp: datetime):
        if self.use_historical_api:
            from_dt = timestamp - timedelta(days=self._history_lookback_days(interval))
            try:
                historical = self.broker.get_historical_data(
                    item.exchange,
                    item.symbol,
                    interval,
                    from_dt,
                    timestamp,
                )
                if not historical.empty:
                    candle_path = self.local_candle_store._file_path(
                        item.exchange,
                        item.symbol,
                        interval,
                    )
                    candle_path.parent.mkdir(parents=True, exist_ok=True)
                    historical.to_csv(candle_path, index=False)
                return historical
            except Exception as exc:
                self.logger.warning(
                    "Historical API failed for %s, falling back to local candles: %s",
                    item.symbol,
                    exc,
                )
                self.use_historical_api = False

        price = self.broker.get_ltp(item.exchange, item.symbol)
        snapshot = self.local_candle_store.record_price(
            exchange=item.exchange,
            symbol=item.symbol,
            interval=interval,
            price=price,
            timestamp=timestamp,
        )
        candles = snapshot.candles
        required_rows = self.strategy_config.min_candles_for_analysis
        if len(candles) < required_rows:
            self.logger.info(
                "Building local candle history for %s: %s/%s candles collected",
                item.symbol,
                len(candles),
                required_rows,
            )
        try:
            public_candles = self.broker.get_public_historical_data(item.symbol, interval)
            if not public_candles.empty:
                candle_path = self.local_candle_store._file_path(item.exchange, item.symbol, interval)
                public_candles.to_csv(candle_path, index=False)
                return public_candles
        except Exception as exc:
            self.logger.info("Public historical fallback unavailable for %s: %s", item.symbol, exc)
        return candles

    def _history_lookback_days(self, interval: str) -> int:
        configured = int(self.strategy_config.history_lookback_days)
        normalized = interval.strip().lower().replace("_", "").replace("-", "")
        if normalized in {"day", "1day", "1d", "daily"}:
            # Calendar days include weekends/holidays; daily strategies need enough trading rows.
            return max(configured, int(self.strategy_config.min_candles_for_analysis * 3))
        return configured

    def _manage_open_trade(self, trade: OpenTrade) -> None:
        try:
            price = self.broker.get_ltp(trade.exchange, trade.tradingsymbol)
        except Exception as exc:
            self.logger.warning("Quote unavailable for active trade %s: %s", trade.tradingsymbol, exc)
            return
        trade.current_price = price
        self._apply_profit_protection(trade, price)
        decision = should_exit_trade(
            direction=trade.direction,
            price=price,
            stop_loss=trade.stop_loss,
            target_price=trade.target_price,
        )
        if not decision.should_exit:
            self.logger.info(
                "Holding %s at %.2f | stop %.2f | target %.2f",
                trade.tradingsymbol,
                price,
                trade.stop_loss,
                trade.target_price,
            )
            return

        self._close_trade(trade, price=price, reason=decision.reason)

    def _apply_profit_protection(self, trade: OpenTrade, price: float) -> None:
        if not hasattr(self, "strategy_config"):
            return
        risk = self.strategy_config.risk
        if not getattr(risk, "profit_protection_enabled", True):
            return
        initial_stop = trade.initial_stop_loss if trade.initial_stop_loss is not None else trade.stop_loss
        initial_target = trade.initial_target_price if trade.initial_target_price is not None else trade.target_price
        reward = abs(initial_target - trade.entry_price)
        if reward <= 0:
            return

        if trade.direction == "BUY":
            favourable_price = max(trade.max_favourable_price or trade.entry_price, price)
            progress_pct = ((favourable_price - trade.entry_price) / reward) * 100.0
            breakeven_stop = trade.entry_price * (1.0 + risk.breakeven_buffer_pct / 100.0)
            lock_stop = trade.entry_price + (reward * risk.lock_profit_pct / 100.0)
            extension_stop = trade.entry_price + (reward * risk.target_extension_stop_lock_pct / 100.0)
            next_target = trade.target_price + (reward * risk.target_extension_pct / 100.0)
            should_raise_stop = lambda proposed: proposed > trade.stop_loss
            should_extend_target = lambda proposed: proposed > trade.target_price
        else:
            favourable_price = min(trade.max_favourable_price or trade.entry_price, price)
            progress_pct = ((trade.entry_price - favourable_price) / reward) * 100.0
            breakeven_stop = trade.entry_price * (1.0 - risk.breakeven_buffer_pct / 100.0)
            lock_stop = trade.entry_price - (reward * risk.lock_profit_pct / 100.0)
            extension_stop = trade.entry_price - (reward * risk.target_extension_stop_lock_pct / 100.0)
            next_target = trade.target_price - (reward * risk.target_extension_pct / 100.0)
            should_raise_stop = lambda proposed: proposed < trade.stop_loss
            should_extend_target = lambda proposed: proposed < trade.target_price

        changed = False
        trade.max_favourable_price = favourable_price

        if progress_pct >= risk.breakeven_trigger_pct and should_raise_stop(breakeven_stop):
            old_stop = trade.stop_loss
            trade.stop_loss = round(breakeven_stop, 2)
            changed = True
            self.logger.info(
                "Profit protection moved %s stop %.2f -> %.2f at %.1f%% progress",
                trade.tradingsymbol,
                old_stop,
                trade.stop_loss,
                progress_pct,
            )

        if progress_pct >= risk.lock_profit_trigger_pct and should_raise_stop(lock_stop):
            old_stop = trade.stop_loss
            trade.stop_loss = round(lock_stop, 2)
            changed = True
            self.logger.info(
                "Profit protection locked %s stop %.2f -> %.2f at %.1f%% progress",
                trade.tradingsymbol,
                old_stop,
                trade.stop_loss,
                progress_pct,
            )

        extension_trigger = risk.target_extension_trigger_pct + (trade.target_extension_count * 15.0)
        if (
            progress_pct >= extension_trigger
            and trade.target_extension_count < risk.max_target_extensions
            and should_extend_target(next_target)
        ):
            old_stop = trade.stop_loss
            old_target = trade.target_price
            if should_raise_stop(extension_stop):
                trade.stop_loss = round(extension_stop, 2)
            trade.target_price = round(next_target, 2)
            trade.target_extension_count += 1
            changed = True
            self.logger.info(
                "Profit protection extended %s target %.2f -> %.2f and stop %.2f -> %.2f at %.1f%% progress",
                trade.tradingsymbol,
                old_target,
                trade.target_price,
                old_stop,
                trade.stop_loss,
                progress_pct,
            )

        if changed:
            self._save_state()

    def _manage_open_trades_if_due(self) -> None:
        if not self.open_trades:
            return
        now = datetime.now()
        if (
            self._last_open_trade_check is not None
            and (now - self._last_open_trade_check).total_seconds() < self._open_trade_check_seconds
        ):
            return
        self._manage_all_open_trades()

    def _manage_all_open_trades(self, force: bool = False) -> None:
        if not self.open_trades:
            return
        if force:
            self._last_open_trade_check = None
        for trade in list(self.open_trades.values()):
            if self._trade_key(trade.symbol, trade.instrument_type) not in self.open_trades:
                continue
            self._manage_open_trade(trade)
        self._last_open_trade_check = datetime.now()

    def _daily_loss_kill_switch_if_needed(self) -> bool:
        if not self.open_trades:
            return False
        risk = self.strategy_config.risk
        if not getattr(risk, "square_off_on_daily_loss", False):
            return False
        reason = self._daily_marked_loss_block_reason()
        if not reason:
            return False

        self.logger.warning("Daily-loss kill switch active: %s", reason)
        for trade in list(self.open_trades.values()):
            if self._trade_key(trade.symbol, trade.instrument_type) not in self.open_trades:
                continue
            try:
                price = self.broker.get_ltp(trade.exchange, trade.tradingsymbol)
            except Exception as exc:
                self.logger.warning(
                    "Daily-loss kill switch waiting for quote on %s: %s",
                    trade.tradingsymbol,
                    exc,
                )
                continue
            trade.current_price = price
            self._close_trade(trade, price=price, reason="Daily loss kill switch")
        return True

    def _square_off_open_trades_if_due(self) -> None:
        if not self.open_trades:
            return
        rules = self.strategy_config.session_rules
        if not getattr(rules, "square_off_open_positions", True):
            return
        try:
            tz = ZoneInfo(rules.timezone)
        except Exception:
            tz = ZoneInfo("Asia/Kolkata")
        now = datetime.now(tz)
        square_off_clock = datetime.strptime(rules.square_off_time, "%H:%M").time()
        if now.time() < square_off_clock:
            return

        for trade in list(self.open_trades.values()):
            if self._trade_key(trade.symbol, trade.instrument_type) not in self.open_trades:
                continue
            try:
                price = self.broker.get_ltp(trade.exchange, trade.tradingsymbol)
            except Exception as exc:
                self.logger.warning(
                    "Session square-off waiting for quote on %s: %s",
                    trade.tradingsymbol,
                    exc,
                )
                continue
            trade.current_price = price
            self.logger.warning(
                "Session square-off closing %s at %.2f after %s",
                trade.tradingsymbol,
                price,
                rules.square_off_time,
            )
            self._close_trade(trade, price=price, reason="Session square-off")

    def _resolve_contract(
        self,
        item: WatchItem,
        spot_price: float,
        regime: str,
        policy: ContractSelectionConfig | None = None,
    ) -> dict[str, object]:
        contract_policy = policy or self.strategy_config.contract_selection
        if item.instrument_type == "equity":
            return {
                "tradingsymbol": item.symbol,
                "exchange": item.exchange,
                "entry_price": spot_price,
                "lot_size": 1,
                "option_side": "",
                "expiry": "",
                "strike": None,
                "bid": None,
                "ask": None,
                "spread_pct": None,
            }
        if item.instrument_type in {"index_future", "stock_future"}:
            try:
                details = self.broker.find_future_contract_details(
                    underlying_symbol=item.symbol,
                    contract_exchange=item.contract_exchange,
                    min_dte=contract_policy.min_dte,
                    max_dte=contract_policy.max_dte,
                    expiry_type=contract_policy.expiry_type,
                )
            except Exception as exc:
                return {"rejected_reason": f"Future contract resolution failed: {exc}"}
            details["entry_price"] = self.broker.get_ltp(
                str(details["exchange"]), str(details["tradingsymbol"])
            )
            details["option_side"] = ""
            details["strike"] = None
            details["bid"] = None
            details["ask"] = None
            details["spread_pct"] = None
            return details

        try:
            snapshot = self.option_chain_service.load(
                item=item,
                spot_price=spot_price,
                policy=contract_policy,
            )
            selected = self.contract_selector.select(
                rows=snapshot.rows,
                item=item,
                policy=contract_policy,
                spot_price=spot_price,
                regime=regime,
            )
            if selected.contract is None:
                return {
                    "rejected_reason": selected.rejection_reason,
                }
            contract = selected.contract
            entry_price = contract.ltp
            if entry_price is None and contract.bid is not None and contract.ask is not None:
                entry_price = (contract.bid + contract.ask) / 2.0
            if entry_price is None:
                return {
                    "rejected_reason": (
                        f"No live option quote for {contract.exchange}:{contract.tradingsymbol}"
                    )
                }
            return {
                "tradingsymbol": contract.tradingsymbol,
                "exchange": contract.exchange,
                "strike": contract.strike,
                "expiry": contract.expiry,
                "lot_size": contract.lot_size,
                "option_side": contract.option_side,
                "entry_price": entry_price,
                "bid": contract.bid,
                "ask": contract.ask,
                "spread_pct": contract.spread_pct,
            }
        except Exception as exc:
            self.logger.warning("Option-chain selection failed for %s, using fallback: %s", item.symbol, exc)
            option_side = item.option_side.upper()
            if option_side == "AUTO":
                option_side = "CE" if regime == "bullish" else "PE"
            details = self.broker.find_option_contract_details(
                underlying_symbol=item.symbol,
                spot_price=spot_price,
                option_side=option_side,
                contract_exchange=item.contract_exchange,
                expiry_hint=item.option_expiry_hint,
            )
            try:
                entry_price = self.broker.get_ltp(
                    str(details["exchange"]),
                    str(details["tradingsymbol"]),
                )
            except Exception as quote_exc:
                return {
                    "rejected_reason": (
                        f"Fallback contract quote unavailable for {details.get('exchange')}:"
                        f"{details.get('tradingsymbol')}: {quote_exc}"
                    )
                }
            details["entry_price"] = entry_price
            return details

    def _execute_entry(self, signal: Signal) -> None:
        option_side = self._normalized_option_side(signal.tradingsymbol, signal.option_side)
        self._submit_order(
            exchange=signal.exchange,
            tradingsymbol=signal.tradingsymbol,
            transaction_type=signal.direction,
            quantity=signal.quantity,
        )
        self.open_trades[self._trade_key(signal.symbol, signal.instrument_type)] = OpenTrade(
            symbol=signal.symbol,
            exchange=signal.exchange,
            direction=signal.direction,
            tradingsymbol=signal.tradingsymbol,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            target_price=signal.target_price,
            quantity=signal.quantity,
            instrument_type=signal.instrument_type,
            underlying_symbol=signal.underlying_symbol or signal.symbol,
            underlying_exchange=signal.underlying_exchange or signal.exchange,
            lot_size=signal.lot_size,
            lots=signal.lots,
            option_side=option_side,
            option_expiry=signal.option_expiry,
            option_strike=signal.option_strike,
            option_metrics=signal.option_metrics,
            option_bid=signal.option_bid,
            option_ask=signal.option_ask,
            option_spread_pct=signal.option_spread_pct,
            expected_slippage_pct=signal.expected_slippage_pct,
            entry_reason=signal.reason,
            current_price=signal.entry_price,
            initial_stop_loss=signal.stop_loss,
            initial_target_price=signal.target_price,
            max_favourable_price=signal.entry_price,
        )
        self._save_state()
        self.logger.info(
            "Entered %s %s qty=%s lots=%s entry=%.2f stop=%.2f target=%.2f reason=%s iv=%s delta=%s",
            signal.direction,
            signal.tradingsymbol,
            signal.quantity,
            signal.lots,
            signal.entry_price,
            signal.stop_loss,
            signal.target_price,
            signal.reason,
            f"{signal.option_metrics.implied_volatility:.4f}" if signal.option_metrics and signal.option_metrics.implied_volatility is not None else "n/a",
            f"{signal.option_metrics.delta:.4f}" if signal.option_metrics and signal.option_metrics.delta is not None else "n/a",
        )

    def _entry_risk_gate_reason(self, signal: Signal) -> str:
        trade_value = abs(signal.entry_price * signal.quantity)
        if signal.instrument_type.endswith("_option"):
            min_premium = float(getattr(self.strategy_config.risk, "min_option_premium", 0.0) or 0.0)
            if min_premium > 0 and signal.entry_price < min_premium:
                return (
                    f"Option premium {signal.entry_price:.2f} below "
                    f"min_option_premium {min_premium:.2f}"
                )

        if (
            self.strategy_config.risk.max_premium_per_trade > 0
            and trade_value > self.strategy_config.risk.max_premium_per_trade
        ):
            return (
                f"Trade premium {trade_value:.2f} exceeds "
                f"max_premium_per_trade {self.strategy_config.risk.max_premium_per_trade:.2f}"
            )

        if signal.instrument_type != "equity":
            current_exposure = self._current_underlying_exposure(signal.symbol)
            proposed_exposure = current_exposure + trade_value
            if (
                self.strategy_config.risk.max_exposure_per_underlying > 0
                and proposed_exposure > self.strategy_config.risk.max_exposure_per_underlying
            ):
                return (
                    f"Underlying exposure {proposed_exposure:.2f} exceeds "
                    f"max_exposure_per_underlying {self.strategy_config.risk.max_exposure_per_underlying:.2f}"
                )

            spread_threshold = max(
                self.strategy_config.risk.max_spread_pct,
                self.strategy_config.contract_selection.max_spread_pct,
            )
            if (
                signal.option_spread_pct is not None
                and spread_threshold > 0
                and signal.option_spread_pct > spread_threshold
            ):
                return (
                    f"Spread {signal.option_spread_pct:.2f}% exceeds threshold {spread_threshold:.2f}%"
                )
            if (
                signal.expected_slippage_pct is not None
                and self.strategy_config.risk.max_slippage_pct > 0
                and signal.expected_slippage_pct > self.strategy_config.risk.max_slippage_pct
            ):
                return (
                    f"Expected slippage {signal.expected_slippage_pct:.2f}% exceeds "
                    f"max_slippage_pct {self.strategy_config.risk.max_slippage_pct:.2f}%"
                )
        if signal.instrument_type in {"index_future", "stock_future"}:
            futures_notional = abs(signal.entry_price * signal.quantity)
            if (
                self.strategy_config.risk.max_futures_notional_per_trade > 0
                and futures_notional > self.strategy_config.risk.max_futures_notional_per_trade
            ):
                return (
                    f"Futures notional {futures_notional:.2f} exceeds "
                    f"max_futures_notional_per_trade {self.strategy_config.risk.max_futures_notional_per_trade:.2f}"
                )
            active_futures = [
                trade
                for trade in self.open_trades.values()
                if trade.instrument_type in {"index_future", "stock_future"}
            ]
            if (
                self.strategy_config.risk.max_futures_positions > 0
                and (len(active_futures) + 1) > self.strategy_config.risk.max_futures_positions
            ):
                return (
                    f"Futures positions {(len(active_futures) + 1)} exceed "
                    f"max_futures_positions {self.strategy_config.risk.max_futures_positions}"
                )
            total_open_futures_notional = sum(
                abs(trade.entry_price * trade.quantity) for trade in active_futures
            )
            total_after = total_open_futures_notional + futures_notional
            if (
                self.strategy_config.risk.max_futures_notional_total > 0
                and total_after > self.strategy_config.risk.max_futures_notional_total
            ):
                return (
                    f"Total futures notional {total_after:.2f} exceeds "
                    f"max_futures_notional_total {self.strategy_config.risk.max_futures_notional_total:.2f}"
                )
        strategy_reason = self._strategy_daily_loss_block_reason(signal)
        if strategy_reason:
            return strategy_reason
        cooldown_reason = self._cooldown_block_reason(signal)
        if cooldown_reason:
            return cooldown_reason
        return ""

    def _current_underlying_exposure(self, symbol: str) -> float:
        exposure = 0.0
        for trade in self.open_trades.values():
            if trade.symbol == symbol:
                exposure += trade.capital_used()
        return exposure

    def _close_trade(self, trade: OpenTrade, price: float, reason: str) -> None:
        exit_side = "SELL" if trade.direction == "BUY" else "BUY"
        self._submit_order(
            exchange=trade.exchange,
            tradingsymbol=trade.tradingsymbol,
            transaction_type=exit_side,
            quantity=trade.quantity,
        )
        gross_pnl = (
            (price - trade.entry_price) * trade.quantity
            if trade.direction == "BUY"
            else (trade.entry_price - price) * trade.quantity
        )
        if trade.instrument_type in {"equity", "index_future", "stock_future"}:
            charges = equity_intraday_charges(
                buy_price=trade.entry_price if trade.direction == "BUY" else price,
                sell_price=price if trade.direction == "BUY" else trade.entry_price,
                quantity=trade.quantity,
                exchange=trade.exchange,
            )
        else:
            charges = option_buy_charges(
                buy_price=trade.entry_price,
                sell_price=price,
                quantity=trade.quantity,
                exchange=trade.exchange,
            )
        net_pnl = gross_pnl - charges.total
        self.risk_manager.register_exit(net_pnl)
        self.logger.info(
            "Exited %s | reason=%s | gross_pnl=%.2f | charges=%.2f | net_pnl=%.2f",
            trade.tradingsymbol,
            reason,
            gross_pnl,
            charges.total,
            net_pnl,
        )
        self.closed_trades.append(
            ClosedTrade(
                symbol=trade.symbol,
                exchange=trade.exchange,
                direction=trade.direction,
                tradingsymbol=trade.tradingsymbol,
                entry_price=trade.entry_price,
                exit_price=price,
                stop_loss=trade.stop_loss,
                target_price=trade.target_price,
                quantity=trade.quantity,
                instrument_type=trade.instrument_type,
                underlying_symbol=trade.underlying_symbol,
                underlying_exchange=trade.underlying_exchange,
                lot_size=trade.lot_size,
                lots=trade.lots,
                option_side=self._normalized_option_side(trade.tradingsymbol, trade.option_side),
                option_expiry=trade.option_expiry,
                option_strike=trade.option_strike,
                gross_pnl=gross_pnl,
                charges=charges.to_dict(),
                total_charges=charges.total,
                pnl=net_pnl,
                exit_reason=reason,
                entry_reason=trade.entry_reason,
                opened_at=trade.opened_at,
                closed_at=now_ist(),
            )
        )
        self.open_trades.pop(self._trade_key(trade.symbol, trade.instrument_type), None)
        self._save_state()

    def _process_commands(self) -> None:
        if not self.command_path.exists():
            return
        lines = self.command_path.read_text(encoding="utf-8").splitlines()
        self.command_path.write_text("", encoding="utf-8")
        for line in lines:
            if not line.strip():
                continue
            try:
                command = json.loads(line)
            except json.JSONDecodeError:
                self.logger.warning("Skipping malformed command line: %s", line)
                continue
            action = str(command.get("action", "")).strip()
            if action != "exit_trade":
                self.logger.warning("Unknown engine command: %s", action)
                continue
            symbol = str(command.get("symbol", "")).strip().upper()
            tradingsymbol = str(command.get("tradingsymbol", "")).strip().upper()
            trade = None
            for candidate in self.open_trades.values():
                if tradingsymbol and candidate.tradingsymbol.upper() == tradingsymbol:
                    trade = candidate
                    break
                if symbol and candidate.symbol.upper() == symbol:
                    trade = candidate
                    break
            if trade is None:
                self.logger.info(
                    "Manual exit ignored: no active trade for symbol=%s tradingsymbol=%s",
                    symbol,
                    tradingsymbol,
                )
                continue
            try:
                price = self.broker.get_ltp(trade.exchange, trade.tradingsymbol)
            except Exception as exc:
                self.logger.warning(
                    "Manual exit blocked for %s: live quote unavailable: %s",
                    trade.tradingsymbol,
                    exc,
                )
                continue
            trade.current_price = price
            self._close_trade(trade, price=price, reason="Manual exit")

    def _submit_order(
        self,
        exchange: str,
        tradingsymbol: str,
        transaction_type: str,
        quantity: int,
    ) -> None:
        if self.execution_mode == "paper":
            self.logger.info(
                "[PAPER] %s %s %s x %s",
                transaction_type,
                exchange,
                tradingsymbol,
                quantity,
            )
            return

        self.settings.require_live_credentials()
        self.broker.ensure_authenticated()
        order_id = self.broker.place_market_order(
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=transaction_type,
            quantity=quantity,
        )
        self.logger.info("Live order placed: %s", order_id)

    def _load_state(self) -> None:
        if self.execution_mode != "paper":
            return
        if not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            self.logger.warning("Could not parse paper state file at %s", self.state_path)
            return

        trades = raw.get("open_trades", [])
        for item in trades:
            trade = OpenTrade.from_dict(item)
            self.open_trades[self._trade_key(trade.symbol, trade.instrument_type)] = trade
        self.closed_trades = []
        for item in raw.get("closed_trades", []):
            if "gross_pnl" not in item:
                gross_pnl = float(item["pnl"])
                if item.get("instrument_type", "equity") in {"equity", "index_future", "stock_future"}:
                    charges = equity_intraday_charges(
                        buy_price=float(item["entry_price"]) if item["direction"] == "BUY" else float(item["exit_price"]),
                        sell_price=float(item["exit_price"]) if item["direction"] == "BUY" else float(item["entry_price"]),
                        quantity=int(item["quantity"]),
                        exchange=str(item.get("exchange", "NSE")),
                    )
                else:
                    charges = option_buy_charges(
                        buy_price=float(item["entry_price"]),
                        sell_price=float(item["exit_price"]),
                        quantity=int(item["quantity"]),
                        exchange=str(item.get("exchange", "NFO")),
                    )
                item = dict(item)
                item["gross_pnl"] = gross_pnl
                item["charges"] = charges.to_dict()
                item["total_charges"] = charges.total
                item["pnl"] = gross_pnl - charges.total
            self.closed_trades.append(ClosedTrade.from_dict(item))

        account = raw.get("account", {})
        realized_pnl = account.get("realized_pnl")
        peak_equity = account.get("peak_equity")
        if realized_pnl is not None:
            self.risk_manager.realized_pnl = float(realized_pnl)
        elif self.closed_trades:
            self.risk_manager.realized_pnl = sum(trade.pnl for trade in self.closed_trades)
        if peak_equity is not None:
            self.risk_manager.peak_equity = float(peak_equity)

        if self.open_trades:
            self.logger.info(
                "Restored %s paper trade(s) from %s",
                len(self.open_trades),
                self.state_path,
            )

    def _save_state(self) -> None:
        if self.execution_mode != "paper":
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        capital_committed = sum(trade.capital_used() for trade in self.open_trades.values())
        payload = {
            "saved_at": now_ist().isoformat(),
            "account": {
                "starting_capital": self.risk_manager.capital,
                "realized_pnl": self.risk_manager.realized_pnl,
                "peak_equity": self.risk_manager.peak_equity,
                "capital_committed": capital_committed,
                "available_balance": self.risk_manager.capital + self.risk_manager.realized_pnl - capital_committed,
            },
            "open_trades": [trade.to_dict() for trade in self.open_trades.values()],
            "closed_trades": [trade.to_dict() for trade in self.closed_trades],
        }
        self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _available_capital(self) -> float:
        committed = sum(trade.capital_used() for trade in self.open_trades.values())
        return max(self.risk_manager.capital + self.risk_manager.realized_pnl - committed, 0.0)

    def _daily_realized_pnl(
        self,
        session_date: date | None = None,
        strategy_bucket: str | None = None,
    ) -> float:
        target_date = session_date or now_ist().date()
        total = 0.0
        for trade in getattr(self, "closed_trades", []):
            closed_at = trade.closed_at
            if closed_at.tzinfo is None:
                closed_at = closed_at.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
            if closed_at.astimezone(ZoneInfo("Asia/Kolkata")).date() == target_date:
                if strategy_bucket and self._trade_strategy_bucket(trade) != strategy_bucket:
                    continue
                total += trade.pnl
        return total

    def _open_unrealized_pnl(self) -> float:
        total = 0.0
        for trade in self.open_trades.values():
            if trade.current_price is None:
                continue
            if trade.direction == "BUY":
                total += (trade.current_price - trade.entry_price) * trade.quantity
            else:
                total += (trade.entry_price - trade.current_price) * trade.quantity
        return total

    def _daily_marked_loss_block_reason(self) -> str:
        max_daily_loss = float(getattr(self.strategy_config.risk, "max_daily_loss", 0.0) or 0.0)
        if max_daily_loss <= 0:
            return ""
        realized = self._daily_realized_pnl()
        marked = realized + self._open_unrealized_pnl()
        loss_basis = min(realized, marked)
        if loss_basis <= -max_daily_loss:
            return (
                f"Daily marked loss {abs(loss_basis):.2f} reached "
                f"max_daily_loss {max_daily_loss:.2f}"
            )
        return ""

    @staticmethod
    def _normalized_option_side(tradingsymbol: str, fallback: str = "") -> str:
        upper = str(tradingsymbol or "").upper()
        if upper.endswith("CE"):
            return "CE"
        if upper.endswith("PE"):
            return "PE"
        return str(fallback or "").upper()

    def _signal_strategy_bucket(self, signal: Signal) -> str:
        return self._strategy_bucket_for(
            reason=signal.reason,
            instrument_type=signal.instrument_type,
            symbol=signal.symbol,
        )

    def _trade_strategy_bucket(self, trade: OpenTrade | ClosedTrade) -> str:
        return self._strategy_bucket_for(
            reason=getattr(trade, "entry_reason", ""),
            instrument_type=getattr(trade, "instrument_type", ""),
            symbol=getattr(trade, "symbol", ""),
        )

    @staticmethod
    def _strategy_bucket_for(reason: str, instrument_type: str, symbol: str) -> str:
        raw_reason = str(reason or "")
        if raw_reason.startswith("NIFTY250_2m_scanner"):
            return "NIFTY250 2m Scanner"
        if raw_reason.startswith("index_options_scanner"):
            return "Index Options Scanner"
        if (instrument_type or "").lower() == "index_option" or str(symbol).upper() in {
            "NIFTY",
            "BANKNIFTY",
            "SENSEX",
        }:
            return "Watchlist Directional - Index CE/PE"
        return "Watchlist Directional - Stock CE/PE"

    def _strategy_daily_loss_block_reason(self, signal: Signal) -> str:
        limit = float(getattr(self.strategy_config.risk, "strategy_daily_loss_limit", 0.0) or 0.0)
        if limit <= 0:
            return ""
        bucket = self._signal_strategy_bucket(signal)
        pnl = self._daily_realized_pnl(strategy_bucket=bucket)
        if pnl <= -limit:
            return (
                f"{bucket} daily loss {abs(pnl):.2f} reached "
                f"strategy_daily_loss_limit {limit:.2f}"
            )
        return ""

    def _cooldown_block_reason(self, signal: Signal) -> str:
        now = now_ist()
        bucket = self._signal_strategy_bucket(signal)
        option_side = self._normalized_option_side(signal.tradingsymbol, signal.option_side)

        symbol_cooldown = int(
            getattr(self.strategy_config.risk, "same_symbol_cooldown_minutes", 0) or 0
        )
        if symbol_cooldown > 0:
            cutoff = now - timedelta(minutes=symbol_cooldown)
            for trade in reversed(self.closed_trades):
                closed_at = trade.closed_at
                if closed_at.tzinfo is None:
                    closed_at = closed_at.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
                closed_at = closed_at.astimezone(ZoneInfo("Asia/Kolkata"))
                if closed_at < cutoff:
                    break
                if (
                    trade.pnl < 0
                    and trade.symbol.upper() == signal.symbol.upper()
                    and self._trade_strategy_bucket(trade) == bucket
                ):
                    return (
                        f"{signal.symbol} in {bucket} is cooling down after a loss "
                        f"at {closed_at.strftime('%H:%M:%S')} IST"
                    )

        side_cooldown = int(
            getattr(self.strategy_config.risk, "same_side_loss_cooldown_minutes", 0) or 0
        )
        side_loss_count = int(
            getattr(self.strategy_config.risk, "same_side_loss_cooldown_count", 0) or 0
        )
        if side_cooldown > 0 and side_loss_count > 0 and option_side:
            cutoff = now - timedelta(minutes=side_cooldown)
            losses = 0
            for trade in reversed(self.closed_trades):
                closed_at = trade.closed_at
                if closed_at.tzinfo is None:
                    closed_at = closed_at.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
                closed_at = closed_at.astimezone(ZoneInfo("Asia/Kolkata"))
                if closed_at < cutoff:
                    break
                trade_side = self._normalized_option_side(trade.tradingsymbol, trade.option_side)
                if (
                    trade.pnl < 0
                    and trade_side == option_side
                    and self._trade_strategy_bucket(trade) == bucket
                ):
                    losses += 1
            if losses >= side_loss_count:
                return (
                    f"{bucket} {option_side} entries cooling down after {losses} "
                    f"losses in {side_cooldown} minutes"
                )
        return ""

    def _position_size_block_reason(
        self,
        entry: float,
        stop: float,
        available_capital: float,
        lot_size: int,
    ) -> str:
        if entry <= 0:
            return f"Entry price {entry:.2f} is not positive"
        per_unit_risk = abs(entry - stop)
        if per_unit_risk <= 0:
            return f"Per-unit risk {per_unit_risk:.2f} is not positive"
        normalized_lot_size = max(int(lot_size), 1)
        risk_amount = self.risk_manager.capital * (self.strategy_config.risk.risk_per_trade_pct / 100)
        risk_quantity = int(risk_amount // per_unit_risk)
        trade_value_limit = max(self.strategy_config.risk.max_trade_value, 0.0)
        effective_capital = min(trade_value_limit, available_capital)
        if effective_capital <= 0:
            return f"Effective capital {effective_capital:.2f} is not positive"
        lot_value = entry * normalized_lot_size
        affordable_lots = int(effective_capital // lot_value) if lot_value > 0 else 0
        risk_lots = int(risk_quantity // normalized_lot_size)
        if risk_lots <= 0:
            return (
                f"Risk budget {risk_amount:.2f} supports {risk_quantity} unit(s), "
                f"below lot size {normalized_lot_size}"
            )
        if affordable_lots <= 0:
            return (
                f"Effective capital {effective_capital:.2f} cannot afford one lot value "
                f"{lot_value:.2f}"
            )
        return "Position size resolved to zero"

    def _trade_key(self, symbol: str, instrument_type: str) -> str:
        return f"{symbol.upper()}:{instrument_type.lower()}"

    def _open_position_count_for_instrument(self, instrument_type: str) -> int:
        normalized = (instrument_type or "").lower()
        return sum(1 for trade in self.open_trades.values() if trade.instrument_type == normalized)

    def _new_position_block_reason(self, instrument_type: str) -> str:
        base_reason = self.risk_manager.new_trade_block_reason(
            len(self.open_trades),
            realized_pnl=self._daily_realized_pnl(),
        )
        if base_reason:
            return base_reason
        marked_loss_reason = self._daily_marked_loss_block_reason()
        if marked_loss_reason:
            return marked_loss_reason

        normalized = (instrument_type or "").lower()
        if normalized == "stock_option":
            limit = int(getattr(self.strategy_config.risk, "max_stock_option_positions", 0) or 0)
            if limit > 0:
                active = self._open_position_count_for_instrument("stock_option")
                if active >= limit:
                    return f"Stock-option positions {active} reached max_stock_option_positions {limit}"
        if normalized == "index_option":
            raw = self.strategy_config.index_options_scanner or {}
            configured_limit = raw.get("max_open_positions")
            if configured_limit is None:
                configured_limit = getattr(self.strategy_config.risk, "max_index_option_positions", 0)
            limit = int(configured_limit or 0)
            if limit > 0:
                active = self._open_position_count_for_instrument("index_option")
                if active >= limit:
                    return f"Index-option positions {active} reached max_index_option_positions {limit}"
        return ""

    def _scanner_enabled(self) -> bool:
        raw = self.strategy_config.scanner_2m_nifty250 or {}
        return bool(raw.get("enabled", False))

    def _index_options_scanner_enabled(self) -> bool:
        raw = self.strategy_config.index_options_scanner or {}
        return bool(raw.get("enabled", False))

    def _index_option_watch_item(self, symbol: str, interval: str, raw: dict) -> WatchItem:
        symbol = symbol.upper().strip()
        exchange = "BSE" if symbol == "SENSEX" else "NSE"
        contract_exchange_by_symbol = raw.get("contract_exchange_by_symbol", {}) or {}
        contract_exchange = str(
            contract_exchange_by_symbol.get(symbol, "BFO" if symbol == "SENSEX" else "NFO")
        ).upper()
        return WatchItem(
            symbol=symbol,
            exchange=exchange,
            contract_exchange=contract_exchange,
            instrument_type="index_option",
            interval=interval,
            enabled=True,
            option_side="auto",
            option_type="call",
            option_expiry_hint="",
            option_days_to_expiry=max(self.strategy_config.contract_selection.max_dte, 1),
            option_iv_min=0.05,
            option_iv_max=1.5,
            option_delta_min=0.15,
            option_delta_max=0.75,
            risk_free_rate=0.07,
        )

    def _index_scanner_contract_policy(self, symbol: str, raw: dict) -> ContractSelectionConfig:
        symbol = symbol.upper().strip()
        base = self.strategy_config.contract_selection
        expiry_type_by_symbol = raw.get("expiry_type_by_symbol", {}) or {}
        min_dte_by_symbol = raw.get("min_dte_by_symbol", {}) or {}
        max_dte_by_symbol = raw.get("max_dte_by_symbol", {}) or {}
        expiry_type = str(expiry_type_by_symbol.get(symbol, raw.get("expiry_type", base.expiry_type)))
        min_dte = int(min_dte_by_symbol.get(symbol, raw.get("min_dte", base.min_dte)))
        max_dte = int(max_dte_by_symbol.get(symbol, raw.get("max_dte", base.max_dte)))
        return replace(base, expiry_type=expiry_type, min_dte=min_dte, max_dte=max_dte)

    def _index_scanner_evaluation(self, snapshot, raw: dict) -> dict[str, object]:
        close = max(float(snapshot.close), 0.01)
        momentum_pct = float(snapshot.momentum) * 100.0
        ema_gap_pct = (abs(float(snapshot.ema20) - float(snapshot.ema50)) / close) * 100.0
        min_momentum_pct = float(raw.get("min_momentum_pct", 0.02))
        min_ema_gap_pct = float(raw.get("min_ema_gap_pct", 0.03))
        bullish_rsi = float(raw.get("bullish_rsi", 55.0))
        bearish_rsi = float(raw.get("bearish_rsi", 45.0))
        require_breakout = bool(raw.get("require_breakout", False))

        bullish = (
            snapshot.regime == "bullish"
            and snapshot.rsi14 >= bullish_rsi
            and momentum_pct >= min_momentum_pct
            and ema_gap_pct >= min_ema_gap_pct
            and (not require_breakout or snapshot.breakout_up)
        )
        bearish = (
            snapshot.regime == "bearish"
            and snapshot.rsi14 <= bearish_rsi
            and momentum_pct <= -min_momentum_pct
            and ema_gap_pct >= min_ema_gap_pct
            and (not require_breakout or snapshot.breakout_down)
        )

        rsi_strength = abs(float(snapshot.rsi14) - 50.0)
        score = 50.0
        score += min(rsi_strength * 1.1, 25.0)
        score += min(ema_gap_pct * 12.0, 15.0)
        score += min(abs(momentum_pct) * 20.0, 10.0)
        score = min(score, 100.0)
        min_score = float(raw.get("min_score", 65.0))
        direction = "BULLISH" if snapshot.regime == "bullish" else ("BEARISH" if snapshot.regime == "bearish" else "-")
        option_side = "CE" if snapshot.regime == "bullish" else ("PE" if snapshot.regime == "bearish" else "-")
        failures: list[str] = []
        if snapshot.regime == "bullish":
            if snapshot.rsi14 < bullish_rsi:
                failures.append(f"RSI {snapshot.rsi14:.1f} below bullish threshold {bullish_rsi:.1f}")
            if momentum_pct < min_momentum_pct:
                failures.append(f"Momentum {momentum_pct:.4f}% below {min_momentum_pct:.4f}%")
            if ema_gap_pct < min_ema_gap_pct:
                failures.append(f"EMA gap {ema_gap_pct:.4f}% below {min_ema_gap_pct:.4f}%")
            if require_breakout and not snapshot.breakout_up:
                failures.append("Breakout confirmation missing")
        elif snapshot.regime == "bearish":
            if snapshot.rsi14 > bearish_rsi:
                failures.append(f"RSI {snapshot.rsi14:.1f} above bearish threshold {bearish_rsi:.1f}")
            if momentum_pct > -min_momentum_pct:
                failures.append(f"Momentum {momentum_pct:.4f}% above -{min_momentum_pct:.4f}%")
            if ema_gap_pct < min_ema_gap_pct:
                failures.append(f"EMA gap {ema_gap_pct:.4f}% below {min_ema_gap_pct:.4f}%")
            if require_breakout and not snapshot.breakout_down:
                failures.append("Breakdown confirmation missing")
        else:
            failures.append("Regime neutral; EMA/RSI trend alignment missing")
        if score < min_score:
            failures.append(f"Score {score:.1f} below min_score {min_score:.1f}")
        entry_ready = (bullish or bearish) and score >= min_score

        return {
            "direction": direction,
            "option_side": option_side,
            "score": score,
            "momentum_pct": momentum_pct,
            "ema_gap_pct": ema_gap_pct,
            "entry_ready": entry_ready,
            "reason": "; ".join(failures) if failures else "All signal gates passed.",
        }

    def _index_scanner_decision(self, snapshot, raw: dict) -> dict[str, object] | None:
        evaluation = self._index_scanner_evaluation(snapshot, raw)
        if not evaluation["entry_ready"]:
            return None

        return {
            "direction": evaluation["direction"],
            "option_side": evaluation["option_side"],
            "score": evaluation["score"],
            "momentum_pct": evaluation["momentum_pct"],
            "ema_gap_pct": evaluation["ema_gap_pct"],
            "reason": evaluation["reason"],
        }

    def _run_index_options_scanner_strategy(self) -> None:
        raw = self.strategy_config.index_options_scanner or {}
        symbols = [
            str(symbol).upper().strip()
            for symbol in raw.get("symbols", ["NIFTY", "BANKNIFTY", "SENSEX"])
            if str(symbol).strip()
        ]
        symbols = [symbol for symbol in symbols if symbol in {"NIFTY", "BANKNIFTY", "SENSEX"}]
        if not symbols:
            self.logger.info("Index options scanner disabled: no supported index symbols configured")
            return

        interval = str(raw.get("interval", "5minute"))
        max_signals_per_cycle = int(raw.get("max_signals_per_cycle", 1))
        traded = 0

        session_decision = self.session_guard.can_open_new_positions(datetime.now())
        if not session_decision.allowed:
            self.logger.info("Index options scanner gated by session rule: %s", session_decision.reason)
            return
        risk_block_reason = self._new_position_block_reason("index_option")
        if risk_block_reason:
            self.logger.warning("Index options scanner gated by risk manager: %s", risk_block_reason)
            return

        for symbol in symbols:
            if traded >= max_signals_per_cycle:
                break
            item = self._index_option_watch_item(symbol, interval, raw)
            key = self._trade_key(item.symbol, item.instrument_type)
            if key in self.open_trades:
                self.logger.info("Index options scanner skip %s: position already open", symbol)
                continue

            try:
                df = self._get_analysis_data(item, interval, datetime.now())
            except Exception as exc:
                self.logger.info("Index options scanner data skip for %s: %s", symbol, exc)
                continue
            df = completed_intraday_candles(df, interval, now_ist())
            if df.empty:
                self.logger.info(
                    "Index options scanner data skip for %s: no completed candles",
                    symbol,
                )
                continue
            if len(df) < self.strategy_config.min_candles_for_analysis:
                self.logger.info(
                    "Index options scanner waiting for %s candles: %s/%s",
                    symbol,
                    len(df),
                    self.strategy_config.min_candles_for_analysis,
                )
                continue

            try:
                snapshot = analyze_market(df, item)
            except Exception as exc:
                self.logger.info("Index options scanner analysis skip for %s: %s", symbol, exc)
                continue
            self.risk_manager.register_price_change(snapshot.previous_close, snapshot.close)

            decision = self._index_scanner_evaluation(snapshot, raw)
            if not decision["entry_ready"]:
                self.logger.info(
                    "Index options scanner no signal for %s regime=%s close=%.2f rsi=%.2f score=%.2f momentum_pct=%.4f ema_gap_pct=%.4f reason=%s",
                    symbol,
                    snapshot.regime,
                    snapshot.close,
                    snapshot.rsi14,
                    float(decision["score"]),
                    float(decision["momentum_pct"]),
                    float(decision["ema_gap_pct"]),
                    decision["reason"],
                )
                continue

            regime = "bullish" if decision["direction"] == "BULLISH" else "bearish"
            contract_policy = self._index_scanner_contract_policy(symbol, raw)
            contract = self._resolve_contract(item, snapshot.close, regime, policy=contract_policy)
            if contract.get("rejected_reason"):
                self.logger.info(
                    "Index options scanner contract selection blocked for %s: %s",
                    symbol,
                    contract["rejected_reason"],
                )
                continue

            expiry_decision = self.session_guard.can_trade_contract_expiry(str(contract.get("expiry", "")))
            if not expiry_decision.allowed:
                self.logger.info("Index options scanner expiry policy blocks %s: %s", symbol, expiry_decision.reason)
                continue

            entry = float(contract.get("entry_price", 0.0) or 0.0)
            if entry <= 0:
                self.logger.info("Index options scanner skip %s: invalid entry price %.2f", symbol, entry)
                continue

            stop = entry * (1.0 - (self.strategy_config.risk.option_sl_pct / 100.0))
            target = entry * (1.0 + (self.strategy_config.risk.option_target_pct / 100.0))
            lot_size = int(contract.get("lot_size", 1) or 1)
            available_capital = self._available_capital()
            qty = self.risk_manager.position_size(
                entry,
                stop,
                available_capital=available_capital,
                lot_size=lot_size,
            )
            if qty <= 0:
                self.logger.info(
                    "Index options scanner skip %s: position size is zero | entry=%.2f stop=%.2f lot_size=%s reason=%s",
                    symbol,
                    entry,
                    stop,
                    lot_size,
                    self._position_size_block_reason(entry, stop, available_capital, lot_size),
                )
                continue

            signal = Signal(
                symbol=symbol,
                exchange=str(contract.get("exchange", item.contract_exchange)),
                direction="BUY",
                regime=regime,
                entry_price=entry,
                stop_loss=float(stop),
                target_price=float(target),
                quantity=int(qty),
                reason=(
                    f"index_options_scanner interval={interval} score={float(decision['score']):.2f} "
                    f"side={decision['option_side']} momentum_pct={float(decision['momentum_pct']):.4f} "
                    f"ema_gap_pct={float(decision['ema_gap_pct']):.4f}"
                ),
                tradingsymbol=str(contract.get("tradingsymbol", "")),
                instrument_type="index_option",
                underlying_symbol=symbol,
                underlying_exchange=item.exchange,
                lot_size=lot_size,
                lots=max(int(qty // lot_size), 0),
                option_side=str(contract.get("option_side", decision["option_side"])),
                option_expiry=str(contract.get("expiry", "")),
                option_strike=float(contract["strike"]) if contract.get("strike") is not None else None,
                option_bid=float(contract["bid"]) if contract.get("bid") is not None else None,
                option_ask=float(contract["ask"]) if contract.get("ask") is not None else None,
                option_spread_pct=float(contract["spread_pct"]) if contract.get("spread_pct") is not None else None,
            )

            risk_gate_reason = self._entry_risk_gate_reason(signal)
            if risk_gate_reason:
                self.logger.info("Index options scanner risk-gate skip for %s: %s", symbol, risk_gate_reason)
                continue

            self._execute_entry(signal)
            traded += 1

        if traded == 0:
            self.logger.info("Index options scanner: no index trades passed execution gates this cycle")

    def _scanner_sideways_context(self, raw: dict) -> dict[str, object]:
        if not bool(raw.get("sideways_filter_enabled", False)):
            return {"sideways": False, "reason": "disabled"}

        symbols = [
            str(symbol).upper().strip()
            for symbol in raw.get("sideways_reference_symbols", ["NIFTY", "SENSEX"])
            if str(symbol).strip()
        ]
        symbols = [symbol for symbol in symbols if symbol in {"NIFTY", "BANKNIFTY", "SENSEX"}]
        if not symbols:
            return {"sideways": False, "reason": "no supported reference symbols"}

        interval = str(raw.get("sideways_interval", "5minute"))
        max_abs_momentum_pct = float(raw.get("sideways_max_abs_momentum_pct", 0.08))
        max_ema_gap_pct = float(raw.get("sideways_max_ema_gap_pct", 0.08))
        min_votes = int(raw.get("sideways_min_votes", len(symbols)))
        votes = 0
        details: list[str] = []
        index_raw = self.strategy_config.index_options_scanner or {}
        for symbol in symbols:
            item = self._index_option_watch_item(symbol, interval, index_raw)
            try:
                df = self._get_analysis_data(item, interval, datetime.now())
                if df.empty or len(df) < self.strategy_config.min_candles_for_analysis:
                    details.append(f"{symbol}: insufficient candles")
                    continue
                snapshot = analyze_market(df, item)
            except Exception as exc:
                details.append(f"{symbol}: data unavailable ({exc})")
                continue
            close = max(float(snapshot.close), 0.01)
            momentum_pct = abs(float(snapshot.momentum) * 100.0)
            ema_gap_pct = (abs(float(snapshot.ema20) - float(snapshot.ema50)) / close) * 100.0
            sideways = (
                snapshot.regime == "neutral"
                or (
                    momentum_pct <= max_abs_momentum_pct
                    and ema_gap_pct <= max_ema_gap_pct
                )
            )
            if sideways:
                votes += 1
            details.append(
                f"{symbol}: regime={snapshot.regime}, momentum={momentum_pct:.4f}%, "
                f"ema_gap={ema_gap_pct:.4f}%"
            )

        is_sideways = votes >= max(min_votes, 1)
        return {
            "sideways": is_sideways,
            "reason": f"{votes}/{len(symbols)} sideways votes; " + "; ".join(details),
        }

    def _run_nifty250_scanner_strategy(self) -> None:
        raw = self.strategy_config.scanner_2m_nifty250 or {}
        min_score = float(raw.get("min_score", 60.0))
        max_symbols = int(raw.get("max_symbols", 250))
        max_signals_per_cycle = int(raw.get("max_signals_per_cycle", 1))
        min_candle_length_pct = float(raw.get("min_candle_length_pct", 0.25))
        min_volume_ratio = float(raw.get("min_volume_ratio", 0.0) or 0.0)
        sideways_context = self._scanner_sideways_context(raw)

        session_decision = self.session_guard.can_open_new_positions(datetime.now())
        if not session_decision.allowed:
            self.logger.info("Scanner gated by session rule: %s", session_decision.reason)
            return
        risk_block_reason = self._new_position_block_reason("stock_option")
        if risk_block_reason:
            self.logger.warning("Scanner gated by risk manager: %s", risk_block_reason)
            return

        signals = scan_nifty250_2m(max_symbols=max_symbols, min_score=min_score)
        if not signals:
            self.logger.info("NIFTY250 2m scanner: no actionable signals this cycle")
            return

        traded = 0
        for scanner_signal in signals:
            if traded >= max_signals_per_cycle:
                break
            symbol = scanner_signal.symbol.upper()
            instrument_type = "index_option" if symbol in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"} else "stock_option"
            key = self._trade_key(symbol, instrument_type)
            if key in self.open_trades:
                continue
            if float(scanner_signal.candle_length_pct) < min_candle_length_pct:
                self.logger.info(
                    "Scanner skip %s: candle_length_pct %.4f below min %.4f",
                    symbol,
                    float(scanner_signal.candle_length_pct),
                    min_candle_length_pct,
                )
                continue
            if min_volume_ratio > 0 and float(scanner_signal.volume_ratio) < min_volume_ratio:
                self.logger.info(
                    "Scanner skip %s: volume_ratio %.2f below min_volume_ratio %.2f",
                    symbol,
                    float(scanner_signal.volume_ratio),
                    min_volume_ratio,
                )
                continue
            if sideways_context["sideways"]:
                sideways_min_score = float(raw.get("sideways_min_score", max(min_score, 80.0)))
                sideways_min_volume_ratio = float(
                    raw.get("sideways_min_volume_ratio", max(min_volume_ratio, 1.5))
                )
                if (
                    float(scanner_signal.score) < sideways_min_score
                    or float(scanner_signal.volume_ratio) < sideways_min_volume_ratio
                ):
                    self.logger.info(
                        "Scanner skip %s: sideways market filter blocked score %.2f / volume_ratio %.2f "
                        "(requires score %.2f and volume_ratio %.2f). Context: %s",
                        symbol,
                        float(scanner_signal.score),
                        float(scanner_signal.volume_ratio),
                        sideways_min_score,
                        sideways_min_volume_ratio,
                        sideways_context["reason"],
                    )
                    continue

            option_side = "PE" if scanner_signal.direction == "BEARISH" else "CE"
            try:
                contract = self.broker.find_option_contract_details(
                    underlying_symbol=symbol,
                    spot_price=scanner_signal.signal_price,
                    option_side=option_side,
                    contract_exchange="NFO",
                    expiry_hint="",
                )
            except Exception as exc:
                self.logger.info("Scanner contract resolution skip for %s: %s", symbol, exc)
                continue

            try:
                entry = self.broker.get_ltp(str(contract["exchange"]), str(contract["tradingsymbol"]))
            except Exception as exc:
                self.logger.info("Scanner LTP fetch skip for %s: %s", symbol, exc)
                continue
            if entry <= 0:
                continue

            candle_pct = max(scanner_signal.candle_length_pct, min_candle_length_pct)
            target_pct = max(candle_pct * 1.0, self.strategy_config.risk.option_target_pct)
            sl_pct = max(candle_pct * 0.5, self.strategy_config.risk.option_sl_pct)
            stop = entry * (1.0 - sl_pct / 100.0)
            target = entry * (1.0 + target_pct / 100.0)
            lot_size = int(contract.get("lot_size", 1) or 1)
            qty = self.risk_manager.position_size(
                entry,
                stop,
                available_capital=self._available_capital(),
                lot_size=lot_size,
            )
            if qty <= 0:
                continue

            signal = Signal(
                symbol=symbol,
                exchange=str(contract["exchange"]),
                direction="BUY",
                regime=scanner_signal.direction.lower(),
                entry_price=float(entry),
                stop_loss=float(stop),
                target_price=float(target),
                quantity=int(qty),
                reason=(
                    f"NIFTY250_2m_scanner score={scanner_signal.score:.2f} "
                    f"vol_ratio={scanner_signal.volume_ratio:.2f}"
                ),
                tradingsymbol=str(contract["tradingsymbol"]),
                instrument_type=instrument_type,
                underlying_symbol=symbol,
                underlying_exchange="NSE",
                lot_size=lot_size,
                lots=max(int(qty // lot_size), 0),
                option_side=option_side,
                option_expiry=str(contract.get("expiry", "")),
                option_strike=float(contract["strike"]) if contract.get("strike") is not None else None,
            )
            risk_gate_reason = self._entry_risk_gate_reason(signal)
            if risk_gate_reason:
                self.logger.info("Scanner risk-gate skip for %s: %s", symbol, risk_gate_reason)
                continue

            self._execute_entry(signal)
            traded += 1

        if traded == 0:
            self.logger.info("NIFTY250 2m scanner: signals found but none passed execution gates")
