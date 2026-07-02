from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from algotrader.config import SessionRulesConfig


@dataclass
class SessionDecision:
    allowed: bool
    reason: str = ""


class SessionGuard:
    def __init__(self, config: SessionRulesConfig) -> None:
        self.config = config
        try:
            self._tz = ZoneInfo(config.timezone)
        except Exception:
            self._tz = ZoneInfo("Asia/Kolkata")

    def can_open_new_positions(self, now: datetime) -> SessionDecision:
        local_now = self._local_datetime(now)
        time_now = local_now.time()
        start = self._parse_hhmm(self.config.trading_start).time()
        end = self._parse_hhmm(self.config.trading_end).time()
        cutoff = self._parse_hhmm(self.config.no_new_trade_after).time()

        if time_now < start or time_now > end:
            return SessionDecision(False, "Outside trading window")
        if time_now > cutoff:
            return SessionDecision(False, "Past no-new-trade cutoff")
        for window in self.config.event_blackout_windows or []:
            window_start = self._parse_hhmm(window.start).time()
            window_end = self._parse_hhmm(window.end).time()
            if window_start <= time_now <= window_end:
                label = f" ({window.label})" if window.label else ""
                return SessionDecision(False, f"Event blackout window{label}")
        return SessionDecision(True, "")

    def can_trade_contract_expiry(self, expiry: str) -> SessionDecision:
        behavior = self.config.expiry_day_behavior.lower().strip()
        if behavior == "allow":
            return SessionDecision(True, "")
        if behavior not in {"no_new_trades", "skip_day"}:
            return SessionDecision(True, "")
        try:
            expiry_date = date.fromisoformat(expiry)
        except ValueError:
            return SessionDecision(True, "")
        if expiry_date != datetime.now(self._tz).date():
            return SessionDecision(True, "")
        if behavior == "no_new_trades":
            return SessionDecision(False, "Expiry day no-new-trades policy")
        return SessionDecision(False, "Expiry day skip-day policy")

    def _parse_hhmm(self, value: str) -> datetime:
        return datetime.strptime(value, "%H:%M")

    def _local_datetime(self, now: datetime) -> datetime:
        if now.tzinfo is None:
            return now.replace(tzinfo=self._tz)
        return now.astimezone(self._tz)
