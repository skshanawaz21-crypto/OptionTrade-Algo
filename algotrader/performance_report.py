from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class TradeSummary:
    trades: int
    net_pnl: float
    wins: int
    losses: int
    win_rate_pct: float
    avg_pnl: float
    avg_win: float
    avg_loss: float
    profit_factor: float | str
    best: float
    worst: float
    max_drawdown: float
    max_consecutive_losses: int


def infer_option_side(tradingsymbol: str, fallback: str = "") -> str:
    upper = str(tradingsymbol or "").upper()
    if upper.endswith("CE"):
        return "CE"
    if upper.endswith("PE"):
        return "PE"
    return str(fallback or "").upper()


def strategy_bucket(trade: dict[str, Any]) -> str:
    reason = str(trade.get("entry_reason") or "")
    instrument_type = str(trade.get("instrument_type") or "")
    symbol = str(trade.get("symbol") or "").upper()
    if reason.startswith("NIFTY250_2m_scanner"):
        return "NIFTY250 2m Scanner"
    if reason.startswith("index_options_scanner"):
        return "Index Options Scanner"
    if instrument_type == "index_option" or symbol in {"NIFTY", "BANKNIFTY", "SENSEX"}:
        return "Watchlist Directional - Index CE/PE"
    return "Watchlist Directional - Stock CE/PE"


def summarize(trades: list[dict[str, Any]]) -> TradeSummary:
    pnls = [float(trade.get("pnl") or 0.0) for trade in trades]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    avg_win = gross_profit / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    loss_streak = 0
    max_loss_streak = 0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        if pnl < 0:
            loss_streak += 1
            max_loss_streak = max(max_loss_streak, loss_streak)
        else:
            loss_streak = 0
    if gross_loss:
        profit_factor: float | str = round(gross_profit / gross_loss, 3)
    else:
        profit_factor = "inf" if gross_profit else 0.0
    return TradeSummary(
        trades=len(trades),
        net_pnl=round(sum(pnls), 2),
        wins=len(wins),
        losses=len(losses),
        win_rate_pct=round((len(wins) / len(trades) * 100.0) if trades else 0.0, 2),
        avg_pnl=round((sum(pnls) / len(trades)) if trades else 0.0, 2),
        avg_win=round(avg_win, 2),
        avg_loss=round(avg_loss, 2),
        profit_factor=profit_factor,
        best=round(max(pnls), 2) if pnls else 0.0,
        worst=round(min(pnls), 2) if pnls else 0.0,
        max_drawdown=round(max_drawdown, 2),
        max_consecutive_losses=max_loss_streak,
    )


def build_report(state_path: Path) -> dict[str, Any]:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    closed = list(state.get("closed_trades", []))
    open_trades = list(state.get("open_trades", []))
    by_strategy: dict[str, list[dict[str, Any]]] = {}
    by_side: dict[str, list[dict[str, Any]]] = {}
    mismatches: list[dict[str, Any]] = []
    for trade in closed:
        bucket = strategy_bucket(trade)
        by_strategy.setdefault(bucket, []).append(trade)
        inferred_side = infer_option_side(
            str(trade.get("tradingsymbol") or ""),
            str(trade.get("option_side") or ""),
        )
        by_side.setdefault(inferred_side or "UNKNOWN", []).append(trade)
        stored_side = str(trade.get("option_side") or "").upper()
        if stored_side and inferred_side and stored_side != inferred_side:
            mismatches.append(
                {
                    "symbol": trade.get("symbol"),
                    "tradingsymbol": trade.get("tradingsymbol"),
                    "stored_option_side": stored_side,
                    "inferred_option_side": inferred_side,
                    "pnl": trade.get("pnl"),
                }
            )
    charges_total = sum(float(trade.get("total_charges") or 0.0) for trade in closed)
    gross_total = sum(
        float(trade.get("gross_pnl") if trade.get("gross_pnl") is not None else trade.get("pnl") or 0.0)
        for trade in closed
    )
    report = {
        "state_path": str(state_path),
        "account": state.get("account", {}),
        "open_trades": len(open_trades),
        "closed_trades": len(closed),
        "summary": summarize(closed).__dict__,
        "gross_pnl": round(gross_total, 2),
        "total_charges": round(charges_total, 2),
        "by_strategy": {
            bucket: summarize(rows).__dict__
            for bucket, rows in sorted(by_strategy.items())
        },
        "by_side": {
            side: summarize(rows).__dict__
            for side, rows in sorted(by_side.items())
        },
        "metadata_warnings": {
            "option_side_mismatches": mismatches,
        },
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a repeatable paper-trade performance report. This is not a "
            "historical option-chain backtest."
        )
    )
    parser.add_argument("--state", default="data/paper_state.json")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)

    report = build_report(Path(args.state))
    payload = json.dumps(report, indent=2)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload, encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
