from __future__ import annotations

from collections import deque

import math

from algotrader.config import RiskConfig


class RiskManager:
    def __init__(self, risk_config: RiskConfig, capital: float) -> None:
        self.risk_config = risk_config
        self.capital = capital
        self.realized_pnl = 0.0
        self.peak_equity = capital
        self.return_window: deque[float] = deque(maxlen=max(risk_config.min_history_for_var, 50))
        self.last_var_pct = 0.0

    def can_open_new_trade(self, open_trade_count: int, realized_pnl: float | None = None) -> bool:
        return self.new_trade_block_reason(open_trade_count, realized_pnl=realized_pnl) == ""

    def new_trade_block_reason(self, open_trade_count: int, realized_pnl: float | None = None) -> str:
        if open_trade_count >= self.risk_config.max_open_positions:
            return (
                f"Open positions {open_trade_count} reached max_open_positions "
                f"{self.risk_config.max_open_positions}"
            )
        pnl_for_loss_gate = self.realized_pnl if realized_pnl is None else realized_pnl
        if abs(pnl_for_loss_gate) >= self.risk_config.max_daily_loss and pnl_for_loss_gate < 0:
            return (
                f"Daily loss {abs(pnl_for_loss_gate):.2f} reached max_daily_loss "
                f"{self.risk_config.max_daily_loss:.2f}"
            )
        drawdown_limit = self.risk_config.max_portfolio_drawdown_pct
        if drawdown_limit > 0 and self.current_drawdown_pct() > drawdown_limit:
            return (
                f"Drawdown {self.current_drawdown_pct():.2f}% exceeds "
                f"max_portfolio_drawdown_pct {drawdown_limit:.2f}%"
            )
        if self.last_var_pct > self.risk_config.max_var_pct:
            return (
                f"VaR {self.last_var_pct:.2f}% exceeds max_var_pct "
                f"{self.risk_config.max_var_pct:.2f}%"
            )
        return ""

    def position_size(
        self,
        entry_price: float,
        stop_loss: float,
        available_capital: float | None = None,
        lot_size: int = 1,
    ) -> int:
        risk_amount = self.capital * (self.risk_config.risk_per_trade_pct / 100)
        per_unit_risk = abs(entry_price - stop_loss)
        if per_unit_risk <= 0:
            return 0
        risk_quantity = int(risk_amount // per_unit_risk)
        if entry_price <= 0:
            return 0
        normalized_lot_size = max(int(lot_size), 1)
        trade_value_limit = max(self.risk_config.max_trade_value, 0.0)
        effective_capital = trade_value_limit
        if available_capital is not None:
            effective_capital = min(effective_capital, available_capital)
        if effective_capital <= 0:
            return 0
        affordable_lots = int(effective_capital // (entry_price * normalized_lot_size))
        risk_lots = int(risk_quantity // normalized_lot_size)
        quantity = min(risk_lots, affordable_lots) * normalized_lot_size
        return max(quantity, 0)

    def register_exit(self, pnl: float) -> None:
        self.realized_pnl += pnl
        current_equity = self.capital + self.realized_pnl
        self.peak_equity = max(self.peak_equity, current_equity)

    def register_price_change(self, previous_price: float, current_price: float) -> None:
        if previous_price <= 0:
            return
        self.return_window.append((current_price - previous_price) / previous_price)
        self.last_var_pct = self.calculate_var_pct()

    def current_drawdown_pct(self) -> float:
        current_equity = self.capital + self.realized_pnl
        if self.peak_equity <= 0:
            return 0.0
        drawdown = ((self.peak_equity - current_equity) / self.peak_equity) * 100
        return max(drawdown, 0.0)

    def calculate_var_pct(self) -> float:
        if len(self.return_window) < self.risk_config.min_history_for_var:
            return 0.0
        sorted_returns = sorted(self.return_window)
        percentile_rank = (1.0 - self.risk_config.var_confidence_level) * (len(sorted_returns) - 1)
        lower_index = math.floor(percentile_rank)
        upper_index = math.ceil(percentile_rank)
        if lower_index == upper_index:
            percentile_value = sorted_returns[lower_index]
        else:
            lower_value = sorted_returns[lower_index]
            upper_value = sorted_returns[upper_index]
            weight = percentile_rank - lower_index
            percentile_value = lower_value + (upper_value - lower_value) * weight
        return abs(percentile_value) * 100.0
