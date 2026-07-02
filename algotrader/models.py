from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


@dataclass
class OptionChainEntry:
    tradingsymbol: str
    exchange: str
    underlying_symbol: str
    option_side: str
    expiry: str
    strike: float
    dte_days: int
    lot_size: int
    ltp: float | None = None
    bid: float | None = None
    ask: float | None = None
    oi: int | None = None
    volume: int | None = None
    iv: float | None = None
    delta: float | None = None
    spread_pct: float | None = None


@dataclass
class OptionMetrics:
    implied_volatility: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    theoretical_price: float | None = None


@dataclass
class Signal:
    symbol: str
    exchange: str
    direction: str
    regime: str
    entry_price: float
    stop_loss: float
    target_price: float
    quantity: int
    reason: str
    tradingsymbol: str = ""
    instrument_type: str = "equity"
    underlying_symbol: str = ""
    underlying_exchange: str = ""
    lot_size: int = 1
    lots: int = 0
    option_side: str = ""
    option_expiry: str = ""
    option_strike: float | None = None
    option_metrics: OptionMetrics | None = None
    option_bid: float | None = None
    option_ask: float | None = None
    option_spread_pct: float | None = None
    expected_slippage_pct: float | None = None


@dataclass
class OpenTrade:
    symbol: str
    exchange: str
    direction: str
    tradingsymbol: str
    entry_price: float
    stop_loss: float
    target_price: float
    quantity: int
    instrument_type: str = "equity"
    underlying_symbol: str = ""
    underlying_exchange: str = ""
    lot_size: int = 1
    lots: int = 0
    option_side: str = ""
    option_expiry: str = ""
    option_strike: float | None = None
    option_metrics: OptionMetrics | None = None
    option_bid: float | None = None
    option_ask: float | None = None
    option_spread_pct: float | None = None
    expected_slippage_pct: float | None = None
    entry_reason: str = ""
    current_price: float | None = None
    initial_stop_loss: float | None = None
    initial_target_price: float | None = None
    max_favourable_price: float | None = None
    target_extension_count: int = 0
    opened_at: datetime = field(default_factory=now_ist)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "exchange": self.exchange,
            "direction": self.direction,
            "tradingsymbol": self.tradingsymbol,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "target_price": self.target_price,
            "quantity": self.quantity,
            "instrument_type": self.instrument_type,
            "underlying_symbol": self.underlying_symbol,
            "underlying_exchange": self.underlying_exchange,
            "lot_size": self.lot_size,
            "lots": self.lots,
            "option_side": self.option_side,
            "option_expiry": self.option_expiry,
            "option_strike": self.option_strike,
            "option_metrics": None
            if self.option_metrics is None
            else {
                "implied_volatility": self.option_metrics.implied_volatility,
                "delta": self.option_metrics.delta,
                "gamma": self.option_metrics.gamma,
                "theta": self.option_metrics.theta,
                "vega": self.option_metrics.vega,
                "theoretical_price": self.option_metrics.theoretical_price,
            },
            "option_bid": self.option_bid,
            "option_ask": self.option_ask,
            "option_spread_pct": self.option_spread_pct,
            "expected_slippage_pct": self.expected_slippage_pct,
            "entry_reason": self.entry_reason,
            "current_price": self.current_price,
            "initial_stop_loss": self.initial_stop_loss,
            "initial_target_price": self.initial_target_price,
            "max_favourable_price": self.max_favourable_price,
            "target_extension_count": self.target_extension_count,
            "opened_at": self.opened_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "OpenTrade":
        option_metrics = raw.get("option_metrics")
        return cls(
            symbol=raw["symbol"],
            exchange=raw["exchange"],
            direction=raw["direction"],
            tradingsymbol=raw["tradingsymbol"],
            entry_price=float(raw["entry_price"]),
            stop_loss=float(raw["stop_loss"]),
            target_price=float(raw["target_price"]),
            quantity=int(raw["quantity"]),
            instrument_type=raw.get("instrument_type", "equity"),
            underlying_symbol=raw.get("underlying_symbol", raw.get("symbol", "")),
            underlying_exchange=raw.get("underlying_exchange", raw.get("exchange", "")),
            lot_size=int(raw.get("lot_size", 1)),
            lots=int(raw.get("lots", 0)),
            option_side=raw.get("option_side", ""),
            option_expiry=raw.get("option_expiry", ""),
            option_strike=float(raw["option_strike"]) if raw.get("option_strike") is not None else None,
            option_metrics=OptionMetrics(**option_metrics) if option_metrics else None,
            option_bid=float(raw["option_bid"]) if raw.get("option_bid") is not None else None,
            option_ask=float(raw["option_ask"]) if raw.get("option_ask") is not None else None,
            option_spread_pct=float(raw["option_spread_pct"]) if raw.get("option_spread_pct") is not None else None,
            expected_slippage_pct=float(raw["expected_slippage_pct"])
            if raw.get("expected_slippage_pct") is not None
            else None,
            entry_reason=str(raw.get("entry_reason", "")),
            current_price=float(raw["current_price"]) if raw.get("current_price") is not None else None,
            initial_stop_loss=float(raw["initial_stop_loss"])
            if raw.get("initial_stop_loss") is not None
            else float(raw["stop_loss"]),
            initial_target_price=float(raw["initial_target_price"])
            if raw.get("initial_target_price") is not None
            else float(raw["target_price"]),
            max_favourable_price=float(raw["max_favourable_price"])
            if raw.get("max_favourable_price") is not None
            else None,
            target_extension_count=int(raw.get("target_extension_count", 0)),
            opened_at=datetime.fromisoformat(raw["opened_at"]) if raw.get("opened_at") else now_ist(),
        )

    def capital_used(self) -> float:
        return abs(self.entry_price * self.quantity)


@dataclass
class ClosedTrade:
    symbol: str
    exchange: str
    direction: str
    tradingsymbol: str
    entry_price: float
    exit_price: float
    stop_loss: float
    target_price: float
    quantity: int
    instrument_type: str
    underlying_symbol: str
    underlying_exchange: str
    lot_size: int
    lots: int
    option_side: str
    option_expiry: str
    option_strike: float | None
    gross_pnl: float
    charges: dict[str, float]
    total_charges: float
    pnl: float
    exit_reason: str
    opened_at: datetime
    closed_at: datetime
    entry_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "exchange": self.exchange,
            "direction": self.direction,
            "tradingsymbol": self.tradingsymbol,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "stop_loss": self.stop_loss,
            "target_price": self.target_price,
            "quantity": self.quantity,
            "instrument_type": self.instrument_type,
            "underlying_symbol": self.underlying_symbol,
            "underlying_exchange": self.underlying_exchange,
            "lot_size": self.lot_size,
            "lots": self.lots,
            "option_side": self.option_side,
            "option_expiry": self.option_expiry,
            "option_strike": self.option_strike,
            "gross_pnl": self.gross_pnl,
            "charges": self.charges,
            "total_charges": self.total_charges,
            "pnl": self.pnl,
            "exit_reason": self.exit_reason,
            "entry_reason": self.entry_reason,
            "opened_at": self.opened_at.isoformat(),
            "closed_at": self.closed_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ClosedTrade":
        return cls(
            symbol=raw["symbol"],
            exchange=raw["exchange"],
            direction=raw["direction"],
            tradingsymbol=raw["tradingsymbol"],
            entry_price=float(raw["entry_price"]),
            exit_price=float(raw["exit_price"]),
            stop_loss=float(raw["stop_loss"]),
            target_price=float(raw["target_price"]),
            quantity=int(raw["quantity"]),
            instrument_type=raw.get("instrument_type", "equity"),
            underlying_symbol=raw.get("underlying_symbol", raw.get("symbol", "")),
            underlying_exchange=raw.get("underlying_exchange", raw.get("exchange", "")),
            lot_size=int(raw.get("lot_size", 1)),
            lots=int(raw.get("lots", 0)),
            option_side=raw.get("option_side", ""),
            option_expiry=raw.get("option_expiry", ""),
            option_strike=float(raw["option_strike"]) if raw.get("option_strike") is not None else None,
            gross_pnl=float(raw.get("gross_pnl", raw["pnl"])),
            charges=dict(raw.get("charges", {})),
            total_charges=float(raw.get("total_charges", 0.0)),
            pnl=float(raw["pnl"]),
            exit_reason=raw["exit_reason"],
            entry_reason=str(raw.get("entry_reason", "")),
            opened_at=datetime.fromisoformat(raw["opened_at"]),
            closed_at=datetime.fromisoformat(raw["closed_at"]),
        )


@dataclass
class ExitDecision:
    should_exit: bool
    reason: str
