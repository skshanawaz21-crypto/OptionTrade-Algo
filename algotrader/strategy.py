from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from algotrader.config import RiskConfig, WatchItem
from algotrader.indicators import enrich_indicators
from algotrader.models import ExitDecision, OptionMetrics, Signal
from algotrader.options import black_scholes_greeks, estimate_atm_strike, implied_volatility
from algotrader.risk import RiskManager


@dataclass
class AnalysisSnapshot:
    symbol: str
    exchange: str
    close: float
    ema20: float
    ema50: float
    rsi14: float
    atr14: float
    momentum: float
    breakout_up: bool
    breakout_down: bool
    regime: str
    previous_close: float


def analyze_market(df: pd.DataFrame, item: WatchItem) -> AnalysisSnapshot:
    enriched = enrich_indicators(df)
    if len(enriched) < 2:
        raise ValueError(
            f"Not enough candle history for {item.symbol}. Need at least 2 enriched rows, got {len(enriched)}."
        )
    last = enriched.iloc[-1]
    regime = "neutral"
    if last["ema20"] > last["ema50"] and last["rsi14"] >= 55:
        regime = "bullish"
    elif last["ema20"] < last["ema50"] and last["rsi14"] <= 45:
        regime = "bearish"

    return AnalysisSnapshot(
        symbol=item.symbol,
        exchange=item.exchange,
        close=float(last["close"]),
        ema20=float(last["ema20"]),
        ema50=float(last["ema50"]),
        rsi14=float(last["rsi14"]),
        atr14=float(last["atr14"]),
        momentum=float(last["momentum"]),
        breakout_up=bool(last["close"] > last["prev_high"]),
        breakout_down=bool(last["close"] < last["prev_low"]),
        regime=regime,
        previous_close=float(enriched.iloc[-2]["close"]) if len(enriched) > 1 else float(last["close"]),
    )


def build_option_metrics(snapshot: AnalysisSnapshot, item: WatchItem) -> OptionMetrics | None:
    if item.instrument_type != "index_option":
        return None

    strike = estimate_atm_strike(snapshot.close)
    time_to_expiry_years = max(item.option_days_to_expiry, 1) / 365.0
    theoretical = black_scholes_greeks(
        spot_price=snapshot.close,
        strike_price=strike,
        time_to_expiry_years=time_to_expiry_years,
        risk_free_rate=item.risk_free_rate,
        volatility=max(snapshot.atr14 / snapshot.close, 0.01),
        option_type=item.option_type,
    )
    market_price_estimate = max(theoretical["price"] * 1.02, 0.05)
    iv = implied_volatility(
        market_price=market_price_estimate,
        spot_price=snapshot.close,
        strike_price=strike,
        time_to_expiry_years=time_to_expiry_years,
        risk_free_rate=item.risk_free_rate,
        option_type=item.option_type,
    )
    return OptionMetrics(
        implied_volatility=iv,
        delta=theoretical["delta"],
        gamma=theoretical["gamma"],
        theta=theoretical["theta"],
        vega=theoretical["vega"],
        theoretical_price=theoretical["price"],
    )


def option_filters_pass(item: WatchItem, option_metrics: OptionMetrics | None) -> bool:
    if item.instrument_type != "index_option" or option_metrics is None:
        return True
    if option_metrics.implied_volatility is None or option_metrics.delta is None:
        return False
    delta = abs(option_metrics.delta)
    return (
        item.option_iv_min <= option_metrics.implied_volatility <= item.option_iv_max
        and item.option_delta_min <= delta <= item.option_delta_max
    )


def build_signal(
    snapshot: AnalysisSnapshot,
    item: WatchItem,
    risk_config: RiskConfig,
    risk_manager: RiskManager,
    contract: dict[str, object],
    available_capital: float,
) -> Signal | None:
    stop_distance = snapshot.atr14 * risk_config.sl_atr_multiplier
    if stop_distance <= 0:
        return None
    option_metrics = build_option_metrics(snapshot, item)
    if not option_filters_pass(item, option_metrics):
        return None
    is_option = item.instrument_type in {"index_option", "stock_option"}
    is_future = item.instrument_type in {"index_future", "stock_future"}
    lot_size = int(contract.get("lot_size", 1) or 1)
    lots = 0

    def build_option_trade(reason: str) -> Signal | None:
        entry = float(contract.get("entry_price", 0.0) or 0.0)
        if entry <= 0:
            return None
        bid = float(contract["bid"]) if contract.get("bid") is not None else None
        ask = float(contract["ask"]) if contract.get("ask") is not None else None
        spread_pct = (
            float(contract["spread_pct"]) if contract.get("spread_pct") is not None else None
        )
        expected_slippage_pct = None
        if ask is not None and bid is not None and entry > 0:
            mid = (bid + ask) / 2.0
            if mid > 0:
                expected_slippage_pct = max(((ask - mid) / mid) * 100.0, 0.0)
        stop = entry * (1.0 - (risk_config.option_sl_pct / 100.0))
        target = entry * (1.0 + (risk_config.option_target_pct / 100.0))
        qty = risk_manager.position_size(
            entry,
            stop,
            available_capital=available_capital,
            lot_size=lot_size,
        )
        if qty <= 0:
            return None
        computed_lots = max(qty // lot_size, 0)
        return Signal(
            symbol=item.symbol,
            exchange=str(contract.get("exchange", item.contract_exchange)),
            direction="BUY",
            regime=snapshot.regime,
            entry_price=entry,
            stop_loss=stop,
            target_price=target,
            quantity=qty,
            reason=reason,
            tradingsymbol=str(contract.get("tradingsymbol", "")),
            instrument_type=item.instrument_type,
            underlying_symbol=item.symbol,
            underlying_exchange=item.exchange,
            lot_size=lot_size,
            lots=computed_lots,
            option_side=str(contract.get("option_side", "")),
            option_expiry=str(contract.get("expiry", "")),
            option_strike=float(contract["strike"]) if contract.get("strike") is not None else None,
            option_metrics=option_metrics,
            option_bid=bid,
            option_ask=ask,
            option_spread_pct=spread_pct,
            expected_slippage_pct=expected_slippage_pct,
        )

    if snapshot.regime == "bullish" and snapshot.breakout_up and snapshot.momentum > 0:
        if is_option:
            return build_option_trade("Bullish trend + breakout + momentum alignment via CE")
        if is_future:
            entry = float(contract.get("entry_price", snapshot.close))
            stop = entry - stop_distance
            target = entry + (entry - stop) * risk_config.reward_to_risk
            qty = risk_manager.position_size(
                entry,
                stop,
                available_capital=available_capital,
                lot_size=lot_size,
            )
            if qty <= 0:
                return None
            computed_lots = max(qty // lot_size, 0)
            return Signal(
                symbol=item.symbol,
                exchange=str(contract.get("exchange", item.contract_exchange)),
                direction="BUY",
                regime=snapshot.regime,
                entry_price=entry,
                stop_loss=stop,
                target_price=target,
                quantity=qty,
                reason="Bullish trend + breakout + momentum alignment via FUT",
                tradingsymbol=str(contract.get("tradingsymbol", "")),
                instrument_type=item.instrument_type,
                underlying_symbol=item.symbol,
                underlying_exchange=item.exchange,
                lot_size=lot_size,
                lots=computed_lots,
            )
        entry = snapshot.close
        stop = entry - stop_distance
        target = entry + (entry - stop) * risk_config.reward_to_risk
        qty = risk_manager.position_size(entry, stop, available_capital=available_capital)
        if qty <= 0:
            return None
        return Signal(
            symbol=item.symbol,
            exchange=item.exchange,
            direction="BUY",
            regime=snapshot.regime,
            entry_price=entry,
            stop_loss=stop,
            target_price=target,
            quantity=qty,
            reason="Bullish trend + breakout + momentum alignment",
            tradingsymbol=str(contract.get("tradingsymbol", item.symbol)),
            instrument_type=item.instrument_type,
            underlying_symbol=item.symbol,
            underlying_exchange=item.exchange,
            lot_size=lot_size,
            lots=0,
            option_metrics=option_metrics,
        )

    if snapshot.regime == "bearish" and snapshot.breakout_down and snapshot.momentum < 0:
        if is_option:
            return build_option_trade("Bearish trend + breakdown + momentum alignment via PE")
        if is_future:
            entry = float(contract.get("entry_price", snapshot.close))
            stop = entry + stop_distance
            target = entry - (stop - entry) * risk_config.reward_to_risk
            qty = risk_manager.position_size(
                entry,
                stop,
                available_capital=available_capital,
                lot_size=lot_size,
            )
            if qty <= 0:
                return None
            computed_lots = max(qty // lot_size, 0)
            return Signal(
                symbol=item.symbol,
                exchange=str(contract.get("exchange", item.contract_exchange)),
                direction="SELL",
                regime=snapshot.regime,
                entry_price=entry,
                stop_loss=stop,
                target_price=target,
                quantity=qty,
                reason="Bearish trend + breakdown + momentum alignment via FUT",
                tradingsymbol=str(contract.get("tradingsymbol", "")),
                instrument_type=item.instrument_type,
                underlying_symbol=item.symbol,
                underlying_exchange=item.exchange,
                lot_size=lot_size,
                lots=computed_lots,
            )
        entry = snapshot.close
        stop = entry + stop_distance
        target = entry - (stop - entry) * risk_config.reward_to_risk
        qty = risk_manager.position_size(entry, stop, available_capital=available_capital)
        if qty <= 0:
            return None
        return Signal(
            symbol=item.symbol,
            exchange=item.exchange,
            direction="SELL",
            regime=snapshot.regime,
            entry_price=entry,
            stop_loss=stop,
            target_price=target,
            quantity=qty,
            reason="Bearish trend + breakdown + momentum alignment",
            tradingsymbol=str(contract.get("tradingsymbol", item.symbol)),
            instrument_type=item.instrument_type,
            underlying_symbol=item.symbol,
            underlying_exchange=item.exchange,
            lot_size=lot_size,
            lots=0,
            option_metrics=option_metrics,
        )

    return None


def should_exit_trade(direction: str, price: float, stop_loss: float, target_price: float) -> ExitDecision:
    if direction == "BUY":
        if price <= stop_loss:
            return ExitDecision(True, "Stop loss hit")
        if price >= target_price:
            return ExitDecision(True, "Target hit")
    else:
        if price >= stop_loss:
            return ExitDecision(True, "Stop loss hit")
        if price <= target_price:
            return ExitDecision(True, "Target hit")
    return ExitDecision(False, "")
