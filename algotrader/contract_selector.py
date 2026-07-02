from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from algotrader.config import ContractSelectionConfig, WatchItem
from algotrader.models import OptionChainEntry
from algotrader.options import estimate_atm_strike


@dataclass
class ContractSelectionResult:
    contract: OptionChainEntry | None
    rejection_reason: str = ""


class ContractSelector:
    def select(
        self,
        rows: list[OptionChainEntry],
        item: WatchItem,
        policy: ContractSelectionConfig,
        spot_price: float,
        regime: str,
    ) -> ContractSelectionResult:
        if not rows:
            return ContractSelectionResult(None, "No option chain rows available")

        option_side = self._resolve_option_side(item, policy, regime)
        side_rows = [row for row in rows if row.option_side == option_side]
        if not side_rows:
            return ContractSelectionResult(None, f"No {option_side} contracts available")

        expiry_filtered = self._filter_by_expiry_type(side_rows, policy.expiry_type)
        if not expiry_filtered and item.instrument_type == "stock_option":
            # Stock options are monthly on NSE, so a weekly-first global policy should not
            # accidentally eliminate all otherwise valid stock-option contracts.
            expiry_filtered = self._filter_by_expiry_type(side_rows, "monthly") or side_rows
        if not expiry_filtered:
            return ContractSelectionResult(
                None,
                f"No {option_side} contracts satisfy expiry_type={policy.expiry_type}",
            )

        step = self._strike_step(expiry_filtered)
        atm = estimate_atm_strike(spot_price, step=max(step, 1))
        target_strike = self._target_strike(atm, option_side, policy.strike_mode, step, policy.strike_offset_steps)

        ranked = sorted(
            expiry_filtered,
            key=lambda row: (
                abs(row.strike - target_strike),
                row.dte_days,
                self._safe_spread(row.spread_pct),
                -self._safe_int(row.oi),
                -self._safe_int(row.volume),
            ),
        )
        if not ranked:
            return ContractSelectionResult(None, "No contracts after ranking")
        return ContractSelectionResult(ranked[0], "")

    def _resolve_option_side(
        self,
        item: WatchItem,
        policy: ContractSelectionConfig,
        regime: str,
    ) -> str:
        mode = policy.ce_pe_decision_rule.lower().strip()
        if mode == "force_ce":
            return "CE"
        if mode == "force_pe":
            return "PE"
        if mode == "watchitem":
            watch_side = item.option_side.upper().strip()
            if watch_side in {"CE", "PE"}:
                return watch_side
        if regime == "bearish":
            return "PE"
        return "CE"

    def _filter_by_expiry_type(self, rows: list[OptionChainEntry], expiry_type: str) -> list[OptionChainEntry]:
        mode = expiry_type.lower().strip()
        if mode == "any":
            return rows
        monthly_expiries = self._monthly_expiries(rows)
        if mode == "monthly":
            return [row for row in rows if row.expiry in monthly_expiries]
        if mode == "weekly":
            return [row for row in rows if row.expiry not in monthly_expiries]
        return rows

    def _monthly_expiries(self, rows: list[OptionChainEntry]) -> set[str]:
        by_month: dict[tuple[int, int], date] = {}
        for row in rows:
            try:
                expiry_date = date.fromisoformat(row.expiry)
            except ValueError:
                continue
            key = (expiry_date.year, expiry_date.month)
            if key not in by_month or expiry_date > by_month[key]:
                by_month[key] = expiry_date
        return {value.isoformat() for value in by_month.values()}

    def _strike_step(self, rows: list[OptionChainEntry]) -> int:
        strikes = sorted({int(round(row.strike)) for row in rows})
        if len(strikes) < 2:
            return 50
        diffs = [b - a for a, b in zip(strikes, strikes[1:]) if (b - a) > 0]
        if not diffs:
            return 50
        return max(min(diffs), 1)

    def _target_strike(
        self,
        atm: float,
        option_side: str,
        strike_mode: str,
        step: int,
        offset_steps: int,
    ) -> float:
        mode = strike_mode.upper().strip()
        if mode == "ATM" or offset_steps <= 0:
            return atm
        direction = 1
        if mode == "ITM":
            direction = -1
        if option_side == "PE":
            direction *= -1
        return atm + (direction * step * offset_steps)

    def _safe_int(self, value: int | None) -> int:
        return int(value or 0)

    def _safe_spread(self, value: float | None) -> float:
        if value is None:
            return 9999.0
        return float(value)
