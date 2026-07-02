from __future__ import annotations

from dataclasses import dataclass

from algotrader.brokers.base import BaseBroker
from algotrader.config import ContractSelectionConfig, WatchItem
from algotrader.models import OptionChainEntry


@dataclass
class OptionChainSnapshot:
    underlying_symbol: str
    spot_price: float
    rows: list[OptionChainEntry]


class OptionChainService:
    def __init__(self, broker: BaseBroker) -> None:
        self.broker = broker

    def load(
        self,
        item: WatchItem,
        spot_price: float,
        policy: ContractSelectionConfig,
    ) -> OptionChainSnapshot:
        raw_rows = self.broker.get_option_chain(
            underlying_symbol=item.symbol,
            contract_exchange=item.contract_exchange,
            spot_price=spot_price,
        )
        filtered = self._apply_base_policy_filters(raw_rows, policy, item)
        return OptionChainSnapshot(
            underlying_symbol=item.symbol,
            spot_price=spot_price,
            rows=filtered,
        )

    def _apply_base_policy_filters(
        self,
        rows: list[OptionChainEntry],
        policy: ContractSelectionConfig,
        item: WatchItem,
    ) -> list[OptionChainEntry]:
        output: list[OptionChainEntry] = []
        max_dte = policy.max_dte
        if item.instrument_type == "stock_option":
            # NSE stock options are monthly; the global index-weekly DTE window is too tight for them.
            max_dte = max(max_dte, 45)
        for row in rows:
            if row.dte_days < policy.min_dte or row.dte_days > max_dte:
                continue
            if row.oi is not None and row.oi < policy.min_oi:
                continue
            if row.volume is not None and row.volume < policy.min_volume:
                continue
            if (
                row.spread_pct is not None
                and policy.max_spread_pct > 0
                and row.spread_pct > policy.max_spread_pct
            ):
                continue
            output.append(row)
        return output
