from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[1] / ".env", encoding="utf-8-sig")


@dataclass
class RiskConfig:
    risk_per_trade_pct: float
    max_open_positions: int
    max_daily_loss: float
    reward_to_risk: float
    sl_atr_multiplier: float
    max_stock_option_positions: int = 0
    max_index_option_positions: int = 0
    max_trade_value: float = 50000.0
    option_sl_pct: float = 25.0
    option_target_pct: float = 50.0
    max_portfolio_drawdown_pct: float = 10.0
    var_confidence_level: float = 0.95
    min_history_for_var: int = 50
    max_var_pct: float = 2.0
    min_option_premium: float = 0.0
    max_premium_per_trade: float = 50000.0
    max_exposure_per_underlying: float = 50000.0
    max_spread_pct: float = 1.5
    max_slippage_pct: float = 0.5
    strategy_daily_loss_limit: float = 0.0
    same_symbol_cooldown_minutes: int = 0
    same_side_loss_cooldown_minutes: int = 0
    same_side_loss_cooldown_count: int = 0
    square_off_on_daily_loss: bool = False
    max_futures_notional_per_trade: float = 150000.0
    max_futures_notional_total: float = 300000.0
    max_futures_positions: int = 1
    profit_protection_enabled: bool = True
    breakeven_trigger_pct: float = 30.0
    breakeven_buffer_pct: float = 0.5
    lock_profit_trigger_pct: float = 50.0
    lock_profit_pct: float = 25.0
    target_extension_trigger_pct: float = 70.0
    target_extension_pct: float = 10.0
    target_extension_stop_lock_pct: float = 50.0
    max_target_extensions: int = 2


@dataclass
class ContractSelectionConfig:
    expiry_type: str = "weekly"
    min_dte: int = 0
    max_dte: int = 10
    strike_mode: str = "ATM"
    strike_offset_steps: int = 0
    min_oi: int = 0
    min_volume: int = 0
    max_spread_pct: float = 1.5
    ce_pe_decision_rule: str = "signal"


@dataclass
class EventBlackoutWindow:
    start: str
    end: str
    label: str = ""


@dataclass
class SessionRulesConfig:
    timezone: str = "Asia/Kolkata"
    trading_start: str = "09:15"
    trading_end: str = "15:30"
    no_new_trade_after: str = "15:15"
    square_off_open_positions: bool = True
    square_off_time: str = "15:18"
    expiry_day_behavior: str = "allow"
    event_blackout_windows: list[EventBlackoutWindow] | None = None


@dataclass
class WatchItem:
    symbol: str
    exchange: str
    instrument_type: str
    interval: str
    enabled: bool = True
    contract_exchange: str = "NFO"
    option_side: str = "auto"
    option_expiry_hint: str = ""
    option_type: str = "call"
    option_days_to_expiry: int = 7
    option_iv_min: float = 0.0
    option_iv_max: float = 5.0
    option_delta_min: float = 0.0
    option_delta_max: float = 1.0
    risk_free_rate: float = 0.07


@dataclass
class StrategyConfig:
    poll_seconds: int
    history_lookback_days: int
    default_interval: str
    min_candles_for_analysis: int
    capital: float
    risk: RiskConfig
    contract_selection: ContractSelectionConfig
    session_rules: SessionRulesConfig
    watchlist: list[WatchItem]
    scanner_2m_nifty250: dict[str, Any] | None = None
    index_options_scanner: dict[str, Any] | None = None

    @classmethod
    def from_json(cls, path: Path) -> "StrategyConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        risk = RiskConfig(**raw["risk"])
        contract_selection = ContractSelectionConfig(**raw.get("contract_selection", {}))
        event_blackouts = [
            EventBlackoutWindow(**window)
            for window in raw.get("session_rules", {}).get("event_blackout_windows", [])
        ]
        session_rules = SessionRulesConfig(
            **{
                **raw.get("session_rules", {}),
                "event_blackout_windows": event_blackouts,
            }
        )
        watchlist = [WatchItem(**item) for item in raw["watchlist"]]
        config = cls(
            poll_seconds=raw["poll_seconds"],
            history_lookback_days=raw["history_lookback_days"],
            default_interval=raw["default_interval"],
            min_candles_for_analysis=raw.get("min_candles_for_analysis", 60),
            capital=raw["capital"],
            risk=risk,
            contract_selection=contract_selection,
            session_rules=session_rules,
            watchlist=watchlist,
            scanner_2m_nifty250=raw.get("scanner_2m_nifty250"),
            index_options_scanner=raw.get("index_options_scanner"),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.contract_selection.min_dte < 0:
            raise ValueError("contract_selection.min_dte must be >= 0")
        if self.contract_selection.max_dte < self.contract_selection.min_dte:
            raise ValueError("contract_selection.max_dte must be >= min_dte")
        if self.contract_selection.strike_offset_steps < 0:
            raise ValueError("contract_selection.strike_offset_steps must be >= 0")

        expiry_type = self.contract_selection.expiry_type.lower()
        if expiry_type not in {"weekly", "monthly", "any"}:
            raise ValueError("contract_selection.expiry_type must be one of weekly/monthly/any")

        strike_mode = self.contract_selection.strike_mode.upper()
        if strike_mode not in {"ATM", "OTM", "ITM"}:
            raise ValueError("contract_selection.strike_mode must be one of ATM/OTM/ITM")

        expiry_behavior = self.session_rules.expiry_day_behavior.lower()
        if expiry_behavior not in {"allow", "no_new_trades", "skip_day"}:
            raise ValueError(
                "session_rules.expiry_day_behavior must be one of allow/no_new_trades/skip_day"
            )

        for field_name in ("trading_start", "trading_end", "no_new_trade_after", "square_off_time"):
            value = getattr(self.session_rules, field_name)
            self._validate_hhmm(value, f"session_rules.{field_name}")

        trade_start = self._parse_hhmm(self.session_rules.trading_start)
        trade_end = self._parse_hhmm(self.session_rules.trading_end)
        cutoff = self._parse_hhmm(self.session_rules.no_new_trade_after)
        square_off = self._parse_hhmm(self.session_rules.square_off_time)
        if trade_end <= trade_start:
            raise ValueError("session_rules.trading_end must be later than trading_start")
        if cutoff < trade_start or cutoff > trade_end:
            raise ValueError("session_rules.no_new_trade_after must be within trading window")
        if square_off < trade_start or square_off > trade_end:
            raise ValueError("session_rules.square_off_time must be within trading window")

        for index, window in enumerate(self.session_rules.event_blackout_windows or []):
            self._validate_hhmm(window.start, f"session_rules.event_blackout_windows[{index}].start")
            self._validate_hhmm(window.end, f"session_rules.event_blackout_windows[{index}].end")
            if self._parse_hhmm(window.end) <= self._parse_hhmm(window.start):
                raise ValueError(
                    f"session_rules.event_blackout_windows[{index}] end must be later than start"
                )

    @staticmethod
    def _parse_hhmm(value: str) -> datetime:
        return datetime.strptime(value, "%H:%M")

    @classmethod
    def _validate_hhmm(cls, value: str, field_name: str) -> None:
        try:
            cls._parse_hhmm(value)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be in HH:MM 24-hour format") from exc


@dataclass
class AppSettings:
    zerodha_api_key: str
    zerodha_api_secret: str
    zerodha_access_token: str
    zerodha_token_file: str
    fyers_client_id: str
    fyers_secret_key: str
    fyers_redirect_uri: str
    fyers_access_token: str
    fyers_token_file: str
    fyers_data_base_url: str
    fyers_auth_base_url: str
    market_data_provider: str
    default_exchange: str
    default_product: str
    default_variety: str
    default_order_type: str
    capital: float
    log_level: str

    @classmethod
    def from_env(cls) -> "AppSettings":
        return cls(
            zerodha_api_key=os.getenv("ZERODHA_API_KEY", ""),
            zerodha_api_secret=os.getenv("ZERODHA_API_SECRET", ""),
            zerodha_access_token=os.getenv("ZERODHA_ACCESS_TOKEN", ""),
            zerodha_token_file=os.getenv("ZERODHA_TOKEN_FILE", "access_token.txt"),
            fyers_client_id=os.getenv("FYERS_CLIENT_ID", ""),
            fyers_secret_key=os.getenv("FYERS_SECRET_KEY", ""),
            fyers_redirect_uri=os.getenv("FYERS_REDIRECT_URI", ""),
            fyers_access_token=os.getenv("FYERS_ACCESS_TOKEN", ""),
            fyers_token_file=os.getenv("FYERS_TOKEN_FILE", "fyers_access_token.txt"),
            fyers_data_base_url=os.getenv("FYERS_DATA_BASE_URL", "https://api-t1.fyers.in/data").rstrip("/"),
            fyers_auth_base_url=os.getenv("FYERS_AUTH_BASE_URL", "https://api-t1.fyers.in/api/v3").rstrip("/"),
            market_data_provider=os.getenv("MARKET_DATA_PROVIDER", "auto").lower().strip(),
            default_exchange=os.getenv("ZERODHA_DEFAULT_EXCHANGE", "NSE"),
            default_product=os.getenv("ZERODHA_DEFAULT_PRODUCT", "MIS"),
            default_variety=os.getenv("ZERODHA_DEFAULT_VARIETY", "regular"),
            default_order_type=os.getenv("ZERODHA_DEFAULT_ORDER_TYPE", "MARKET"),
            capital=float(os.getenv("CAPITAL", "100000")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )

    def require_live_credentials(self) -> None:
        required: dict[str, Any] = {
            "ZERODHA_API_KEY": self.zerodha_api_key,
            "ZERODHA_API_SECRET": self.zerodha_api_secret,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            joined = ", ".join(missing)
            raise ValueError(f"Missing required live-trading settings: {joined}")
