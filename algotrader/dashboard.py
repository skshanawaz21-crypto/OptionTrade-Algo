from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pandas as pd
from kiteconnect import KiteConnect
from kiteconnect.exceptions import InputException, PermissionException, TokenException
import yfinance as yf

from algotrader.config import AppSettings, WatchItem
from algotrader.brokers.factory import create_broker
from algotrader.brokers.fyers import FyersBroker
from algotrader.marketdata import completed_intraday_candles
from algotrader.nifty250_strategy import (
    POPULAR_NSE_SYMBOLS,
    fetch_nifty250_symbols,
    scan_engulfing,
    scan_nifty250_2m,
    signal_to_dict,
)
from algotrader.strategy import analyze_market


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "strategy.v1.nifty250_scanner_options.json"
DEFAULT_LOG = ROOT / "logs.txt"
DEFAULT_STATE = ROOT / "data" / "paper_state.json"
DEFAULT_COMMANDS = ROOT / "data" / "engine_commands.jsonl"
LOG_ARCHIVE_DIR = ROOT / "data" / "log_archive"
IST = ZoneInfo("Asia/Kolkata")
LIVE_TICK_POLL_MS = 1000
KITE_HEALTH_TIMEOUT_SEC = 6
KITE_QUOTE_TIMEOUT_SEC = 6
BROKER_HEALTH_CACHE_TTL = timedelta(hours=1)
INDEX_TICKERS = [
    {"label": "NIFTY 50", "exchange": "NSE", "tradingsymbol": "NIFTY 50"},
    {"label": "NIFTY BANK", "exchange": "NSE", "tradingsymbol": "NIFTY BANK"},
    {"label": "SENSEX", "exchange": "BSE", "tradingsymbol": "SENSEX"},
]


def _now_ist_iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def _parse_state_datetime(raw_value: Any) -> datetime | None:
    raw = str(raw_value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Older paper_state entries were written as naive UTC datetimes.
        parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
    return parsed.astimezone(IST)


def _format_ist(raw_value: Any) -> str:
    parsed = _parse_state_datetime(raw_value)
    if parsed is None:
        return str(raw_value or "-")
    return parsed.strftime("%Y-%m-%d %H:%M:%S IST")


def _scan_nifty250_with_timeout(max_symbols: int, min_score: float, timeout_sec: int = 8):
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(scan_nifty250_2m, max_symbols=max_symbols, min_score=min_score)
        return future.result(timeout=timeout_sec)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _scan_engulfing_with_timeout(
    universe: str,
    interval: str,
    max_symbols: int,
    min_score: float,
    timeout_sec: int = 12,
):
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(
            scan_engulfing,
            universe=universe,
            interval=interval,
            max_symbols=max_symbols,
            min_score=min_score,
        )
        return future.result(timeout=timeout_sec)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _call_with_timeout(func, timeout_sec: int = 4):
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(func)
        return future.result(timeout=timeout_sec)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _python_executable() -> str:
    venv_python = ROOT / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def _extract_request_token(raw_input: str) -> str:
    text = raw_input.strip()
    if not text:
        raise ValueError("Paste the full redirected URL or request_token first.")
    if "request_token=" not in text:
        return text
    parsed = urlparse(text)
    token = parse_qs(parsed.query).get("request_token", [""])[0].strip()
    if not token:
        raise ValueError("Could not extract request_token from the pasted URL.")
    return token


@dataclass
class EngineState:
    process: subprocess.Popen[str] | None = None
    log_path: Path = DEFAULT_LOG
    config_path: Path = DEFAULT_CONFIG
    mode: str = "paper"
    started_at: str | None = None
    last_exit_code: int | None = None


class EngineSupervisor:
    def __init__(self) -> None:
        self.state = EngineState()
        self._lock = threading.RLock()
        self._symbol_catalog_cache: list[str] = []
        self._symbol_catalog_loaded_at: datetime | None = None
        self._broker_health_cache: dict[str, Any] | None = None
        self._broker_health_checked_at: datetime | None = None
        self._price_cache: dict[str, tuple[datetime, float | None]] = {}
        self._underlying_quote_cache: dict[str, tuple[datetime, dict[str, Any] | None]] = {}
        self._index_tick_cache: tuple[datetime, list[dict[str, Any]]] | None = None
        self._index_scanner_cache: tuple[datetime, list[dict[str, Any]]] | None = None

    def start(self, config_path: str | None = None, mode: str = "paper") -> dict[str, Any]:
        with self._lock:
            if self._is_running():
                return self._base_status()

            self.state.config_path = Path(config_path) if config_path else DEFAULT_CONFIG
            self.state.mode = mode
            self.state.log_path.parent.mkdir(parents=True, exist_ok=True)
            command = [
                _python_executable(),
                str(ROOT / "main.py"),
                "--config",
                str(self.state.config_path),
                "--mode",
                self.state.mode,
            ]

            with self.state.log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(
                    f"\n=== Engine start requested at {_now_ist_iso()} "
                    f"| mode={self.state.mode} | config={self.state.config_path} ===\n"
                )

            stdout_handle = self.state.log_path.open("a", encoding="utf-8")
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            child_env = {
                **os.environ,
                "OPTIONTRADER_DASHBOARD_PID": str(os.getpid()),
            }
            self.state.process = subprocess.Popen(
                command,
                cwd=ROOT,
                stdout=stdout_handle,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=creationflags,
                env=child_env,
            )
            self.state.started_at = _now_ist_iso()
            self.state.last_exit_code = None
            return self._base_status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            process = self.state.process
            if not process or process.poll() is not None:
                self.state.last_exit_code = None if not process else process.poll()
                self.state.process = None
                return self._base_status()

            self._stop_process_tree(process)

            self.state.last_exit_code = process.returncode
            self.state.process = None

            with self.state.log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(
                    f"=== Engine stop requested at {_now_ist_iso()} "
                    f"| exit_code={self.state.last_exit_code} ===\n"
                )

            return self._base_status()

    def _base_status(self) -> dict[str, Any]:
        process = self.state.process
        running = self._is_running()
        pid = process.pid if process and running else None
        exit_code = self.state.last_exit_code
        if process and not running:
            exit_code = process.poll()
            self.state.last_exit_code = exit_code
            self.state.process = None
        return {
            "running": running,
            "pid": pid,
            "mode": self.state.mode,
            "config_path": str(self.state.config_path),
            "started_at": self.state.started_at,
            "last_exit_code": exit_code,
            "log_path": str(self.state.log_path),
        }

    def _stop_process_tree(self, process: subprocess.Popen[str]) -> None:
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=10,
                )
                process.wait(timeout=5)
                return
            except Exception:
                pass

        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def new_session(self) -> dict[str, Any]:
        with self._lock:
            if self._is_running():
                raise RuntimeError("Stop the engine before starting a new session.")

            LOG_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            if self.state.log_path.exists():
                timestamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
                archive_path = LOG_ARCHIVE_DIR / f"logs_{timestamp}.txt"
                content = self.state.log_path.read_text(encoding="utf-8", errors="replace")
                archive_path.write_text(content, encoding="utf-8")

            self.state.log_path.write_text("", encoding="utf-8")
            if DEFAULT_STATE.exists():
                timestamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
                state_archive = LOG_ARCHIVE_DIR / f"paper_state_{timestamp}.json"
                state_archive.write_text(
                    DEFAULT_STATE.read_text(encoding="utf-8", errors="replace"),
                    encoding="utf-8",
                )
                DEFAULT_STATE.unlink()
            if DEFAULT_COMMANDS.exists():
                DEFAULT_COMMANDS.write_text("", encoding="utf-8")
            self.state.last_exit_code = None
            self.state.started_at = None
            return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            process = self.state.process
            running = self._is_running()
            pid = process.pid if process and running else None
            exit_code = None
            if process and not running:
                exit_code = process.poll()
                self.state.last_exit_code = exit_code
                self.state.process = None
            elif not process:
                exit_code = self.state.last_exit_code

            broker_health = self.broker_health_status(quick=True)
            active_trades = self.active_paper_trades(
                broker_health=broker_health,
                refresh_quotes=False,
            )
            return {
                **self._base_status(),
                "log_tail": self.tail(),
                "candle_progress": self.candle_progress(),
                "paper_trades": self.paper_trades(),
                "active_paper_trades": active_trades,
                "completed_paper_trades": self.completed_paper_trades(),
                "paper_account": self.paper_account_summary(active_trades=active_trades),
                "signal_quality": self.signal_quality_summary("all"),
                "signal_quality_by_period": {
                    "today": self.signal_quality_summary("today"),
                    "last3d": self.signal_quality_summary("last3d"),
                    "all": self.signal_quality_summary("all"),
                },
                "strategy_leaderboard": self.strategy_leaderboard(),
                "index_options_scanner": self.index_options_scanner_status(),
                "market_session": self.market_session_status(),
                "broker_health": broker_health,
                "zerodha_token": self.zerodha_token_status(broker_health=broker_health),
                "watchlist": self.watchlist_items(),
            }

    def live_ticks(self) -> dict[str, Any]:
        with self._lock:
            broker_health = self.broker_health_status()
            active_trades = self.active_paper_trades(broker_health=broker_health)
            return {
                "updated_at": _now_ist_iso(),
                "poll_ms": LIVE_TICK_POLL_MS,
                "indices": self.index_ticks(),
                "index_options_scanner": self.index_options_scanner_status(),
                "active_paper_trades": active_trades,
            }

    def zerodha_token_status(self, broker_health: dict[str, Any] | None = None) -> dict[str, Any]:
        settings = AppSettings.from_env()
        login_url = ""
        if settings.zerodha_api_key:
            try:
                login_url = KiteConnect(api_key=settings.zerodha_api_key).login_url()
            except Exception:
                login_url = ""

        token = self._current_zerodha_access_token(settings)
        token_present = bool(token)
        if not settings.zerodha_api_key:
            return {
                "status": "not_configured",
                "label": "Token Setup Needed",
                "button_label": "Token Setup Needed",
                "needs_refresh": True,
                "token_present": token_present,
                "login_url": login_url,
                "message": "ZERODHA_API_KEY is missing in .env.",
                "updated_at": _now_ist_iso(),
            }
        if not settings.zerodha_api_secret:
            return {
                "status": "not_configured",
                "label": "Token Setup Needed",
                "button_label": "Token Setup Needed",
                "needs_refresh": True,
                "token_present": token_present,
                "login_url": login_url,
                "message": "ZERODHA_API_SECRET is missing in .env.",
                "updated_at": _now_ist_iso(),
            }
        if not token_present:
            return {
                "status": "required",
                "label": "Token Required",
                "button_label": "Token Required",
                "needs_refresh": True,
                "token_present": False,
                "login_url": login_url,
                "message": "No Zerodha access token found. Open the login URL and paste the redirected URL here.",
                "updated_at": _now_ist_iso(),
            }

        health = broker_health or self.broker_health_status()
        health_status = str(health.get("status", "")).lower()
        health_message = str(health.get("message", "")).strip()
        if health_status == "checking":
            return {
                "status": "present",
                "label": "Token Present",
                "button_label": "Token Present",
                "needs_refresh": False,
                "token_present": True,
                "login_url": login_url,
                "message": "Token is saved. Live validation runs separately and will only warn on a real invalid token.",
                "updated_at": _now_ist_iso(),
            }
        if health_status == "ok":
            return {
                "status": "active",
                "label": "Token Active",
                "button_label": "Token Active",
                "needs_refresh": False,
                "token_present": True,
                "login_url": login_url,
                "message": "Zerodha token is active and Kite LTP is working.",
                "updated_at": _now_ist_iso(),
            }
        if health_status == "invalid":
            return {
                "status": "expired",
                "label": "Token Expired",
                "button_label": "Token Expired",
                "needs_refresh": True,
                "token_present": True,
                "login_url": login_url,
                "message": health_message or "Zerodha token is invalid or expired. Refresh token from the login URL.",
                "updated_at": _now_ist_iso(),
            }
        if health_status == "quotes_blocked":
            return {
                "status": "quotes_blocked",
                "label": "Quotes Blocked",
                "button_label": "Quotes Blocked",
                "needs_refresh": True,
                "token_present": True,
                "login_url": login_url,
                "message": health_message or "Token login works, but quote/LTP access is blocked.",
                "updated_at": _now_ist_iso(),
            }
        return {
            "status": "unknown",
            "label": "Token Present",
            "button_label": "Token Present",
            "needs_refresh": False,
            "token_present": True,
            "login_url": login_url,
            "message": health_message or "Token is saved. Use Refresh Token only if live data stops.",
            "updated_at": _now_ist_iso(),
        }

    def refresh_zerodha_token(self, raw_input: str) -> dict[str, Any]:
        with self._lock:
            settings = AppSettings.from_env()
            if not settings.zerodha_api_key or not settings.zerodha_api_secret:
                raise RuntimeError("ZERODHA_API_KEY and ZERODHA_API_SECRET must be set in .env.")
            request_token = _extract_request_token(raw_input)
            kite = KiteConnect(api_key=settings.zerodha_api_key)
            try:
                session = kite.generate_session(
                    request_token,
                    api_secret=settings.zerodha_api_secret,
                )
            except (InputException, TokenException) as exc:
                health = self.broker_health_status(quick=False)
                if health.get("status") == "ok":
                    return {
                        "ok": True,
                        "engine_restarted": False,
                        "token_status": self.zerodha_token_status(broker_health=health),
                        "message": (
                            "Zerodha token is already active. The pasted request_token may have "
                            f"already been used: {exc}"
                        ),
                    }
                raise
            access_token = str(session.get("access_token", "")).strip()
            if not access_token:
                raise RuntimeError("Zerodha did not return an access token.")

            token_path = self._zerodha_token_path(settings)
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(access_token, encoding="utf-8")
            os.environ["ZERODHA_ACCESS_TOKEN"] = access_token
            self._update_env_value("ZERODHA_ACCESS_TOKEN", access_token)
            self._broker_health_cache = None
            self._broker_health_checked_at = None
            self._price_cache.clear()
            self._index_tick_cache = None

            engine_restarted = False
            if self._is_running():
                config_path = str(self.state.config_path)
                mode = self.state.mode
                self.stop()
                self.start(config_path=config_path, mode=mode)
                engine_restarted = True

            return {
                "ok": True,
                "engine_restarted": engine_restarted,
                "token_status": {
                    "status": "active",
                    "label": "Token Active",
                    "button_label": "Token Active",
                    "needs_refresh": False,
                    "token_present": True,
                    "login_url": KiteConnect(api_key=settings.zerodha_api_key).login_url(),
                    "message": "Zerodha token refreshed. Broker validation will run at most once per hour.",
                    "updated_at": _now_ist_iso(),
                },
                "message": "Zerodha access token refreshed successfully.",
            }

    def _current_zerodha_access_token(self, settings: AppSettings) -> str:
        token = settings.zerodha_access_token.strip()
        if token:
            return token
        token_path = self._zerodha_token_path(settings)
        if token_path.exists():
            return token_path.read_text(encoding="utf-8", errors="replace").strip()
        return ""

    def _zerodha_token_path(self, settings: AppSettings) -> Path:
        token_path = Path(settings.zerodha_token_file)
        if not token_path.is_absolute():
            token_path = ROOT / token_path
        return token_path

    def _update_env_value(self, key: str, value: str) -> None:
        env_path = ROOT / ".env"
        if not env_path.exists():
            return
        lines = env_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        replacement = f"{key}={value}"
        updated = False
        for index, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[index] = replacement
                updated = True
                break
        if not updated:
            lines.append(replacement)
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def index_ticks(self) -> list[dict[str, Any]]:
        now = datetime.now()
        if self._index_tick_cache and (now - self._index_tick_cache[0]) < timedelta(milliseconds=850):
            return self._index_tick_cache[1]

        settings = AppSettings.from_env()
        keys = [f"{item['exchange']}:{item['tradingsymbol']}" for item in INDEX_TICKERS]
        rows: list[dict[str, Any]] = []
        try:
            kite = KiteConnect(api_key=settings.zerodha_api_key)
            token = settings.zerodha_access_token.strip()
            if not token:
                token_path = Path(settings.zerodha_token_file)
                if token_path.exists():
                    token = token_path.read_text(encoding="utf-8").strip()
            if token:
                kite.set_access_token(token)
            quotes = _call_with_timeout(lambda: kite.quote(keys), timeout_sec=KITE_QUOTE_TIMEOUT_SEC)
            for item in INDEX_TICKERS:
                key = f"{item['exchange']}:{item['tradingsymbol']}"
                payload = quotes.get(key, {}) if isinstance(quotes, dict) else {}
                last_price = self._to_float(payload.get("last_price"))
                ohlc = payload.get("ohlc", {}) if isinstance(payload.get("ohlc"), dict) else {}
                previous_close = self._to_float(ohlc.get("close"))
                change = self._to_float(payload.get("net_change"))
                if change is None and last_price is not None and previous_close:
                    change = last_price - previous_close
                change_pct = (change / previous_close) * 100.0 if change is not None and previous_close else None
                rows.append(
                    {
                        **item,
                        "key": key,
                        "last_price": last_price,
                        "change": change,
                        "change_pct": change_pct,
                        "previous_close": previous_close,
                        "open": self._to_float(ohlc.get("open")),
                        "high": self._to_float(ohlc.get("high")),
                        "low": self._to_float(ohlc.get("low")),
                        "status": "live" if payload.get("last_price") is not None else "missing",
                        "source": "kite_quote",
                        "updated_at": _now_ist_iso(),
                    }
                )
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            if self._index_tick_cache:
                rows = [
                    {
                        **row,
                        "status": "stale" if row.get("last_price") is not None else "error",
                        "error": error,
                        "updated_at": _now_ist_iso(),
                    }
                    for row in self._index_tick_cache[1]
                ]
            else:
                rows = [
                    {
                        **item,
                        "key": f"{item['exchange']}:{item['tradingsymbol']}",
                        "last_price": None,
                        "change": None,
                        "change_pct": None,
                        "previous_close": None,
                        "status": "error",
                        "source": "kite_quote",
                        "error": error,
                        "updated_at": _now_ist_iso(),
                    }
                    for item in INDEX_TICKERS
                ]

        self._index_tick_cache = (now, rows)
        return rows

    def index_options_scanner_status(self) -> list[dict[str, Any]]:
        now = datetime.now(IST)
        if self._index_scanner_cache and (now - self._index_scanner_cache[0]) < timedelta(seconds=20):
            return self._index_scanner_cache[1]

        config_path = self.state.config_path if self.state.config_path.exists() else DEFAULT_CONFIG
        try:
            raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            raw_config = {}
        raw = raw_config.get("index_options_scanner", {}) or {}
        enabled = bool(raw.get("enabled", False))
        symbols = [
            str(symbol).upper().strip()
            for symbol in raw.get("symbols", ["NIFTY", "BANKNIFTY", "SENSEX"])
            if str(symbol).strip()
        ]
        symbols = [symbol for symbol in symbols if symbol in {"NIFTY", "BANKNIFTY", "SENSEX"}]
        interval = str(raw.get("interval", "5minute"))
        min_required = int(raw_config.get("min_candles_for_analysis", 20) or 20)
        rows = []
        for symbol in symbols:
            exchange = "BSE" if symbol == "SENSEX" else "NSE"
            contract_exchange = str(
                (raw.get("contract_exchange_by_symbol", {}) or {}).get(
                    symbol,
                    "BFO" if symbol == "SENSEX" else "NFO",
                )
            ).upper()
            row = {
                "symbol": symbol,
                "exchange": exchange,
                "contract_exchange": contract_exchange,
                "interval": interval,
                "enabled": enabled,
                "status": "Disabled" if not enabled else "Waiting for candles",
                "regime": "-",
                "score": None,
                "option_side": "-",
                "close": None,
                "rsi14": None,
                "ema20": None,
                "ema50": None,
                "momentum_pct": None,
                "ema_gap_pct": None,
                "breakout": "-",
                "candles": 0,
                "required_candles": min_required,
                "last_candle": "-",
                "reason": "Scanner disabled in config." if not enabled else "No local analysis candles yet.",
            }
            if not enabled:
                rows.append(row)
                continue
            safe_symbol = symbol.replace(" ", "_")
            candle_path = ROOT / "data" / "candles" / f"{exchange}_{safe_symbol}_{interval}.csv"
            if not candle_path.exists():
                rows.append(row)
                continue
            try:
                df = pd.read_csv(candle_path)
            except Exception as exc:
                row["status"] = "Data error"
                row["reason"] = str(exc)
                rows.append(row)
                continue
            df = completed_intraday_candles(df, interval, now)
            row["candles"] = int(len(df.index))
            if not df.empty and "date" in df.columns:
                row["last_candle"] = _format_ist(df.iloc[-1].get("date"))
            if len(df.index) < min_required:
                row["reason"] = f"Need {min_required} candles before scanner can score."
                rows.append(row)
                continue
            try:
                item = WatchItem(
                    symbol=symbol,
                    exchange=exchange,
                    contract_exchange=contract_exchange,
                    instrument_type="index_option",
                    interval=interval,
                )
                snapshot = analyze_market(df, item)
                evaluation = self._index_scanner_evaluation(snapshot, raw)
            except Exception as exc:
                row["status"] = "Analysis error"
                row["reason"] = str(exc)
                rows.append(row)
                continue
            row.update(
                {
                    "regime": snapshot.regime,
                    "close": snapshot.close,
                    "rsi14": snapshot.rsi14,
                    "ema20": snapshot.ema20,
                    "ema50": snapshot.ema50,
                    "score": evaluation["score"],
                    "momentum_pct": evaluation["momentum_pct"],
                    "ema_gap_pct": evaluation["ema_gap_pct"],
                    "breakout": "UP" if snapshot.breakout_up else ("DOWN" if snapshot.breakout_down else "-"),
                }
            )
            if evaluation["entry_ready"]:
                row["status"] = "Entry-ready"
                row["option_side"] = evaluation["option_side"]
                row["reason"] = f"{evaluation['direction']} setup passed score gate."
            else:
                row["status"] = "No entry"
                row["option_side"] = evaluation["option_side"]
                row["reason"] = evaluation["reason"]
            rows.append(row)
        self._index_scanner_cache = (now, rows)
        return rows

    def _index_scanner_evaluation(self, snapshot, raw: dict[str, Any]) -> dict[str, Any]:
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
        score = 50.0
        score += min(abs(float(snapshot.rsi14) - 50.0) * 1.1, 25.0)
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

    def _index_scanner_decision(self, snapshot, raw: dict[str, Any]) -> dict[str, Any] | None:
        evaluation = self._index_scanner_evaluation(snapshot, raw)
        if not evaluation["entry_ready"]:
            return None
        return {
            "direction": evaluation["direction"],
            "option_side": evaluation["option_side"],
            "score": evaluation["score"],
        }

    @staticmethod
    def _to_float(value: object) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def tail(self, lines: int = 80) -> str:
        if not self.state.log_path.exists():
            return ""
        content = self.state.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:])

    def candle_progress(self) -> list[dict[str, Any]]:
        config_path = self.state.config_path if self.state.config_path.exists() else DEFAULT_CONFIG
        if not config_path.exists():
            return []

        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []

        target = int(raw.get("min_candles_for_analysis", 60))
        default_interval = raw.get("default_interval", "1minute")
        rows: list[dict[str, Any]] = []
        for item in raw.get("watchlist", []):
            if not item.get("enabled", True):
                continue
            symbol = item.get("symbol", "")
            exchange = item.get("exchange", "NSE")
            interval = item.get("interval", default_interval)
            safe_symbol = symbol.replace(" ", "_")
            candle_path = ROOT / "data" / "candles" / f"{exchange}_{safe_symbol}_{interval}.csv"
            count = 0
            last_candle = None
            if candle_path.exists():
                try:
                    df = pd.read_csv(candle_path)
                    count = len(df.index)
                    if count:
                        last_candle = str(df.iloc[-1]["date"])
                except Exception:
                    count = 0

            rows.append(
                {
                    "symbol": symbol,
                    "exchange": exchange,
                    "interval": interval,
                    "count": count,
                    "target": target,
                    "ready": count >= target,
                    "last_candle": last_candle,
                }
            )
        return rows

    def paper_trades(self, lines: int = 200) -> list[dict[str, str]]:
        if not self.state.log_path.exists():
            return []

        trade_lines: list[dict[str, str]] = []
        content = self.state.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in reversed(content[-lines:]):
            if "[PAPER]" not in line and "Entered " not in line and "Exited " not in line:
                continue
            parts = line.split(" | ", 2)
            timestamp = parts[0] if parts else ""
            message = parts[-1] if parts else line
            trade_lines.append({"timestamp": timestamp, "message": message})
            if len(trade_lines) >= 12:
                break
        trade_lines.reverse()
        return trade_lines

    def active_paper_trades(
        self,
        broker_health: dict[str, Any] | None = None,
        refresh_quotes: bool = True,
    ) -> list[dict[str, Any]]:
        if not DEFAULT_STATE.exists():
            return []
        try:
            raw = json.loads(DEFAULT_STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        trades: list[dict[str, Any]] = []
        quotes_blocked = (broker_health or {}).get("status") == "quotes_blocked"
        for trade in raw.get("open_trades", []):
            instrument_type = str(trade.get("instrument_type", "")).lower()
            needs_broker_quote = instrument_type in {
                "index_option",
                "stock_option",
                "index_future",
                "stock_future",
            }
            current_price = None
            quote_status = "state"
            if quotes_blocked and needs_broker_quote:
                quote_status = "unavailable"
            elif refresh_quotes:
                current_price = self._latest_price_for_trade(trade)
                quote_status = "live" if current_price is not None else "unavailable"
            else:
                cached_price = self._cached_latest_price_for_trade(trade)
                raw_price = trade.get("current_price")
                current_price = (
                    cached_price
                    if cached_price is not None
                    else (float(raw_price) if raw_price is not None else None)
                )
                if cached_price is not None:
                    quote_status = "live"
            entry_price = float(trade.get("entry_price", 0.0))
            quantity = int(trade.get("quantity", 0))
            direction = trade.get("direction", "BUY")
            unrealized_pnl = None
            if current_price is not None:
                unrealized_pnl = (
                    (current_price - entry_price) * quantity
                    if direction == "BUY"
                    else (entry_price - current_price) * quantity
                )
            trade_copy = dict(trade)
            trade_copy["current_price"] = current_price
            trade_copy["unrealized_pnl"] = unrealized_pnl
            trade_copy["quote_status"] = quote_status
            trade_copy["position_value"] = abs(entry_price * quantity)
            trade_copy["opened_at"] = _format_ist(trade.get("opened_at"))
            trade_copy["underlying_quote"] = (
                self._underlying_quote_for_trade(trade)
                if refresh_quotes
                else self._cached_underlying_quote_for_trade(trade)
            )
            trades.append(trade_copy)
        return trades

    def completed_paper_trades(self) -> list[dict[str, Any]]:
        if not DEFAULT_STATE.exists():
            return []
        try:
            raw = json.loads(DEFAULT_STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        trades = []
        for trade in list(raw.get("closed_trades", []))[-20:][::-1]:
            trade_copy = dict(trade)
            trade_copy["opened_at"] = _format_ist(trade.get("opened_at"))
            trade_copy["closed_at"] = _format_ist(trade.get("closed_at"))
            trades.append(trade_copy)
        return trades

    def watchlist_items(self) -> list[dict[str, Any]]:
        config_path = self.state.config_path if self.state.config_path.exists() else DEFAULT_CONFIG
        if not config_path.exists():
            return []
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        return list(raw.get("watchlist", []))

    def add_watch_item(self, symbol: str, exchange: str = "NSE", interval: str | None = None) -> dict[str, Any]:
        config_path = self.state.config_path if self.state.config_path.exists() else DEFAULT_CONFIG
        if not config_path.exists():
            raise RuntimeError("Strategy config file was not found.")

        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol:
            raise RuntimeError("Please enter an underlying symbol.")
        if not self._is_valid_symbol(normalized_symbol):
            raise RuntimeError(
                f"{normalized_symbol} is not recognized as a tradable NSE/NFO underlying. "
                "Please choose from dropdown suggestions."
            )

        raw = json.loads(config_path.read_text(encoding="utf-8"))
        watchlist = list(raw.get("watchlist", []))
        if any(str(item.get("symbol", "")).upper() == normalized_symbol for item in watchlist):
            raise RuntimeError(f"{normalized_symbol} is already in the watchlist.")
        index_symbols = {"NIFTY", "NIFTY50", "NIFTY 50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"}

        watchlist.append(
            {
                "symbol": normalized_symbol,
                "exchange": exchange,
                "contract_exchange": "NFO",
                "instrument_type": "index_option" if normalized_symbol in index_symbols else "stock_option",
                "interval": interval or raw.get("default_interval", "5minute"),
                "enabled": True,
                "option_side": "auto",
                "option_type": "call",
                "option_expiry_hint": "",
                "option_days_to_expiry": 7,
                "option_iv_min": 0.05,
                "option_iv_max": 1.5,
                "option_delta_min": 0.2,
                "option_delta_max": 0.7,
                "risk_free_rate": 0.07,
            }
        )
        raw["watchlist"] = watchlist
        config_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        return self.status()

    def symbol_suggestions(self, query: str, limit: int = 20) -> list[str]:
        q = query.strip().upper()
        if not q:
            return []
        catalog = self._get_symbol_catalog()
        starts = [sym for sym in catalog if sym.startswith(q)]
        contains = [sym for sym in catalog if q in sym and sym not in starts]
        return (starts + contains)[: max(limit, 1)]

    def remove_watch_item(self, symbol: str) -> dict[str, Any]:
        config_path = self.state.config_path if self.state.config_path.exists() else DEFAULT_CONFIG
        if not config_path.exists():
            raise RuntimeError("Strategy config file was not found.")
        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol:
            raise RuntimeError("Please provide a symbol to remove.")
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        watchlist = list(raw.get("watchlist", []))
        filtered = [
            item for item in watchlist if str(item.get("symbol", "")).upper() != normalized_symbol
        ]
        if len(filtered) == len(watchlist):
            raise RuntimeError(f"{normalized_symbol} was not found in watchlist.")
        raw["watchlist"] = filtered
        config_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        return self.status()

    def signal_quality_summary(self, period: str = "all") -> dict[str, Any]:
        trades = self._load_closed_trades()
        trades = self._filter_closed_trades_by_period(trades, period)
        if not trades:
            return {
                "period": period,
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate_pct": 0.0,
                "avg_net_pnl": 0.0,
                "profit_factor": 0.0,
            }
        pnls = [float(trade.get("pnl", 0.0)) for trade in trades]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        total_profit = sum(wins)
        total_loss = abs(sum(losses))
        profit_factor = (total_profit / total_loss) if total_loss > 0 else 0.0
        return {
            "period": period,
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round((len(wins) / len(trades)) * 100.0, 2) if trades else 0.0,
            "avg_net_pnl": round(sum(pnls) / len(trades), 2) if trades else 0.0,
            "profit_factor": round(profit_factor, 2),
        }

    def strategy_leaderboard(self) -> list[dict[str, Any]]:
        closed_trades = self._load_closed_trades()
        active_trades = self.active_paper_trades(refresh_quotes=False)
        buckets: dict[str, dict[str, Any]] = {}

        def bucket_for(strategy_id: str) -> dict[str, Any]:
            if strategy_id not in buckets:
                buckets[strategy_id] = {
                    "strategy_id": strategy_id,
                    "net_pnl": 0.0,
                    "closed_trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "live_entries": 0,
                    "gross_profit": 0.0,
                    "gross_loss": 0.0,
                    "symbols": {},
                }
            return buckets[strategy_id]

        for trade in closed_trades:
            strategy_id = self._strategy_id_for_trade(trade)
            bucket = bucket_for(strategy_id)
            pnl = float(trade.get("pnl", 0.0) or 0.0)
            symbol = str(trade.get("symbol", "-") or "-")
            bucket["net_pnl"] += pnl
            bucket["closed_trades"] += 1
            if pnl > 0:
                bucket["wins"] += 1
                bucket["gross_profit"] += pnl
            elif pnl < 0:
                bucket["losses"] += 1
                bucket["gross_loss"] += abs(pnl)
            symbol_stats = bucket["symbols"].setdefault(symbol, {"pnl": 0.0, "trades": 0})
            symbol_stats["pnl"] += pnl
            symbol_stats["trades"] += 1

        for trade in active_trades:
            strategy_id = self._strategy_id_for_trade(trade)
            bucket_for(strategy_id)["live_entries"] += 1

        rows: list[dict[str, Any]] = []
        for bucket in buckets.values():
            closed_count = int(bucket["closed_trades"])
            wins = int(bucket["wins"])
            losses = int(bucket["losses"])
            gross_profit = float(bucket["gross_profit"])
            gross_loss = float(bucket["gross_loss"])
            profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0)
            win_rate = (wins / closed_count) * 100.0 if closed_count else 0.0
            avg_pnl = float(bucket["net_pnl"]) / closed_count if closed_count else 0.0
            symbols = bucket.pop("symbols", {})
            best_symbol = "-"
            worst_symbol = "-"
            if symbols:
                best_symbol = max(symbols.items(), key=lambda item: item[1]["pnl"])[0]
                worst_symbol = min(symbols.items(), key=lambda item: item[1]["pnl"])[0]
            score = (
                float(bucket["net_pnl"])
                + (win_rate * 10.0)
                + (profit_factor * 100.0)
                + (int(bucket["live_entries"]) * 25.0)
            )
            rows.append(
                {
                    **bucket,
                    "net_pnl": round(float(bucket["net_pnl"]), 2),
                    "win_rate_pct": round(win_rate, 2),
                    "avg_net_pnl": round(avg_pnl, 2),
                    "profit_factor": round(profit_factor, 2),
                    "score": round(score, 3),
                    "best_symbol": best_symbol,
                    "worst_symbol": worst_symbol,
                }
            )
        return sorted(rows, key=lambda row: (float(row.get("score", 0.0)), float(row.get("net_pnl", 0.0))), reverse=True)

    def _strategy_id_for_trade(self, trade: dict[str, Any]) -> str:
        entry_reason = str(trade.get("entry_reason", "") or "").lower()
        instrument_type = str(trade.get("instrument_type", "") or "").lower()
        option_side = str(trade.get("option_side", "") or "").upper()
        direction = str(trade.get("direction", "") or "").upper()
        if "nifty250_2m_scanner" in entry_reason:
            return "NIFTY250 2m Engulfing Scanner"
        if "index_options_scanner" in entry_reason:
            return "Index Options Scanner"
        if instrument_type == "stock_option":
            side = option_side or direction or "OPTION"
            return f"Watchlist Directional - Stock {side}"
        if instrument_type == "index_option":
            side = option_side or direction or "OPTION"
            return f"Watchlist Directional - Index {side}"
        if instrument_type in {"stock_future", "index_future"}:
            return f"Watchlist Directional - {instrument_type.replace('_', ' ').title()}"
        return f"Watchlist Directional - {instrument_type.replace('_', ' ').title() or 'Unknown'}"

    def market_session_status(self) -> dict[str, Any]:
        now = datetime.now(IST)
        open_time = now.replace(hour=9, minute=15, second=0, microsecond=0)
        close_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
        is_open = open_time <= now <= close_time
        return {
            "is_open": is_open,
            "label": "Market Open" if is_open else "Market Closed",
            "open_time": "09:15",
            "close_time": "15:30",
            "current_time": now.strftime("%H:%M:%S"),
        }

    def paper_account_summary(self, active_trades: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        starting_capital = 0.0
        if self.state.config_path.exists():
            try:
                raw_config = json.loads(self.state.config_path.read_text(encoding="utf-8"))
                starting_capital = float(raw_config.get("capital", 0.0))
            except (json.JSONDecodeError, ValueError, TypeError):
                starting_capital = 0.0

        if not DEFAULT_STATE.exists():
            return {
                "starting_capital": starting_capital,
                "realized_pnl": 0.0,
                "available_balance": starting_capital,
                "capital_committed": 0.0,
                "unrealized_pnl": 0.0,
                "equity": starting_capital,
                "open_positions": 0,
            }

        try:
            raw = json.loads(DEFAULT_STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {
                "starting_capital": starting_capital,
                "realized_pnl": 0.0,
                "available_balance": starting_capital,
                "capital_committed": 0.0,
                "unrealized_pnl": 0.0,
                "equity": starting_capital,
                "open_positions": 0,
            }

        account = raw.get("account", {})
        open_trades = raw.get("open_trades", [])
        active_trades = active_trades if active_trades is not None else self.active_paper_trades()
        account_start = float(account.get("starting_capital", starting_capital or 0.0))
        realized_pnl = float(account.get("realized_pnl", 0.0))
        capital_committed = float(
            account.get(
                "capital_committed",
                sum(float(trade.get("position_value", 0.0)) for trade in active_trades),
            )
        )
        unrealized_pnl = sum(
            float(trade.get("unrealized_pnl") or 0.0)
            for trade in active_trades
        )
        available_balance = float(
            account.get("available_balance", account_start + realized_pnl - capital_committed)
        )
        return {
            "starting_capital": account_start,
            "realized_pnl": realized_pnl,
            "available_balance": available_balance,
            "capital_committed": capital_committed,
            "unrealized_pnl": unrealized_pnl,
            "equity": account_start + realized_pnl + unrealized_pnl,
            "open_positions": len(open_trades),
        }

    def manual_exit(self, symbol: str, tradingsymbol: str = "") -> dict[str, Any]:
        normalized = symbol.strip().upper()
        normalized_ts = tradingsymbol.strip().upper()
        if not normalized and not normalized_ts:
            raise RuntimeError("Please provide a symbol or tradingsymbol for manual exit.")
        if not self._is_running():
            raise RuntimeError("Engine is not running. Start engine before requesting manual exit.")
        DEFAULT_COMMANDS.parent.mkdir(parents=True, exist_ok=True)
        command = {
            "action": "exit_trade",
            "symbol": normalized,
            "tradingsymbol": normalized_ts,
            "requested_at": _now_ist_iso(),
        }
        with DEFAULT_COMMANDS.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(command) + "\n")
        with self.state.log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(
                "=== Manual exit requested at "
                f"{_now_ist_iso()} | symbol={normalized} | tradingsymbol={normalized_ts} ===\n"
            )
        return {
            **self._base_status(),
            "queued": True,
            "message": f"Manual exit queued for {normalized_ts or normalized}.",
        }

    def _latest_price_for_trade(self, trade: dict[str, Any]) -> float | None:
        symbol = str(trade.get("symbol", ""))
        exchange = str(trade.get("exchange", "NSE"))
        tradingsymbol = str(trade.get("tradingsymbol", "")).strip()
        instrument_type = str(trade.get("instrument_type", "")).lower()
        if tradingsymbol and instrument_type in {"index_option", "stock_option", "index_future", "stock_future"}:
            cache_key = f"{exchange}:{tradingsymbol}"
            cached = self._price_cache.get(cache_key)
            now = datetime.now(IST)
            if cached and (now - cached[0]) < timedelta(milliseconds=850):
                return cached[1]
            try:
                price = _call_with_timeout(
                    lambda: create_broker(AppSettings.from_env()).get_ltp(exchange, tradingsymbol),
                    timeout_sec=KITE_QUOTE_TIMEOUT_SEC,
                )
            except Exception:
                if cached and cached[1] is not None:
                    return cached[1]
                self._price_cache[cache_key] = (now, None)
                return None
            self._price_cache[cache_key] = (now, price)
            return price
        interval = self._interval_for_symbol(symbol)
        if not symbol or not interval:
            return None
        safe_symbol = symbol.replace(" ", "_")
        candle_path = ROOT / "data" / "candles" / f"{exchange}_{safe_symbol}_{interval}.csv"
        if not candle_path.exists():
            return None
        try:
            df = pd.read_csv(candle_path)
            if df.empty:
                return None
            return float(df.iloc[-1]["close"])
        except Exception:
            return None

    def _cached_latest_price_for_trade(self, trade: dict[str, Any]) -> float | None:
        exchange = str(trade.get("exchange", "NSE"))
        tradingsymbol = str(trade.get("tradingsymbol", "")).strip()
        instrument_type = str(trade.get("instrument_type", "")).lower()
        if not tradingsymbol or instrument_type not in {"index_option", "stock_option", "index_future", "stock_future"}:
            return None
        cached = self._price_cache.get(f"{exchange}:{tradingsymbol}")
        if cached is None:
            return None
        return cached[1]

    def _underlying_quote_key_for_trade(self, trade: dict[str, Any]) -> str | None:
        symbol = str(trade.get("underlying_symbol") or trade.get("symbol") or "").strip().upper()
        exchange = str(trade.get("underlying_exchange") or "NSE").strip().upper()
        if not symbol:
            return None
        if symbol in {"NIFTY", "NIFTY50", "NIFTY 50"}:
            exchange, symbol = "NSE", "NIFTY 50"
        elif symbol in {"BANKNIFTY", "NIFTYBANK", "NIFTY BANK"}:
            exchange, symbol = "NSE", "NIFTY BANK"
        elif symbol == "SENSEX":
            exchange, symbol = "BSE", "SENSEX"
        return f"{exchange}:{symbol}"

    def _cached_underlying_quote_for_trade(self, trade: dict[str, Any]) -> dict[str, Any] | None:
        key = self._underlying_quote_key_for_trade(trade)
        if not key:
            return None
        cached = self._underlying_quote_cache.get(key)
        if cached is None:
            return None
        return cached[1]

    def _underlying_quote_for_trade(self, trade: dict[str, Any]) -> dict[str, Any] | None:
        key = self._underlying_quote_key_for_trade(trade)
        if not key:
            return None
        exchange, symbol = key.split(":", 1)
        cached = self._underlying_quote_cache.get(key)
        now = datetime.now(IST)
        if cached and (now - cached[0]) < timedelta(milliseconds=850):
            return cached[1]
        try:
            settings = AppSettings.from_env()
            kite = KiteConnect(api_key=settings.zerodha_api_key)
            token = settings.zerodha_access_token.strip()
            if not token:
                token_path = Path(settings.zerodha_token_file)
                if token_path.exists():
                    token = token_path.read_text(encoding="utf-8").strip()
            if token:
                kite.set_access_token(token)
            quotes = _call_with_timeout(lambda: kite.quote([key]), timeout_sec=KITE_QUOTE_TIMEOUT_SEC)
            payload = quotes.get(key, {}) if isinstance(quotes, dict) else {}
            last_price = self._to_float(payload.get("last_price"))
            ohlc = payload.get("ohlc", {}) if isinstance(payload.get("ohlc"), dict) else {}
            previous_close = self._to_float(ohlc.get("close"))
            change = self._to_float(payload.get("net_change"))
            if last_price is not None and previous_close:
                change = last_price - previous_close
            change_pct = (change / previous_close) * 100.0 if change is not None and previous_close else None
            quote = {
                "symbol": symbol,
                "exchange": exchange,
                "key": key,
                "last_price": last_price,
                "change": change,
                "change_pct": change_pct,
                "previous_close": previous_close,
                "status": "live" if last_price is not None else "missing",
                "updated_at": _now_ist_iso(),
            }
            self._underlying_quote_cache[key] = (now, quote)
            return quote
        except Exception:
            if cached:
                return cached[1]
            self._underlying_quote_cache[key] = (now, None)
            return None

    def _interval_for_symbol(self, symbol: str) -> str | None:
        config_path = self.state.config_path if self.state.config_path.exists() else DEFAULT_CONFIG
        if not config_path.exists():
            return None
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        default_interval = raw.get("default_interval", "1minute")
        for item in raw.get("watchlist", []):
            if item.get("symbol") == symbol:
                return item.get("interval", default_interval)
        return default_interval

    def _load_closed_trades(self) -> list[dict[str, Any]]:
        if not DEFAULT_STATE.exists():
            return []
        try:
            raw = json.loads(DEFAULT_STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        return list(raw.get("closed_trades", []))

    def _filter_closed_trades_by_period(self, trades: list[dict[str, Any]], period: str) -> list[dict[str, Any]]:
        mode = period.strip().lower()
        if mode == "all":
            return trades
        now = datetime.now(IST)
        if mode == "today":
            cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif mode == "last3d":
            cutoff = now - timedelta(days=3)
        else:
            return trades
        filtered: list[dict[str, Any]] = []
        for trade in trades:
            closed_raw = str(trade.get("closed_at", "")).strip()
            if not closed_raw:
                continue
            try:
                closed_at = _parse_state_datetime(closed_raw)
            except ValueError:
                continue
            if closed_at is None:
                continue
            if closed_at >= cutoff:
                filtered.append(trade)
        return filtered

    def _is_running(self) -> bool:
        process = self.state.process
        return process is not None and process.poll() is None

    def broker_health_status(self, quick: bool = False) -> dict[str, str]:
        now = datetime.now()
        if (
            self._broker_health_cache is not None
            and self._broker_health_checked_at is not None
            and (now - self._broker_health_checked_at) < BROKER_HEALTH_CACHE_TTL
        ):
            return self._broker_health_cache
        if quick:
            return {
                "status": "checking",
                "label": "CHECKING",
                "message": "Broker token validation runs at most once per hour.",
            }

        settings = AppSettings.from_env()
        if not settings.zerodha_api_key:
            result = {
                "status": "fallback",
                "label": "FALLBACK",
                "message": "API key missing, running in fallback market-data mode.",
            }
            self._broker_health_cache = result
            self._broker_health_checked_at = now
            return result

        token = settings.zerodha_access_token.strip()
        if not token:
            token_path = Path(settings.zerodha_token_file)
            if token_path.exists():
                token = token_path.read_text(encoding="utf-8").strip()
        if not token:
            result = {
                "status": "fallback",
                "label": "FALLBACK",
                "message": "Access token missing, running in fallback market-data mode.",
            }
            self._broker_health_cache = result
            self._broker_health_checked_at = now
            return result

        kite = KiteConnect(api_key=settings.zerodha_api_key)
        kite.set_access_token(token)
        try:
            _call_with_timeout(lambda: kite.ltp("NSE:SBIN"), timeout_sec=KITE_HEALTH_TIMEOUT_SEC)
            result = {
                "status": "ok",
                "label": "OK",
                "message": "Broker token and quote/LTP permission are valid. Next validation is due in one hour.",
            }
        except PermissionException as exc:
            result = self._fyers_quote_health(settings, zerodha_error=str(exc))
        except FuturesTimeoutError:
            result = {
                "status": "fallback",
                "label": "FALLBACK",
                "message": "Broker health check timed out; option quotes are treated as unavailable.",
            }
        except (InputException, TokenException) as exc:
            result = {
                "status": "invalid",
                "label": "INVALID",
                "message": f"Broker auth invalid: {exc}",
            }
        except Exception as exc:
            result = {
                "status": "fallback",
                "label": "FALLBACK",
                "message": f"Broker unavailable, fallback active: {exc}",
            }

        self._broker_health_cache = result
        self._broker_health_checked_at = now
        return result

    def _fyers_quote_health(self, settings: AppSettings, zerodha_error: str) -> dict[str, str]:
        if not FyersBroker.is_configured(settings):
            return {
                "status": "quotes_blocked",
                "label": "QUOTES BLOCKED",
                "message": (
                    "Zerodha login/trading auth is valid, but Kite quote/LTP permission is blocked. "
                    "Configure FYERS credentials or use a paid Kite Connect app for accurate option quotes."
                ),
            }
        try:
            price = _call_with_timeout(
                lambda: FyersBroker(settings).get_ltp("NSE", "SBIN"),
                timeout_sec=6,
            )
            return {
                "status": "fyers_ok",
                "label": "FYERS DATA",
                "message": (
                    f"Zerodha quotes are blocked ({zerodha_error}); FYERS quote fallback is active "
                    f"and returned SBIN LTP {price:.2f}."
                ),
            }
        except Exception as exc:
            return {
                "status": "quotes_blocked",
                "label": "QUOTES BLOCKED",
                "message": (
                    "Zerodha quote/LTP permission is blocked and FYERS fallback is not usable yet: "
                    f"{exc}"
                ),
            }

    def _is_valid_symbol(self, symbol: str) -> bool:
        upper = symbol.strip().upper()
        if not upper:
            return False
        if upper in set(self._get_symbol_catalog()):
            return True
        index_aliases = {
            "NIFTY",
            "NIFTY50",
            "NIFTY 50",
            "BANKNIFTY",
            "FINNIFTY",
            "MIDCPNIFTY",
            "SENSEX",
        }
        if upper in index_aliases:
            return True
        try:
            ticker = yf.Ticker(f"{upper}.NS")
            history = ticker.history(period="5d", interval="1d", auto_adjust=False)
            return not history.empty
        except Exception:
            return False

    def _get_symbol_catalog(self, refresh: bool = False) -> list[str]:
        now = datetime.now()
        if (
            not refresh
            and self._symbol_catalog_cache
            and self._symbol_catalog_loaded_at is not None
            and (now - self._symbol_catalog_loaded_at) < timedelta(hours=4)
        ):
            return self._symbol_catalog_cache

        symbols: set[str] = {
            "NIFTY",
            "BANKNIFTY",
            "FINNIFTY",
            "MIDCPNIFTY",
            "SENSEX",
        }
        symbols.update(POPULAR_NSE_SYMBOLS)
        try:
            symbols.update(fetch_nifty250_symbols(timeout_sec=4))
        except Exception:
            pass
        for item in self.watchlist_items():
            sym = str(item.get("symbol", "")).strip().upper()
            if sym:
                symbols.add(sym)

        try:
            settings = AppSettings.from_env()
            if settings.zerodha_api_key:
                kite = KiteConnect(api_key=settings.zerodha_api_key)
                token = settings.zerodha_access_token.strip()
                if not token:
                    token_path = Path(settings.zerodha_token_file)
                    if token_path.exists():
                        token = token_path.read_text(encoding="utf-8").strip()
                if token:
                    kite.set_access_token(token)

                nse_instruments = kite.instruments("NSE")
                for row in nse_instruments:
                    tradingsymbol = str(row.get("tradingsymbol", "")).strip().upper()
                    if tradingsymbol and tradingsymbol.isascii() and " " not in tradingsymbol:
                        symbols.add(tradingsymbol)

                nfo_instruments = kite.instruments("NFO")
                for row in nfo_instruments:
                    if str(row.get("segment", "")).upper() != "NFO-OPT":
                        continue
                    name = str(row.get("name", "")).strip().upper()
                    if name:
                        symbols.add(name)
        except Exception:
            pass

        self._symbol_catalog_cache = sorted(symbols)
        self._symbol_catalog_loaded_at = now
        return self._symbol_catalog_cache


SUPERVISOR = EngineSupervisor()

HTML_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OptionTrader Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #070b14;
      --bg-elev: #0f1729;
      --panel: rgba(16, 24, 41, 0.78);
      --panel-2: rgba(13, 20, 35, 0.92);
      --ink: #e5edf9;
      --muted: #8ca2c6;
      --accent: #18e0d0;
      --accent-2: #ffb454;
      --danger: #ff5f7a;
      --border: rgba(85, 110, 162, 0.45);
      --glow: 0 0 0 1px rgba(24, 224, 208, 0.14), 0 14px 44px rgba(3, 8, 18, 0.62);
      --radius: 16px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Space Grotesk", "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(1200px 600px at 8% -10%, rgba(24, 224, 208, 0.16), transparent 60%),
        radial-gradient(900px 520px at 100% 5%, rgba(255, 180, 84, 0.16), transparent 62%),
        linear-gradient(145deg, #050812, #0a1120 45%, #0e1528);
      min-height: 100vh;
    }
    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background-image:
        linear-gradient(rgba(128, 158, 216, 0.09) 1px, transparent 1px),
        linear-gradient(90deg, rgba(128, 158, 216, 0.07) 1px, transparent 1px);
      background-size: 54px 54px, 54px 54px;
      opacity: 0.22;
      z-index: -1;
    }
    .wrap {
      max-width: 1400px;
      margin: 0 auto;
      padding: 26px 22px 54px;
    }
    .hero {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(320px, 430px);
      gap: 16px;
      margin-bottom: 22px;
      align-items: start;
    }
    .hero-copy {
      min-width: 0;
    }
    h1 {
      margin: 0;
      font-size: 40px;
      line-height: 1;
      letter-spacing: 0.01em;
      color: #f2f8ff;
      text-shadow: 0 8px 32px rgba(5, 9, 18, 0.45);
    }
    .subtitle {
      margin: 0;
      color: var(--muted);
      max-width: 880px;
      font-size: 16px;
    }
    .token-card {
      justify-self: end;
      width: 100%;
      max-width: 430px;
      display: grid;
      gap: 10px;
    }
    .token-status-button {
      justify-self: end;
      width: auto;
      min-width: 190px;
      border: 1px solid rgba(85, 110, 162, 0.45);
      border-radius: 999px;
      padding: 11px 16px;
      cursor: pointer;
      font-weight: 900;
      letter-spacing: 0.02em;
      color: #06131d;
      box-shadow: 0 10px 30px rgba(3, 8, 18, 0.24);
    }
    .token-status-button.token-active {
      background: linear-gradient(135deg, #18e0d0, #9ff7dd);
    }
    .token-status-button.token-expired,
    .token-status-button.token-required,
    .token-status-button.token-not_configured {
      background: linear-gradient(135deg, #ff5f7a, #ffd0d8);
    }
    .token-status-button.token-quotes_blocked,
    .token-status-button.token-unknown {
      background: linear-gradient(135deg, #ffb454, #ffe0ae);
    }
    .token-panel {
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 13px;
      background: linear-gradient(180deg, rgba(22, 32, 52, 0.94), rgba(13, 20, 35, 0.96));
      box-shadow: var(--glow);
    }
    .token-panel.hidden {
      display: none;
    }
    .token-panel-title {
      font-weight: 900;
      color: #f2f7ff;
      margin-bottom: 6px;
    }
    .token-help {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.35;
      margin-bottom: 8px;
    }
    .token-login-link {
      display: block;
      color: var(--accent);
      font-weight: 800;
      text-decoration: none;
      margin-bottom: 10px;
      word-break: break-word;
    }
    .token-login-link:hover {
      text-decoration: underline;
    }
    .token-panel textarea {
      min-height: 74px;
      resize: vertical;
      margin-bottom: 10px;
    }
    .token-actions {
      display: flex;
      gap: 10px;
    }
    .token-actions button {
      border: 0;
      border-radius: 12px;
      padding: 10px 12px;
      cursor: pointer;
      font-weight: 900;
    }
    .token-submit {
      background: var(--accent);
      color: #04211d;
    }
    .token-close {
      background: #6e7da0;
      color: #0f1729;
    }
    .token-message {
      margin-top: 8px;
      color: var(--muted);
      font-size: 13px;
    }
    .grid {
      display: grid;
      grid-template-columns: 1.35fr 0.95fr;
      gap: 14px;
      margin-bottom: 14px;
      align-items: start;
    }
    .panel {
      background: linear-gradient(180deg, rgba(22, 32, 52, 0.84), var(--panel));
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 16px;
      box-shadow: var(--glow);
      backdrop-filter: blur(10px);
      min-width: 0;
    }
    .panel h2 {
      margin-top: 0;
      margin-bottom: 12px;
      font-size: 21px;
      color: #f2f7ff;
      letter-spacing: 0.02em;
    }
    .metric-stack {
      display: grid;
      gap: 12px;
    }
    .metric-stack.horizontal {
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      align-items: stretch;
    }
    .account-panel {
      grid-column: 1 / -1;
    }
    .metric-card {
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 12px;
      background: linear-gradient(180deg, rgba(19, 28, 47, 0.78), rgba(14, 22, 38, 0.92));
      min-height: 96px;
    }
    .metric-card.good {
      background: linear-gradient(180deg, rgba(24, 224, 208, 0.12), rgba(14, 30, 44, 0.95));
      border-color: rgba(24, 224, 208, 0.38);
    }
    .metric-card.bad {
      background: linear-gradient(180deg, rgba(255, 95, 122, 0.13), rgba(36, 16, 27, 0.95));
      border-color: rgba(255, 95, 122, 0.34);
    }
    .metric-card.neutral {
      background: linear-gradient(180deg, rgba(255, 180, 84, 0.1), rgba(28, 23, 16, 0.92));
      border-color: rgba(255, 180, 84, 0.3);
    }
    .metric-label {
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 6px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }
    .metric-value {
      font-size: 24px;
      font-weight: 700;
      line-height: 1;
      font-family: "JetBrains Mono", Consolas, monospace;
      letter-spacing: 0.02em;
    }
    .metric-sub {
      margin-top: 6px;
      color: var(--muted);
      font-size: 13px;
    }
    .index-strip {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin: 0 0 12px;
    }
    .index-tile {
      border: 1px solid rgba(24, 224, 208, 0.24);
      border-radius: 14px;
      padding: 10px;
      background: linear-gradient(180deg, rgba(24, 224, 208, 0.12), rgba(12, 21, 37, 0.92));
      min-height: 84px;
    }
    .index-label {
      color: #b8c8e8;
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    .index-value {
      margin-top: 8px;
      color: #f7fbff;
      font-family: "JetBrains Mono", Consolas, monospace;
      font-size: 23px;
      line-height: 1;
      font-weight: 800;
    }
    .index-change {
      margin-top: 8px;
      font-family: "JetBrains Mono", Consolas, monospace;
      font-size: 14px;
      font-weight: 900;
    }
    .index-change.positive {
      color: #10b981;
    }
    .index-change.negative {
      color: #ef4444;
    }
    .index-change.neutral {
      color: var(--muted);
    }
    .index-meta {
      margin-top: 7px;
      color: var(--muted);
      font-size: 11px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .index-scanner-panel {
      grid-column: 1 / -1;
    }
    .index-scanner-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin-top: 14px;
    }
    .scanner-index-card {
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 14px;
      background: rgba(255, 252, 245, 0.94);
      box-shadow: 0 14px 30px rgba(24, 32, 44, 0.08);
    }
    .scanner-index-head {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: flex-start;
      margin-bottom: 10px;
    }
    .scanner-index-title {
      font-weight: 900;
      color: var(--ink);
      letter-spacing: 0.02em;
    }
    .scanner-index-sub {
      color: var(--muted);
      font-size: 12px;
      margin-top: 2px;
    }
    .scanner-status-pill {
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 12px;
      font-weight: 900;
      white-space: nowrap;
      background: rgba(107, 114, 128, 0.12);
      color: #374151;
    }
    .scanner-status-pill.ready {
      background: rgba(15, 118, 110, 0.16);
      color: #0f766e;
    }
    .scanner-status-pill.waiting {
      background: rgba(180, 83, 9, 0.14);
      color: #92400e;
    }
    .scanner-status-pill.no-entry {
      background: rgba(37, 99, 235, 0.10);
      color: #1d4ed8;
    }
    .scanner-index-metrics {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }
    .scanner-mini-metric {
      border: 1px solid rgba(210, 194, 165, 0.8);
      border-radius: 12px;
      padding: 9px;
      background: rgba(255, 255, 255, 0.55);
    }
    .scanner-mini-label {
      color: #64748b;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      font-weight: 800;
    }
    .scanner-mini-value {
      margin-top: 4px;
      color: var(--ink);
      font-family: "JetBrains Mono", Consolas, monospace;
      font-size: 16px;
      font-weight: 900;
    }
    .scanner-reason {
      margin-top: 10px;
      color: #475569;
      font-size: 13px;
      line-height: 1.4;
    }
    .underlying-strip {
      margin: 0;
      border: 1px solid rgba(20, 184, 166, 0.28);
      border-radius: 999px;
      padding: 5px 9px;
      background: rgba(236, 253, 245, 0.75);
      display: flex;
      justify-content: space-between;
      gap: 8px;
      align-items: center;
      min-height: 34px;
    }
    .underlying-title {
      color: #334155;
      font-size: 10px;
      font-weight: 900;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .underlying-price {
      margin-top: 1px;
      color: var(--ink);
      font-family: "JetBrains Mono", Consolas, monospace;
      font-size: 13px;
      font-weight: 900;
    }
    .underlying-change {
      font-family: "JetBrains Mono", Consolas, monospace;
      font-size: 11px;
      font-weight: 900;
      text-align: right;
      white-space: nowrap;
    }
    .underlying-change.positive {
      color: #059669;
    }
    .underlying-change.negative {
      color: #dc2626;
    }
    .underlying-change.neutral {
      color: #64748b;
    }
    .status-pill {
      display: inline-block;
      padding: 6px 12px;
      border-radius: 999px;
      font-weight: 700;
      font-size: 14px;
    }
    .running { background: rgba(15, 118, 110, 0.16); color: var(--accent); }
    .stopped { background: rgba(255, 95, 122, 0.14); color: var(--danger); }
    .market-pill {
      display: inline-block;
      margin-left: 8px;
      padding: 6px 12px;
      border-radius: 999px;
      font-weight: 700;
      font-size: 13px;
    }
    .market-open {
      background: rgba(15, 118, 110, 0.16);
      color: var(--accent);
    }
    .market-closed {
      background: rgba(185, 28, 28, 0.12);
      color: var(--danger);
    }
    .broker-pill {
      display: inline-block;
      margin-left: 8px;
      padding: 6px 12px;
      border-radius: 999px;
      font-weight: 700;
      font-size: 12px;
    }
    .broker-ok {
      background: rgba(15, 118, 110, 0.16);
      color: var(--accent);
    }
    .broker-fyers-ok {
      background: rgba(37, 99, 235, 0.14);
      color: #1d4ed8;
    }
    .broker-invalid {
      background: rgba(185, 28, 28, 0.12);
      color: var(--danger);
    }
    .broker-quotes-blocked {
      background: rgba(180, 83, 9, 0.16);
      color: var(--accent-2);
    }
    .broker-fallback {
      background: rgba(180, 83, 9, 0.14);
      color: var(--accent-2);
    }
    label {
      display: block;
      margin-bottom: 6px;
      font-size: 14px;
      color: var(--muted);
    }
    input, select, button, textarea {
      width: 100%;
      font: inherit;
    }
    input, select {
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 10px 12px;
      background: rgba(11, 17, 30, 0.88);
      color: var(--ink);
      margin-bottom: 12px;
    }
    input::placeholder {
      color: rgba(140, 162, 198, 0.75);
    }
    .actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    .actions button {
      flex: 1;
      min-width: 140px;
      border: 0;
      border-radius: 12px;
      padding: 12px 16px;
      cursor: pointer;
      font-weight: 700;
    }
    .start { background: var(--accent); color: #002722; }
    .stop { background: var(--danger); color: #1f0910; }
    .refresh { background: var(--accent-2); color: #2b1800; }
    .session { background: #6e7da0; color: #0f1729; }
    .meta {
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 15px;
    }
    .compact-panel {
      min-height: 0;
    }
    .compact-panel input,
    .compact-panel select {
      margin-bottom: 10px;
    }
    .compact-panel .actions {
      margin-top: 2px;
    }
    pre {
      margin: 0;
      min-height: 180px;
      max-height: 240px;
      overflow: auto;
      padding: 16px;
      border-radius: 14px;
      background: #081121;
      color: #d8ebff;
      border: 1px solid rgba(95, 130, 191, 0.4);
      font-family: "JetBrains Mono", Consolas, monospace;
      font-size: 13px;
      line-height: 1.45;
      white-space: pre-wrap;
    }
    .log-shell.maximized pre {
      min-height: 65vh;
      max-height: 65vh;
    }
    .log-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 12px;
    }
    .log-header h2 {
      margin: 0;
    }
    .log-toggle {
      border: 0;
      border-radius: 999px;
      padding: 10px 14px;
      cursor: pointer;
      font-weight: 700;
      background: rgba(24, 224, 208, 0.12);
      color: #d8fbf7;
    }
    ul {
      margin: 0;
      padding-left: 18px;
    }
    li { margin-bottom: 8px; }
    .mini-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 16px;
      margin: 0 0 18px;
    }
    .control-panel .meta {
      grid-template-columns: repeat(2, minmax(0, 1fr));
      margin-top: 10px;
      font-size: 13px;
      gap: 4px 12px;
    }
    .control-panel,
    .active-panel {
      min-height: 350px;
    }
    #activeTrades {
      max-height: 292px;
      overflow: auto;
      padding-right: 4px;
    }
    #completedTrades {
      max-height: 330px;
      overflow: auto;
      padding-right: 4px;
    }
    .watchlist-panel {
      grid-column: 1 / -1;
    }
    .top-picks-panel {
      grid-column: 1 / -1;
    }
    .progress-card {
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 14px;
      background: linear-gradient(180deg, rgba(20, 31, 51, 0.84), rgba(12, 21, 37, 0.94));
    }
    .progress-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 10px;
      align-items: center;
    }
    .progress-bar {
      width: 100%;
      height: 10px;
      border-radius: 999px;
      background: rgba(24, 33, 50, 0.9);
      overflow: hidden;
    }
    .progress-fill {
      height: 100%;
      background: linear-gradient(90deg, var(--accent), var(--accent-2));
      width: 0%;
    }
    .trade-list {
      display: grid;
      gap: 10px;
    }
    .trade-item {
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 12px;
      background: linear-gradient(180deg, rgba(20, 31, 50, 0.84), rgba(11, 18, 33, 0.94));
    }
    .active-trade-card {
      padding: 9px;
    }
    .active-trade-head {
      display: grid;
      grid-template-columns: minmax(160px, 1fr) minmax(160px, 260px) auto;
      align-items: center;
      gap: 10px;
      margin-bottom: 8px;
    }
    .active-trade-title {
      font-size: 20px;
      font-weight: 800;
      line-height: 1.1;
      letter-spacing: 0.01em;
      color: #eef5ff;
    }
    .active-trade-sub {
      font-size: 16px;
      font-weight: 700;
      color: #a7bbd8;
      white-space: nowrap;
      text-align: right;
    }
    .active-metrics {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 7px;
      margin-bottom: 7px;
    }
    .active-metric {
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 7px;
      background: rgba(8, 17, 31, 0.72);
    }
    .active-metric .label {
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
      margin-bottom: 2px;
    }
    .active-metric .value {
      font-size: 17px;
      font-weight: 800;
      line-height: 1.1;
      color: #e9f4ff;
      font-family: "JetBrains Mono", Consolas, monospace;
    }
    .active-meta {
      font-size: 12px;
      color: #9bb0cf;
      margin-bottom: 4px;
      word-break: break-word;
    }
    .active-pnl {
      font-size: 17px;
      font-weight: 800;
      margin-bottom: 5px;
      font-family: "JetBrains Mono", Consolas, monospace;
    }
    .flash-up {
      animation: flashUp 0.65s ease-out;
    }
    .flash-down {
      animation: flashDown 0.65s ease-out;
    }
    @keyframes flashUp {
      0% { background-color: rgba(16, 185, 129, 0.35); color: #d1fae5; }
      100% { background-color: transparent; color: inherit; }
    }
    @keyframes flashDown {
      0% { background-color: rgba(244, 63, 94, 0.32); color: #ffe4e6; }
      100% { background-color: transparent; color: inherit; }
    }
    .trade-controls {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      margin-top: 10px;
      flex-wrap: wrap;
    }
    .trade-button {
      border: 0;
      border-radius: 999px;
      padding: 8px 12px;
      cursor: pointer;
      font-weight: 700;
      background: rgba(24, 224, 208, 0.12);
      color: #d9fbf7;
    }
    .trade-button.exit {
      width: auto;
      margin-top: 2px;
      padding: 7px 13px;
      font-size: 13px;
      background: var(--danger);
      color: #fff;
    }
    .trade-button:disabled {
      cursor: not-allowed;
      opacity: 0.45;
    }
    .trade-page {
      color: var(--muted);
      font-size: 13px;
    }
    .trade-time {
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 4px;
    }
    .signal-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 10px;
    }
    .signal-pill {
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px;
      background: linear-gradient(180deg, rgba(20, 31, 50, 0.84), rgba(11, 18, 33, 0.94));
    }
    .signal-pill .label {
      font-size: 12px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .signal-pill .value {
      font-size: 20px;
      font-weight: 700;
      margin-top: 4px;
    }
    .signal-list {
      display: grid;
      gap: 8px;
      margin-top: 8px;
    }
    .strategy-board {
      display: grid;
      gap: 8px;
    }
    .strategy-row {
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px;
      background: linear-gradient(180deg, rgba(20, 31, 50, 0.84), rgba(11, 18, 33, 0.94));
      font-size: 13px;
    }
    .strategy-top {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      align-items: center;
      margin-bottom: 4px;
    }
    .strategy-id {
      font-weight: 700;
      color: #e8f1ff;
    }
    .strategy-score {
      font-family: "JetBrains Mono", Consolas, monospace;
      font-weight: 700;
      color: var(--accent);
    }
    .signal-item {
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px;
      background: linear-gradient(180deg, rgba(20, 31, 50, 0.84), rgba(11, 18, 33, 0.94));
      font-size: 13px;
    }
    .signal-filter {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      margin-bottom: 10px;
    }
    .signal-filter label {
      margin: 0;
      font-size: 13px;
      color: var(--muted);
    }
    .signal-filter select {
      margin: 0;
      max-width: 200px;
      padding: 8px 10px;
      border-radius: 10px;
    }
    .watchlist-form {
      display: grid;
      grid-template-columns: 1.4fr 1fr auto;
      gap: 10px;
      align-items: end;
    }
    .watchlist-form button {
      margin-bottom: 12px;
      border: 0;
      border-radius: 12px;
      padding: 12px 16px;
      cursor: pointer;
      font-weight: 700;
      background: var(--accent);
      color: #fff;
    }
    .suggest-wrap {
      position: relative;
    }
    .suggest-list {
      position: absolute;
      z-index: 20;
      top: calc(100% - 10px);
      left: 0;
      right: 0;
      max-height: 220px;
      overflow-y: auto;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: #fff;
      box-shadow: 0 8px 20px rgba(0, 0, 0, 0.10);
      display: none;
    }
    .suggest-item {
      padding: 9px 10px;
      cursor: pointer;
      font-size: 13px;
      border-bottom: 1px solid rgba(0, 0, 0, 0.05);
    }
    .suggest-item:last-child {
      border-bottom: 0;
    }
    .suggest-item:hover {
      background: rgba(15, 118, 110, 0.10);
    }
    .chip-list {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 4px;
    }
    .chip {
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(24, 224, 208, 0.12);
      border: 1px solid rgba(24, 224, 208, 0.28);
      color: var(--ink);
      font-size: 13px;
      font-weight: 700;
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }
    .chip.golden {
      background: rgba(255, 180, 84, 0.14);
      border-color: rgba(255, 180, 84, 0.36);
      color: #ffe0ad;
    }
    .chip-remove {
      border: 0;
      border-radius: 999px;
      width: 20px;
      height: 20px;
      line-height: 20px;
      padding: 0;
      cursor: pointer;
      font-weight: 700;
      background: rgba(185, 28, 28, 0.12);
      color: var(--danger);
      flex-shrink: 0;
    }
    .scan-add {
      border: 0;
      border-radius: 999px;
      width: 24px;
      height: 24px;
      line-height: 24px;
      padding: 0;
      cursor: pointer;
      font-weight: 700;
      background: rgba(24, 224, 208, 0.18);
      color: var(--accent);
    }
    .scan-add:disabled {
      cursor: not-allowed;
      opacity: 0.5;
    }
    .subnote {
      margin-top: 8px;
      color: var(--muted);
      font-size: 13px;
    }
    .inline-actions {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      margin-top: 12px;
    }
    .inline-actions .trade-button {
      width: auto;
      margin: 0;
    }
    .top-card {
      border-top: 4px solid rgba(180, 83, 9, 0.25);
    }
    .top-card:nth-child(1) { border-top-color: rgba(15, 118, 110, 0.28); }
    .top-card:nth-child(2) { border-top-color: rgba(180, 83, 9, 0.28); }
    .top-card:nth-child(3) { border-top-color: rgba(31, 41, 55, 0.22); }
    .scan-toolbar {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      align-items: end;
      margin-bottom: 10px;
    }
    .scan-toolbar select {
      margin-bottom: 0;
    }
    .scan-toolbar .actions {
      margin: 0;
    }
    .scan-checkbox {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
      margin-top: 10px;
      margin-bottom: 8px;
    }
    .scan-checkbox input {
      width: auto;
      margin: 0;
    }
    .scan-summary {
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px;
      background: linear-gradient(180deg, rgba(20, 31, 50, 0.84), rgba(11, 18, 33, 0.94));
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 10px;
    }
    .table-wrap {
      overflow: auto;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: linear-gradient(180deg, rgba(20, 31, 50, 0.84), rgba(11, 18, 33, 0.94));
    }
    .scan-table {
      width: 100%;
      border-collapse: collapse;
      min-width: 980px;
      font-size: 13px;
    }
    .scan-table th,
    .scan-table td {
      padding: 9px 10px;
      border-bottom: 1px solid rgba(73, 96, 140, 0.55);
      text-align: left;
      white-space: nowrap;
    }
    .scan-table th {
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--muted);
      background: rgba(14, 21, 36, 0.95);
      position: sticky;
      top: 0;
    }
    .scan-table tbody tr:hover {
      background: rgba(24, 224, 208, 0.07);
    }
    .scan-tag {
      padding: 4px 8px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      display: inline-block;
    }
    .scan-tag.buy {
      background: rgba(24, 224, 208, 0.18);
      color: var(--accent);
    }
    .scan-tag.sell {
      background: rgba(255, 95, 122, 0.15);
      color: var(--danger);
    }
    .scan-empty {
      color: var(--muted);
      font-size: 14px;
      padding: 10px 2px;
    }
    @media (max-width: 1150px) {
      .hero {
        grid-template-columns: 1fr;
      }
      .token-card {
        justify-self: stretch;
        max-width: none;
      }
      .token-status-button {
        justify-self: start;
      }
      .grid, .mini-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
      .metric-stack.horizontal {
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }
      .scan-toolbar {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
      .index-scanner-grid {
        grid-template-columns: 1fr;
      }
    }
    @media (max-width: 760px) {
      .grid, .mini-grid {
        grid-template-columns: 1fr;
      }
      .signal-grid {
        grid-template-columns: 1fr;
      }
      .active-trade-title {
        font-size: 19px;
      }
      .active-trade-sub {
        font-size: 15px;
      }
      .active-metric .value {
        font-size: 16px;
      }
      .active-trade-head {
        grid-template-columns: 1fr auto;
      }
      .active-trade-head .underlying-strip {
        grid-column: 1 / -1;
        order: 3;
      }
      .active-meta {
        font-size: 12px;
      }
      .active-pnl {
        font-size: 16px;
      }
      .watchlist-form {
        grid-template-columns: 1fr;
      }
      .metric-stack.horizontal {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
      .index-strip {
        grid-template-columns: 1fr;
      }
      .index-scanner-grid {
        grid-template-columns: 1fr;
      }
      .underlying-strip {
        align-items: center;
        flex-direction: row;
      }
      .underlying-change {
        text-align: right;
      }
      .scan-toolbar {
        grid-template-columns: 1fr;
      }
      .control-panel .meta {
        grid-template-columns: 1fr;
      }
      .token-actions {
        flex-direction: column;
      }
    }
    /* OptionTrader light-theme override while preserving AlgoTrader layout */
    :root {
      --bg: #f4efe7 !important;
      --bg-elev: #fffaf1 !important;
      --panel: #fffaf1 !important;
      --panel-2: #fff8ec !important;
      --ink: #1f2937 !important;
      --muted: #6b7280 !important;
      --border: #d6c7ae !important;
      --accent: #0f766e !important;
      --accent-2: #b45309 !important;
      --danger: #b91c1c !important;
    }
    body {
      background:
        radial-gradient(circle at top right, rgba(180, 83, 9, 0.12), transparent 28%),
        radial-gradient(circle at left center, rgba(15, 118, 110, 0.10), transparent 25%),
        var(--bg) !important;
      color: var(--ink) !important;
    }
    .panel, .metric-card, .trade-item, .progress-card, .signal-pill, .leader-item, .scan-card, .market-card {
      background: #fffaf1 !important;
      color: var(--ink) !important;
      border-color: var(--border) !important;
    }
    pre, input, select, textarea {
      background: #ffffff !important;
      color: var(--ink) !important;
      border-color: var(--border) !important;
    }
    .scan-table thead th, .scan-table tbody td {
      color: var(--ink) !important;
      border-color: var(--border) !important;
      background: #fffaf1 !important;
    }
    .scan-table tbody tr:hover {
      background: rgba(15, 118, 110, 0.08) !important;
    }
    /* Readability boost */
    body {
      font-size: 16px !important;
      line-height: 1.45 !important;
    }
    h1, h2 {
      color: #1f2937 !important;
    }
    .subtitle, .subnote, .metric-sub, .trade-time, .meta, .trade-page {
      color: #334155 !important;
      font-size: 14px !important;
      opacity: 1 !important;
    }
    .metric-label {
      color: #334155 !important;
      font-size: 12px !important;
      font-weight: 700 !important;
    }
    .metric-value {
      color: #111827 !important;
      font-size: 28px !important;
      font-weight: 800 !important;
    }
    .panel {
      box-shadow: 0 10px 25px rgba(15, 23, 42, 0.18) !important;
    }
    .status-pill, .market-pill, .broker-pill {
      font-size: 13px !important;
      font-weight: 800 !important;
      border: 1px solid rgba(15, 23, 42, 0.18) !important;
    }
    button, .trade-button, .scan-add {
      font-weight: 800 !important;
      color: #111827 !important;
    }
    .start { color: #ffffff !important; background: #0f766e !important; }
    .stop { color: #ffffff !important; background: #b91c1c !important; }
    .refresh { color: #ffffff !important; background: #b45309 !important; }
    .session { color: #ffffff !important; background: #475569 !important; }
    input, select, textarea {
      color: #111827 !important;
      background: #ffffff !important;
      border: 1px solid #94a3b8 !important;
    }
    input::placeholder {
      color: #64748b !important;
      opacity: 1 !important;
    }
    #logTail {
      color: #e5e7eb !important;
      background: #0f172a !important;
      font-size: 13px !important;
    }
    .scan-summary, .scan-empty {
      color: #1e293b !important;
      font-size: 13px !important;
      font-weight: 600 !important;
    }
    .scan-table th {
      color: #0f172a !important;
      font-size: 12px !important;
      font-weight: 800 !important;
      background: #e2e8f0 !important;
    }
    .scan-table td {
      color: #111827 !important;
      font-size: 13px !important;
      font-weight: 600 !important;
      background: #fffaf1 !important;
    }
    .chip {
      color: #0f172a !important;
      font-weight: 800 !important;
      background: #d1fae5 !important;
      border-color: #99f6e4 !important;
    }
    .chip.golden {
      background: #fef3c7 !important;
      border-color: #fcd34d !important;
      color: #78350f !important;
    }
    .active-trade-card,
    .active-trade-head,
    .active-trade-title,
    .active-trade-sub,
    .active-meta,
    .active-pnl {
      color: #111827 !important;
    }
    .active-trade-title {
      font-weight: 900 !important;
      letter-spacing: 0.01em !important;
    }
    .active-trade-sub {
      color: #334155 !important;
      font-weight: 800 !important;
    }
    .active-metric {
      background: #4b5563 !important;
      border-color: #374151 !important;
    }
    .active-metric .label {
      color: #d1d5db !important;
    }
    .active-metric .value {
      color: #f9fafb !important;
    }
    .index-tile {
      background: #ecfdf5 !important;
      border-color: #99f6e4 !important;
      color: #111827 !important;
    }
    .index-label, .index-meta {
      color: #334155 !important;
    }
    .index-value {
      color: #0f172a !important;
    }
    .index-change.positive {
      color: #059669 !important;
    }
    .index-change.negative {
      color: #dc2626 !important;
    }
    .index-change.neutral {
      color: #64748b !important;
    }
    .token-panel {
      background: #fffaf1 !important;
      color: #111827 !important;
      border-color: var(--border) !important;
      box-shadow: 0 10px 25px rgba(15, 23, 42, 0.18) !important;
    }
    .token-panel-title {
      color: #111827 !important;
    }
    .token-help,
    .token-message {
      color: #334155 !important;
    }
    .token-login-link {
      color: #0f766e !important;
    }
    .token-status-button {
      color: #111827 !important;
      border-color: rgba(15, 23, 42, 0.18) !important;
    }
    .token-submit {
      background: #0f766e !important;
      color: #ffffff !important;
    }
    .token-close {
      background: #475569 !important;
      color: #ffffff !important;
    }
    .strategy-board {
      gap: 12px !important;
    }
    .strategy-row {
      background: #ffffff !important;
      color: #111827 !important;
      border: 1px solid #cbd5e1 !important;
      border-left: 6px solid #0f766e !important;
      box-shadow: 0 8px 18px rgba(15, 23, 42, 0.10) !important;
      padding: 14px !important;
    }
    .strategy-top {
      border-bottom: 1px solid #e2e8f0 !important;
      padding-bottom: 8px !important;
      margin-bottom: 10px !important;
    }
    .strategy-id {
      color: #0f172a !important;
      font-size: 15px !important;
      font-weight: 900 !important;
      line-height: 1.25 !important;
    }
    .strategy-score {
      background: #ecfdf5 !important;
      border: 1px solid #99f6e4 !important;
      border-radius: 999px !important;
      color: #0f766e !important;
      font-size: 13px !important;
      padding: 5px 9px !important;
      white-space: nowrap !important;
    }
    .strategy-row .trade-time {
      color: #1f2937 !important;
      font-size: 14px !important;
      font-weight: 700 !important;
      line-height: 1.45 !important;
      margin-top: 5px !important;
    }
    .strategy-row:nth-child(3) {
      border-left-color: #b91c1c !important;
    }
    .strategy-row:nth-child(3) .strategy-score {
      background: #fef2f2 !important;
      border-color: #fecaca !important;
      color: #b91c1c !important;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <div class="hero-copy">
        <h1>OptionTrader Control Room</h1>
        <p class="subtitle">Start or stop the paper engine, watch the live log stream, and iterate on strategy behavior without staying glued to a terminal window.</p>
      </div>
      <div class="token-card">
        <button id="tokenStatusButton" class="token-status-button token-unknown" onclick="toggleTokenPanel()">Checking Token...</button>
        <div id="tokenPanel" class="token-panel hidden">
          <div class="token-panel-title">Zerodha Token Refresh</div>
          <div id="tokenHelp" class="token-help">Open Zerodha login, complete login, then paste the full redirected URL or only request_token below.</div>
          <a id="zerodhaLoginLink" class="token-login-link" href="#" target="_blank" rel="noreferrer">Open Zerodha login URL</a>
          <textarea id="zerodhaTokenInput" placeholder="Paste full redirected URL or request_token here"></textarea>
          <div class="token-actions">
            <button class="token-submit" onclick="submitZerodhaToken()">Submit Token</button>
            <button class="token-close" onclick="toggleTokenPanel(false)">Close</button>
          </div>
          <div id="tokenMessage" class="token-message">Waiting for token status...</div>
        </div>
      </div>
    </section>
    <section class="grid">
      <div class="panel compact-panel top-card control-panel">
        <h2>Engine</h2>
        <div id="indexStrip" class="index-strip">
          <div class="index-tile" data-index-key="NSE:NIFTY 50">
            <div class="index-label">NIFTY 50</div>
            <div class="index-value" id="indexNifty50">-</div>
            <div class="index-change neutral" id="indexNifty50Change">-</div>
            <div class="index-meta" id="indexNifty50Meta">Kite LTP waiting...</div>
          </div>
          <div class="index-tile" data-index-key="NSE:NIFTY BANK">
            <div class="index-label">NIFTY BANK</div>
            <div class="index-value" id="indexNiftyBank">-</div>
            <div class="index-change neutral" id="indexNiftyBankChange">-</div>
            <div class="index-meta" id="indexNiftyBankMeta">Kite LTP waiting...</div>
          </div>
          <div class="index-tile" data-index-key="BSE:SENSEX">
            <div class="index-label">SENSEX</div>
            <div class="index-value" id="indexSensex">-</div>
            <div class="index-change neutral" id="indexSensexChange">-</div>
            <div class="index-meta" id="indexSensexMeta">Kite LTP waiting...</div>
          </div>
        </div>
        <div>
          <span id="pill" class="status-pill stopped">Stopped</span>
          <span id="marketPill" class="market-pill market-closed">Market Closed</span>
        </div>
        <div class="actions">
          <button class="start" onclick="startEngine()">Start Engine</button>
          <button class="stop" onclick="stopEngine()">Stop Engine</button>
          <button class="refresh" onclick="refreshStatus()">Refresh</button>
          <button class="session" onclick="newSession()">New Session</button>
        </div>
        <div class="meta">
          <div><strong>PID:</strong> <span id="pid">-</span></div>
          <div><strong>Started:</strong> <span id="startedAt">-</span></div>
          <div><strong>Last Exit Code:</strong> <span id="exitCode">-</span></div>
          <div><strong>Mode:</strong> <span id="modeValue">paper</span></div>
          <div><strong>Broker:</strong> <span id="brokerLabel" class="broker-pill broker-fallback">FALLBACK</span></div>
          <div style="color: var(--muted); font-size: 12px;" id="brokerMessage">Broker health not checked yet.</div>
          <div><strong>Market:</strong> <span id="marketNow">-</span></div>
          <div><strong>Next Open:</strong> <span id="marketNextOpen">-</span></div>
          <div><strong>Log File:</strong> <span id="logPath">-</span></div>
        </div>
      </div>
      <section class="panel top-card active-panel">
        <h2>Active Paper Positions</h2>
        <div id="activeTrades">No active paper positions.</div>
      </section>
      <div class="panel compact-panel top-card account-panel">
        <h2>Paper Account</h2>
        <div id="paperAccount" class="metric-stack horizontal">
          <div class="metric-card">
            <div class="metric-label">Starting Capital</div>
            <div class="metric-value">0.00</div>
          </div>
        </div>
      </div>
      <div class="panel watchlist-panel">
        <h2>Watchlist Manager</h2>
        <div class="watchlist-form">
          <div class="suggest-wrap">
            <label for="newSymbol">Stock Symbol</label>
            <input id="newSymbol" placeholder="Example: TCS">
            <div id="symbolSuggestions" class="suggest-list"></div>
          </div>
          <div>
            <label for="newInterval">Interval</label>
            <select id="newInterval">
              <option value="2minute">2minute</option>
              <option value="1minute">1minute</option>
              <option value="5minute" selected>5minute</option>
              <option value="15minute">15minute</option>
              <option value="60minute">60minute</option>
              <option value="day">day</option>
            </select>
          </div>
          <button onclick="addWatchSymbol()">Add Stock</button>
        </div>
        <div id="watchlistChips" class="chip-list"></div>
        <div class="inline-actions">
          <div class="subnote" style="margin:0;">New stocks are written to the config immediately. If engine is running, changes are applied automatically.</div>
        </div>
        <div class="inline-actions" style="margin-top:14px;">
          <div class="subnote" style="margin:0;">Stocks with 20/200 golden crossover (NIFTY 250)</div>
          <button id="refreshCrossoversBtn" class="trade-button" onclick="refreshGoldenCrossover(true)">Refresh Crossovers</button>
        </div>
        <div id="goldenCrossoverStatus" class="subnote" style="margin-top:6px;">Waiting for first refresh...</div>
        <div id="goldenCrossoverChips" class="chip-list"></div>
      </div>
    </section>
    <section class="mini-grid">
      <section class="panel index-scanner-panel">
        <h2>Index Options Scanner</h2>
        <div class="subnote" style="margin-top:2px;">Tracks NIFTY, BANKNIFTY, and SENSEX separately. Shows trend score, CE/PE bias, and why a trade is or is not ready.</div>
        <div id="indexOptionsScanner" class="index-scanner-grid">Waiting for index scanner data...</div>
      </section>
      <section class="panel top-picks-panel">
        <h2>Engulfing Strategy Scanner (2m)</h2>
        <div class="subnote" style="margin-top:2px;">Pattern: 2-minute bullish/bearish engulfing-style reversal on NIFTY250. Entries map to ATM CE/PE with candle-length based SL/Target rules.</div>
        <div class="scan-toolbar">
          <div>
            <label for="scanUniverse">Universe</label>
            <select id="scanUniverse">
              <option value="nifty100">NIFTY 100</option>
              <option value="nifty250" selected>NIFTY 250 (LargeMidcap 250)</option>
              <option value="watchlist">Watchlist</option>
            </select>
          </div>
          <div>
            <label for="scanInterval">Interval</label>
            <select id="scanInterval">
              <option value="2m" selected>2m (Engulfing)</option>
              <option value="5m">5m</option>
              <option value="15m">15m</option>
              <option value="1h">1h</option>
              <option value="1d">1 day</option>
            </select>
          </div>
          <div>
            <label for="scanTop">Top Picks</label>
            <select id="scanTop">
              <option value="3">3</option>
              <option value="5" selected>5</option>
              <option value="10">10</option>
            </select>
          </div>
          <div class="actions">
            <button class="refresh" onclick="refreshDailyScan(true)">Refresh Engulfing Plan</button>
          </div>
        </div>
        <label class="scan-checkbox" for="scanActionableOnly">
          <input id="scanActionableOnly" type="checkbox" onchange="rerenderDailyScan()">
          Show actionable picks only
        </label>
        <div id="dailyScanSummary" class="scan-summary">Loading engulfing scanner...</div>
        <div id="dailyScanPicks" class="scan-empty">Waiting for scanner data...</div>
      </section>
      <section class="panel">
        <h2>Candle Progress</h2>
        <div id="candleProgress">Waiting for candle files...</div>
      </section>
      <section class="panel">
        <h2>Signal Quality</h2>
        <div class="signal-filter">
          <label for="signalQualityPeriod">Window</label>
          <select id="signalQualityPeriod" onchange="changeSignalQualityPeriod()">
            <option value="today">Today</option>
            <option value="last3d">Last 3 Days</option>
            <option value="all" selected>All-Time</option>
          </select>
        </div>
        <div id="signalQuality">No closed paper trades yet.</div>
      </section>
      <section class="panel">
        <h2>Strategy Leaderboard</h2>
        <div id="strategyLeaderboard">No strategy stats yet.</div>
      </section>
      <section class="panel">
        <h2>Paper Trades</h2>
        <div id="paperTrades">No paper trades yet.</div>
      </section>
      <section class="panel">
        <h2>Completed Paper Trades</h2>
        <div id="completedTrades">No completed paper trades.</div>
      </section>
    </section>
    <section id="logShell" class="panel log-shell">
      <div class="log-header">
        <h2>Live Engine Log</h2>
        <button id="logToggle" class="log-toggle" onclick="toggleLog()">Maximize Log</button>
      </div>
      <pre id="logTail">Waiting for engine output...</pre>
    </section>
  </div>
  <script>
    let completedTradesExpanded = false;
    let completedTradesPage = 0;
    let latestSymbolSuggestions = [];
    let selectedSuggestion = "";
    let suggestionFetchTimer = null;
    let signalQualityPeriod = "all";
    let signalQualityByPeriod = {};
    let latestDailyScan = null;
    let watchlistSymbols = new Set();
    let lastActiveSnapshots = {};
    let lastIndexSnapshots = {};
    let liveTicksInFlight = false;
    let lastGoldenCrossoverKey = "";
    let candleProgressExpanded = false;
    let candleProgressPage = 0;
    let tokenPanelPinned = false;
    let tokenNeedsRefresh = false;

    async function api(path, method = "GET", body = null) {
      const response = await fetch(path, {
        method,
        headers: {"Content-Type": "application/json"},
        body: body ? JSON.stringify(body) : null
      });
      if (!response.ok) {
        const raw = await response.text();
        const clean = raw.replace(/<[^>]+>/g, " ").replace(/\\s+/g, " ").trim();
        const short = clean ? clean.slice(0, 240) : `HTTP ${response.status}`;
        throw new Error(short);
      }
      return response.json();
    }

    function renderTokenStatus(token) {
      const button = document.getElementById("tokenStatusButton");
      const panel = document.getElementById("tokenPanel");
      const link = document.getElementById("zerodhaLoginLink");
      const message = document.getElementById("tokenMessage");
      const help = document.getElementById("tokenHelp");
      if (!button || !panel || !link || !message || !help) {
        return;
      }
      const status = String(token.status || "unknown");
      tokenNeedsRefresh = Boolean(token.needs_refresh);
      button.textContent = token.button_label || token.label || "Token Check";
      button.className = `token-status-button token-${status}`;
      const loginUrl = token.login_url || "#";
      link.href = loginUrl;
      link.textContent = loginUrl && loginUrl !== "#"
        ? "Open Zerodha login URL in new tab"
        : "Zerodha login URL unavailable";
      link.style.pointerEvents = loginUrl && loginUrl !== "#" ? "auto" : "none";
      help.textContent = tokenNeedsRefresh
        ? "Token is required or not valid. Open the login URL, complete Zerodha login, then paste the redirected URL or request_token below."
        : "Token is active. You can still refresh it manually if you want to regenerate today's access token.";
      message.textContent = token.message || "";
      if (tokenNeedsRefresh || tokenPanelPinned) {
        panel.classList.remove("hidden");
      } else {
        panel.classList.add("hidden");
      }
    }

    function toggleTokenPanel(force) {
      const panel = document.getElementById("tokenPanel");
      if (!panel) {
        return;
      }
      if (typeof force === "boolean") {
        tokenPanelPinned = force;
      } else {
        tokenPanelPinned = panel.classList.contains("hidden");
      }
      if (tokenPanelPinned || tokenNeedsRefresh) {
        panel.classList.remove("hidden");
      } else {
        panel.classList.add("hidden");
      }
    }

    async function submitZerodhaToken() {
      const input = document.getElementById("zerodhaTokenInput");
      const message = document.getElementById("tokenMessage");
      const value = input.value.trim();
      if (!value) {
        alert("Paste the full redirected URL or request_token first.");
        return;
      }
      try {
        message.textContent = "Refreshing Zerodha token...";
        const payload = await api("/api/zerodha-token", "POST", {token_input: value});
        input.value = "";
        tokenPanelPinned = false;
        renderTokenStatus(payload.token_status || {});
        refreshStatus();
        message.textContent = payload.message || "Token refreshed successfully.";
      } catch (error) {
        try {
          const status = await api("/api/token-status");
          renderTokenStatus(status || {});
          if (status && status.status === "active") {
            tokenPanelPinned = false;
            message.textContent = "Token is already active. Ignoring the stale refresh error: " + error.message;
            refreshStatus();
            return;
          }
        } catch (_statusError) {
          // Fall through to the real failure message below.
        }
        message.textContent = "Token refresh failed: " + error.message;
        alert(error.message);
      }
    }

    function renderStatus(data) {
      const pill = document.getElementById("pill");
      pill.textContent = data.running ? "Running" : "Stopped";
      pill.className = "status-pill " + (data.running ? "running" : "stopped");
      document.getElementById("pid").textContent = data.pid ?? "-";
      document.getElementById("startedAt").textContent = data.started_at ?? "-";
      document.getElementById("exitCode").textContent = data.last_exit_code ?? "-";
      document.getElementById("modeValue").textContent = data.mode ?? "-";
      const broker = data.broker_health || {status: "fallback", label: "FALLBACK", message: "No broker health data."};
      const brokerLabel = document.getElementById("brokerLabel");
      brokerLabel.textContent = broker.label || "FALLBACK";
      brokerLabel.className = "broker-pill " + (
        broker.status === "ok"
          ? "broker-ok"
          : (broker.status === "invalid"
            ? "broker-invalid"
            : (broker.status === "fyers_ok"
              ? "broker-fyers-ok"
              : (broker.status === "quotes_blocked" ? "broker-quotes-blocked" : "broker-fallback")))
      );
      document.getElementById("brokerMessage").textContent = broker.message || "";
      renderTokenStatus(data.zerodha_token || {});
      document.getElementById("logPath").textContent = data.log_path ?? "-";
      const logTail = document.getElementById("logTail");
      logTail.textContent = data.log_tail || "No log output yet.";
      logTail.scrollTop = logTail.scrollHeight;
      renderMarketSession(data.market_session || {});
      signalQualityByPeriod = data.signal_quality_by_period || {};
      if (!Object.keys(signalQualityByPeriod).length && data.signal_quality) {
        signalQualityByPeriod = {all: data.signal_quality};
      }
      renderCandleProgress(data.candle_progress || []);
      renderIndexOptionsScanner(data.index_options_scanner || []);
      renderSignalQuality();
      renderStrategyLeaderboard(data.strategy_leaderboard || []);
      renderPaperTrades(data.paper_trades || []);
      renderActiveTrades(data.active_paper_trades || []);
      renderCompletedTrades(data.completed_paper_trades || []);
      renderPaperAccount(data.paper_account || {});
      renderWatchlist(data.watchlist || []);
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }

    function formatNum(value, digits = 2) {
      if (value === null || value === undefined || value === "") {
        return "-";
      }
      const num = Number(value);
      if (!Number.isFinite(num)) {
        return "-";
      }
      return num.toFixed(digits);
    }

    function indexElementId(key, suffix = "") {
      const mapping = {
        "NSE:NIFTY 50": "indexNifty50",
        "NSE:NIFTY BANK": "indexNiftyBank",
        "BSE:SENSEX": "indexSensex",
      };
      return (mapping[key] || "") + suffix;
    }

    function renderIndexTicks(rows) {
      if (!Array.isArray(rows)) {
        return;
      }
      const next = {};
      for (const row of rows) {
        const key = row.key || `${row.exchange}:${row.tradingsymbol}`;
        const valueEl = document.getElementById(indexElementId(key));
        const changeEl = document.getElementById(indexElementId(key, "Change"));
        const metaEl = document.getElementById(indexElementId(key, "Meta"));
        if (!valueEl || !changeEl || !metaEl) {
          continue;
        }
        const price = row.last_price === null || row.last_price === undefined ? null : Number(row.last_price);
        const change = row.change === null || row.change === undefined ? null : Number(row.change);
        const changePct = row.change_pct === null || row.change_pct === undefined ? null : Number(row.change_pct);
        const prev = lastIndexSnapshots[key];
        valueEl.classList.remove("flash-up", "flash-down");
        if (price !== null && prev !== undefined && Number.isFinite(prev) && price !== prev) {
          valueEl.classList.add(price > prev ? "flash-up" : "flash-down");
        }
        valueEl.textContent = price === null ? "-" : price.toFixed(2);
        const directionClass = change === null || change === 0 ? "neutral" : (change > 0 ? "positive" : "negative");
        const sign = change !== null && change > 0 ? "+" : "";
        changeEl.className = `index-change ${directionClass}`;
        changeEl.textContent = change === null || changePct === null
          ? "-"
          : `${sign}${change.toFixed(2)} (${sign}${changePct.toFixed(2)}%)`;
        metaEl.textContent = `Prev close ${formatNum(row.previous_close, 2)} | ${row.source || "kite_quote"} | ${row.updated_at || "-"}`;
        next[key] = price;
      }
      lastIndexSnapshots = next;
    }

    async function refreshLiveTicks() {
      if (liveTicksInFlight) {
        return;
      }
      liveTicksInFlight = true;
      try {
        const data = await api("/api/live-ticks");
        renderIndexTicks(data.indices || []);
        renderIndexOptionsScanner(data.index_options_scanner || []);
        if (Array.isArray(data.active_paper_trades)) {
          renderActiveTrades(data.active_paper_trades);
        }
      } catch (_error) {
        // Keep the main dashboard stable; status refresh will surface larger failures.
      } finally {
        liveTicksInFlight = false;
      }
    }

    function formatIstDateTime(value) {
      if (!value) {
        return "-";
      }
      const dt = new Date(value);
      if (Number.isNaN(dt.getTime())) {
        return String(value);
      }
      try {
        return new Intl.DateTimeFormat("en-IN", {
          timeZone: "Asia/Kolkata",
          year: "numeric",
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
          hour12: false,
        }).format(dt) + " IST";
      } catch (_) {
        return dt.toLocaleString();
      }
    }

    function rerenderDailyScan() {
      if (latestDailyScan) {
        renderDailyScan(latestDailyScan);
      }
    }

    function watchIntervalFromScanInterval(scanInterval) {
      if (scanInterval === "2m") {
        return "2minute";
      }
      if (scanInterval === "5m") {
        return "5minute";
      }
      if (scanInterval === "15m") {
        return "15minute";
      }
      if (scanInterval === "1h") {
        return "60minute";
      }
      if (scanInterval === "1d") {
        return "day";
      }
      return "5minute";
    }

    async function addSymbolToWatchlist(symbol, interval) {
      try {
        const data = await api("/api/watchlist", "POST", {action: "add", symbol, interval});
        renderStatus(data);
        await refreshDailyScan(false);
      } catch (error) {
        alert(error.message);
      }
    }

    async function removeWatchSymbol(symbol) {
      if (!confirm(`Remove ${symbol} from watchlist?`)) {
        return;
      }
      try {
        const data = await api("/api/watchlist", "POST", {action: "remove", symbol});
        renderStatus(data);
        await refreshDailyScan(false);
      } catch (error) {
        alert(error.message);
      }
    }

    async function refreshGoldenCrossover(forceRefresh = false) {
      const refreshValue = forceRefresh ? "1" : "0";
      const statusEl = document.getElementById("goldenCrossoverStatus");
      const btn = document.getElementById("refreshCrossoversBtn");
      if (statusEl) {
        statusEl.textContent = "Refreshing crossover list...";
      }
      if (btn) {
        btn.disabled = true;
        btn.textContent = "Refreshing...";
      }
      try {
        const data = await api(`/api/golden-crossover?refresh=${refreshValue}`);
        renderGoldenCrossover(data);
        if (statusEl) {
          const stocks = Array.isArray(data.stocks) ? data.stocks : [];
          const key = stocks.map((x) => String(x.symbol || "").toUpperCase()).join(",");
          const changed = key !== lastGoldenCrossoverKey;
          lastGoldenCrossoverKey = key;
          statusEl.textContent = `Refreshed at ${new Date().toLocaleTimeString()}${changed ? " | list updated" : " | no change"}`;
        }
      } catch (error) {
        const container = document.getElementById("goldenCrossoverChips");
        container.innerHTML = `<div class="scan-empty">Golden crossover scan unavailable right now. Please retry in a minute.</div>`;
        if (statusEl) {
          statusEl.textContent = `Refresh failed: ${error.message}`;
        }
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.textContent = "Refresh Crossovers";
        }
      }
    }

    function renderGoldenCrossover(data) {
      const container = document.getElementById("goldenCrossoverChips");
      const stocks = Array.isArray(data.stocks) ? data.stocks : [];
      if (!stocks.length) {
        const message = escapeHtml(data.message || "No 20/200 golden crossover stocks found right now.");
        container.innerHTML = `<div class="scan-empty">${message}</div>`;
        return;
      }

      container.innerHTML = stocks.slice(0, 12).map((row) => {
        const symbol = String(row.symbol || "").toUpperCase();
        const fresh = Boolean(row.fresh_cross);
        const strength = Number(row.strength_pct || 0).toFixed(2);
        const inWatch = watchlistSymbols.has(symbol);
        return `
          <div class="chip golden">
            ${escapeHtml(symbol)} | ${fresh ? "Fresh" : "Active"} | ${strength}%
            <button class="scan-add" onclick='addSymbolToWatchlist(${JSON.stringify(symbol)},"5minute")' ${inWatch ? "disabled" : ""}>${inWatch ? "âœ“" : "+"}</button>
          </div>
        `;
      }).join("");
    }

    async function refreshDailyScan(forceRefresh = false) {
      const universe = document.getElementById("scanUniverse").value || "nifty100";
      const interval = document.getElementById("scanInterval").value || "5m";
      const top = document.getElementById("scanTop").value || "5";
      const refreshValue = forceRefresh ? "1" : "0";
      const path = `/api/daily-scan?universe=${encodeURIComponent(universe)}&top=${encodeURIComponent(top)}&interval=${encodeURIComponent(interval)}&refresh=${refreshValue}`;
      try {
        const data = await api(path);
        latestDailyScan = data;
        renderDailyScan(data);
      } catch (error) {
        document.getElementById("dailyScanSummary").textContent = "Scanner unavailable right now.";
        document.getElementById("dailyScanPicks").innerHTML = `<div class="scan-empty">Could not load daily scan: ${escapeHtml(error.message)}</div>`;
      }
    }

    function renderDailyScan(data) {
      const summary = document.getElementById("dailyScanSummary");
      const container = document.getElementById("dailyScanPicks");
      const actionableOnly = document.getElementById("scanActionableOnly").checked;
      const market = data.market_regime || {};
      const actionable = Array.isArray(data.actionable) ? data.actionable : [];
      const watch = Array.isArray(data.watchlist) ? data.watchlist : [];
      const watchCandidates = watch
        .filter((row) => row.status === "watch" || row.status === "skip")
        .sort((a, b) => Number(b.score || 0) - Number(a.score || 0))
        .slice(0, 12);
      const selectedScanInterval = document.getElementById("scanInterval").value || "5m";
      const watchInterval = watchIntervalFromScanInterval(selectedScanInterval);

      summary.textContent =
        `Engulfing Scan | Updated: ${formatIstDateTime(data.generated_at)} | Universe: ${(data.universe || "-").toUpperCase()} (${data.universe_count || 0}) | Scanned: ${data.scanned_count || 0} | ` +
        `Market: ${market.regime || "-"} | NIFTY: ${formatNum(market.close, 2)} | Actionable: ${actionable.length}`;

      const actionableSection = !actionable.length
        ? `<div class="scan-empty">${escapeHtml(data.message || "No actionable picks right now.")}</div>`
        : `
          <div class="table-wrap">
            <table class="scan-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th>Entry</th>
                  <th>SL</th>
                  <th>Target</th>
                  <th>Qty</th>
                  <th>Score</th>
                  <th>Setup</th>
                  <th>Add</th>
                </tr>
              </thead>
              <tbody>
                ${actionable.map((row, idx) => `
                  <tr>
                    <td>${idx + 1}</td>
                    <td>${escapeHtml(row.symbol)}</td>
                    <td><span class="scan-tag ${String(row.direction || "").toLowerCase() === "buy" ? "buy" : "sell"}">${escapeHtml(row.direction)}</span></td>
                    <td>${formatNum(row.entry, 2)}</td>
                    <td>${formatNum(row.stop_loss, 2)}</td>
                    <td>${formatNum(row.target, 2)}</td>
                    <td>${escapeHtml(row.quantity)}</td>
                    <td>${formatNum(row.score, 2)}</td>
                    <td>${escapeHtml(row.setup || "-")}</td>
                    <td><button class="scan-add" onclick='addSymbolToWatchlist(${JSON.stringify(String(row.symbol || ""))},"${watchInterval}")' ${watchlistSymbols.has(String(row.symbol || "").toUpperCase()) ? "disabled" : ""}>${watchlistSymbols.has(String(row.symbol || "").toUpperCase()) ? "âœ“" : "+"}</button></td>
                  </tr>
                `).join("")}
              </tbody>
            </table>
          </div>
        `;

      if (actionableOnly && actionable.length) {
        container.innerHTML = actionableSection;
        return;
      }

      const watchSection = !watchCandidates.length
        ? `<div class="scan-empty" style="margin-top:10px;">No watch candidates available.</div>`
        : `
          <div class="table-wrap" style="margin-top:10px;">
            <table class="scan-table">
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Status</th>
                  <th>Score</th>
                  <th>Direction</th>
                  <th>Close</th>
                  <th>Reason</th>
                  <th>Add</th>
                </tr>
              </thead>
              <tbody>
                ${watchCandidates.map((row) => `
                  <tr>
                    <td>${escapeHtml(row.symbol)}</td>
                    <td>${escapeHtml(row.status || "-")}</td>
                    <td>${formatNum(row.score, 2)}</td>
                    <td>${escapeHtml(row.direction || "-")}</td>
                    <td>${formatNum(row.close, 2)}</td>
                    <td>${escapeHtml(row.reason || "-")}</td>
                    <td><button class="scan-add" onclick='addSymbolToWatchlist(${JSON.stringify(String(row.symbol || ""))},"${watchInterval}")' ${watchlistSymbols.has(String(row.symbol || "").toUpperCase()) ? "disabled" : ""}>${watchlistSymbols.has(String(row.symbol || "").toUpperCase()) ? "âœ“" : "+"}</button></td>
                  </tr>
                `).join("")}
              </tbody>
            </table>
          </div>
        `;

      container.innerHTML = `${actionableSection}${watchSection}`;
    }

    function renderMarketSession(market) {
      const pill = document.getElementById("marketPill");
      const isOpen = Boolean(market.is_open);
      pill.textContent = isOpen ? "Market Open" : "Market Closed";
      pill.className = "market-pill " + (isOpen ? "market-open" : "market-closed");
      document.getElementById("marketNow").textContent = market.now || "-";
      document.getElementById("marketNextOpen").textContent = market.next_open || "-";
    }

    function renderCandleProgress(rows) {
      const container = document.getElementById("candleProgress");
      if (!rows.length) {
        candleProgressExpanded = false;
        candleProgressPage = 0;
        container.textContent = "No enabled watchlist items found.";
        return;
      }
      const pageSize = 10;
      const initialVisible = 3;
      if (!candleProgressExpanded) {
        const visibleRows = rows.slice(0, initialVisible);
        container.innerHTML = `${visibleRows.map((row) => {
          const percent = row.target > 0 ? Math.min(100, Math.round((row.count / row.target) * 100)) : 0;
          const readiness = row.ready ? "Ready for analysis" : "Collecting candles";
          const lastCandle = row.last_candle ?? "-";
          return `
            <div class="progress-card">
              <div class="progress-head">
                <strong>${row.symbol}</strong>
                <span>${row.count}/${row.target}</span>
              </div>
              <div class="progress-bar"><div class="progress-fill" style="width:${percent}%"></div></div>
              <div style="margin-top:10px; color:#6b7280; font-size:14px;">${row.exchange} | ${row.interval} | ${readiness}</div>
              <div style="margin-top:6px; color:#6b7280; font-size:13px;">Last candle: ${lastCandle}</div>
            </div>
          `;
        }).join("")}${rows.length > initialVisible ? `<div class="trade-controls"><button class="trade-button" onclick="expandCandleProgress()">See More</button><div class="trade-page">${rows.length - initialVisible} more stock(s)</div></div>` : ""}`;
        return;
      }

      const totalPages = Math.max(1, Math.ceil(rows.length / pageSize));
      if (candleProgressPage >= totalPages) {
        candleProgressPage = totalPages - 1;
      }
      const start = candleProgressPage * pageSize;
      const visibleRows = rows.slice(start, start + pageSize);
      container.innerHTML = `${visibleRows.map((row) => {
        const percent = row.target > 0 ? Math.min(100, Math.round((row.count / row.target) * 100)) : 0;
        const readiness = row.ready ? "Ready for analysis" : "Collecting candles";
        const lastCandle = row.last_candle ?? "-";
        return `
          <div class="progress-card">
            <div class="progress-head">
              <strong>${row.symbol}</strong>
              <span>${row.count}/${row.target}</span>
            </div>
            <div class="progress-bar"><div class="progress-fill" style="width:${percent}%"></div></div>
            <div style="margin-top:10px; color:#6b7280; font-size:14px;">${row.exchange} | ${row.interval} | ${readiness}</div>
            <div style="margin-top:6px; color:#6b7280; font-size:13px;">Last candle: ${lastCandle}</div>
          </div>
        `;
      }).join("")}
      <div class="trade-controls">
        <div style="display:flex; gap:8px; align-items:center;">
          <button class="trade-button" onclick="collapseCandleProgress()">See Less</button>
          ${rows.length > pageSize ? `<button class="trade-button" onclick="prevCandleProgressPage()" ${candleProgressPage === 0 ? "disabled" : ""}>Prev</button><button class="trade-button" onclick="nextCandleProgressPage(${totalPages})" ${candleProgressPage >= totalPages - 1 ? "disabled" : ""}>Next</button>` : ""}
        </div>
        <div class="trade-page">Page ${candleProgressPage + 1} of ${totalPages}</div>
      </div>`;
    }

    function expandCandleProgress() {
      candleProgressExpanded = true;
      candleProgressPage = 0;
      refreshStatus();
    }

    function collapseCandleProgress() {
      candleProgressExpanded = false;
      candleProgressPage = 0;
      refreshStatus();
    }

    function nextCandleProgressPage(totalPages) {
      if (candleProgressPage < totalPages - 1) {
        candleProgressPage += 1;
        refreshStatus();
      }
    }

    function prevCandleProgressPage() {
      if (candleProgressPage > 0) {
        candleProgressPage -= 1;
        refreshStatus();
      }
    }

    function renderPaperTrades(trades) {
      const container = document.getElementById("paperTrades");
      if (!trades.length) {
        container.textContent = "No paper trades yet.";
        return;
      }
      container.innerHTML = `<div class="trade-list">${trades.map((trade) => `
        <div class="trade-item">
          <div class="trade-time">${trade.timestamp || "-"}</div>
          <div>${trade.message}</div>
        </div>
      `).join("")}</div>`;
    }

    function renderSignalQuality() {
      const container = document.getElementById("signalQuality");
      const summary = signalQualityByPeriod[signalQualityPeriod] || signalQualityByPeriod.all || {};
      const totalTrades = Number(summary.total_trades || 0);
      if (!totalTrades) {
        container.textContent = "No closed paper trades yet.";
        return;
      }
      const exits = summary.exit_reasons || {};
      const noisy = Array.isArray(summary.noisy_symbols) ? summary.noisy_symbols : [];
      container.innerHTML = `
        <div class="signal-grid">
          <div class="signal-pill"><div class="label">Win Rate</div><div class="value">${Number(summary.win_rate_pct || 0).toFixed(2)}%</div></div>
          <div class="signal-pill"><div class="label">Avg Net P&L</div><div class="value">${Number(summary.avg_net_pnl || 0).toFixed(2)}</div></div>
          <div class="signal-pill"><div class="label">Profit Factor</div><div class="value">${Number(summary.profit_factor || 0).toFixed(3)}</div></div>
          <div class="signal-pill"><div class="label">Total Trades</div><div class="value">${totalTrades}</div></div>
          <div class="signal-pill"><div class="label">Target Hits</div><div class="value">${Number(exits.target_hit || 0)}</div></div>
          <div class="signal-pill"><div class="label">Stop Hits</div><div class="value">${Number(exits.stop_loss_hit || 0)}</div></div>
        </div>
        <div class="trade-time">Manual exits: ${Number(exits.manual_exit || 0)} | Other exits: ${Number(exits.other || 0)}</div>
        <div class="trade-time">Avg Win: ${Number(summary.avg_win || 0).toFixed(2)} | Avg Loss: ${Number(summary.avg_loss || 0).toFixed(2)}</div>
        <div class="signal-list">
          ${noisy.length ? noisy.map((item) => `<div class="signal-item"><strong>${item.symbol}</strong> | trades: ${item.trades} | win rate: ${Number(item.win_rate_pct || 0).toFixed(2)}% | avg pnl: ${Number(item.avg_net_pnl || 0).toFixed(2)}</div>`).join("") : '<div class="signal-item">No noisy symbols flagged yet.</div>'}
        </div>
      `;
    }

    function renderStrategyLeaderboard(rows) {
      const container = document.getElementById("strategyLeaderboard");
      if (!rows.length) {
        container.textContent = "No strategy stats yet.";
        return;
      }
      container.innerHTML = `<div class="strategy-board">${rows.map((row, idx) => `
        <div class="strategy-row">
          <div class="strategy-top">
            <div class="strategy-id">${idx + 1}. ${escapeHtml(row.strategy_id || "-")}</div>
            <div class="strategy-score">Score ${formatNum(row.score, 3)}</div>
          </div>
          <div class="trade-time">Net P&L: ${formatNum(row.net_pnl, 2)} | Live entries: ${Number(row.live_entries || 0)}</div>
          <div class="trade-time">Closed: ${Number(row.closed_trades || 0)} | Wins: ${Number(row.wins || 0)} | Losses: ${Number(row.losses || 0)} | Win rate: ${formatNum(row.win_rate_pct, 2)}%</div>
          <div class="trade-time">Avg P&L: ${formatNum(row.avg_net_pnl, 2)} | Profit factor: ${formatNum(row.profit_factor, 2)}</div>
          <div class="trade-time">Best: ${escapeHtml(row.best_symbol || "-")} | Weakest: ${escapeHtml(row.worst_symbol || "-")}</div>
        </div>
      `).join("")}</div>`;
    }

    function changeSignalQualityPeriod() {
      const selected = document.getElementById("signalQualityPeriod").value;
      signalQualityPeriod = selected || "all";
      renderSignalQuality();
    }

    function statusClass(status) {
      const normalized = String(status || "").toLowerCase();
      if (normalized.includes("entry-ready")) return "ready";
      if (normalized.includes("waiting") || normalized.includes("disabled")) return "waiting";
      return "no-entry";
    }

    function renderIndexOptionsScanner(rows) {
      const container = document.getElementById("indexOptionsScanner");
      if (!container) return;
      if (!Array.isArray(rows) || !rows.length) {
        container.textContent = "Index options scanner data not available yet.";
        return;
      }
      container.innerHTML = rows.map((row) => {
        const regime = String(row.regime || "-").toUpperCase();
        const side = row.option_side && row.option_side !== "-" ? row.option_side : "-";
        return `
          <div class="scanner-index-card">
            <div class="scanner-index-head">
              <div>
                <div class="scanner-index-title">${escapeHtml(row.symbol || "-")}</div>
                <div class="scanner-index-sub">${escapeHtml(row.exchange || "-")} underlying | ${escapeHtml(row.contract_exchange || "-")} options | ${escapeHtml(row.interval || "-")}</div>
              </div>
              <div class="scanner-status-pill ${statusClass(row.status)}">${escapeHtml(row.status || "-")}</div>
            </div>
            <div class="scanner-index-metrics">
              <div class="scanner-mini-metric">
                <div class="scanner-mini-label">Regime</div>
                <div class="scanner-mini-value">${escapeHtml(regime)}</div>
              </div>
              <div class="scanner-mini-metric">
                <div class="scanner-mini-label">Score / Side</div>
                <div class="scanner-mini-value">${formatNum(row.score, 1)} / ${escapeHtml(side)}</div>
              </div>
              <div class="scanner-mini-metric">
                <div class="scanner-mini-label">Close / RSI</div>
                <div class="scanner-mini-value">${formatNum(row.close, 2)} / ${formatNum(row.rsi14, 1)}</div>
              </div>
              <div class="scanner-mini-metric">
                <div class="scanner-mini-label">Momentum %</div>
                <div class="scanner-mini-value">${formatNum(row.momentum_pct, 4)}</div>
              </div>
              <div class="scanner-mini-metric">
                <div class="scanner-mini-label">EMA Gap %</div>
                <div class="scanner-mini-value">${formatNum(row.ema_gap_pct, 4)}</div>
              </div>
              <div class="scanner-mini-metric">
                <div class="scanner-mini-label">Candles</div>
                <div class="scanner-mini-value">${Number(row.candles || 0)}/${Number(row.required_candles || 0)}</div>
              </div>
            </div>
            <div class="scanner-reason">${escapeHtml(row.reason || "-")} Last candle: ${escapeHtml(row.last_candle || "-")}</div>
          </div>
        `;
      }).join("");
    }

    function renderUnderlyingQuote(quote) {
      if (!quote || quote.last_price === null || quote.last_price === undefined) {
        return `<div class="underlying-strip"><div><div class="underlying-title">Stock LTP</div><div class="underlying-price">Quote -</div></div><div class="underlying-change neutral">-</div></div>`;
      }
      const change = quote.change === null || quote.change === undefined ? null : Number(quote.change);
      const changePct = quote.change_pct === null || quote.change_pct === undefined ? null : Number(quote.change_pct);
      const directionClass = change === null || change === 0 ? "neutral" : (change > 0 ? "positive" : "negative");
      const sign = change !== null && change > 0 ? "+" : "";
      const changeText = change === null || changePct === null ? "-" : `${sign}${change.toFixed(2)} (${sign}${changePct.toFixed(2)}%)`;
      return `
        <div class="underlying-strip">
          <div>
            <div class="underlying-title">${escapeHtml(quote.symbol || "Stock")} LTP</div>
            <div class="underlying-price">${Number(quote.last_price).toFixed(2)}</div>
          </div>
          <div class="underlying-change ${directionClass}">${changeText}</div>
        </div>
      `;
    }

    function renderActiveTrades(trades) {
      const container = document.getElementById("activeTrades");
      if (!trades.length) {
        lastActiveSnapshots = {};
        container.textContent = "No active paper positions.";
        return;
      }
      const displayTrades = trades.map((trade) => {
        const key = `${trade.symbol}|${trade.tradingsymbol}`;
        const prev = lastActiveSnapshots[key];
        const copy = {...trade};
        if (prev) {
          const quoteStatus = String(copy.quote_status || "").toLowerCase();
          if (quoteStatus !== "live" && prev.ltp !== null && prev.ltp !== undefined) {
            copy.current_price = prev.ltp;
            const entry = Number(copy.entry_price || 0);
            const qty = Number(copy.quantity || 0);
            copy.unrealized_pnl = (Number(copy.current_price) - entry) * qty;
            copy.quote_status = "live";
          }
          if (!copy.underlying_quote && prev.underlying_quote) {
            copy.underlying_quote = prev.underlying_quote;
          }
        }
        return copy;
      });
      const nextSnapshots = {};
      container.innerHTML = `<div class="trade-list">${displayTrades.map((trade) => `
          <div class="trade-item active-trade-card">
            <div class="active-trade-head">
              <div class="active-trade-title">${trade.direction} ${trade.symbol || "-"}<br><span style="font-size:13px; font-weight:700; color:#334155;">${trade.tradingsymbol || "-"}</span></div>
              ${renderUnderlyingQuote(trade.underlying_quote)}
              <div class="active-trade-sub">Qty ${trade.quantity}</div>
            </div>
          <div class="active-metrics">
            <div class="active-metric">
              <div class="label">Entry</div>
              <div class="value">${Number(trade.entry_price).toFixed(2)}</div>
            </div>
            <div class="active-metric">
              <div class="label">LTP</div>
              <div class="value ${(() => {
                const key = `${trade.symbol}|${trade.tradingsymbol}`;
                const prev = lastActiveSnapshots[key];
                const curr = (trade.current_price === null || trade.current_price === undefined) ? null : Number(trade.current_price);
                if (!prev || prev.ltp === null || curr === null || curr === prev.ltp) return "";
                return curr > prev.ltp ? "flash-up" : "flash-down";
              })()}">${trade.current_price === null || trade.current_price === undefined ? "Quote -" : Number(trade.current_price).toFixed(2)}</div>
            </div>
            <div class="active-metric">
              <div class="label">Stop Loss</div>
              <div class="value">${Number(trade.stop_loss).toFixed(2)}</div>
            </div>
            <div class="active-metric">
              <div class="label">Target</div>
              <div class="value">${Number(trade.target_price).toFixed(2)}</div>
            </div>
          </div>
          <div class="active-meta">Symbol: ${trade.symbol} | Position Value: ${Number(trade.position_value || 0).toFixed(2)} | Quote: ${escapeHtml(trade.quote_status || "-")}</div>
          <div class="active-pnl ${(() => {
            const key = `${trade.symbol}|${trade.tradingsymbol}`;
            const prev = lastActiveSnapshots[key];
            const curr = trade.unrealized_pnl === null || trade.unrealized_pnl === undefined ? null : Number(trade.unrealized_pnl || 0);
            if (curr === null) return "";
            if (!prev || prev.pnl === null || curr === prev.pnl) return "";
            return curr > prev.pnl ? "flash-up" : "flash-down";
          })()}" style="color:${Number(trade.unrealized_pnl || 0) > 0 ? '#0f766e' : (Number(trade.unrealized_pnl || 0) < 0 ? '#b91c1c' : '#6b7280')}">Open-Trade P&L: ${trade.unrealized_pnl === null || trade.unrealized_pnl === undefined ? "Quote unavailable" : Number(trade.unrealized_pnl || 0).toFixed(2)}</div>
          <div class="active-meta">Opened: ${trade.opened_at || "-"}</div>
          <button class="trade-button exit" onclick="manualExit('${trade.symbol}', '${trade.tradingsymbol}')">Exit Position</button>
        </div>
      `).join("")}</div>`;
      for (const trade of displayTrades) {
        const key = `${trade.symbol}|${trade.tradingsymbol}`;
        nextSnapshots[key] = {
          ltp: (trade.current_price === null || trade.current_price === undefined) ? null : Number(trade.current_price),
          pnl: (trade.unrealized_pnl === null || trade.unrealized_pnl === undefined) ? null : Number(trade.unrealized_pnl || 0),
          underlying_quote: trade.underlying_quote || null,
        };
      }
      lastActiveSnapshots = nextSnapshots;
    }

    function renderCompletedTrades(trades) {
      const container = document.getElementById("completedTrades");
      if (!trades.length) {
        container.textContent = "No completed paper trades.";
        return;
      }
      const pageSize = 5;
      const initialVisible = 4;
      if (!completedTradesExpanded) {
        const visibleTrades = trades.slice(0, initialVisible);
        container.innerHTML = `<div class="trade-list">${visibleTrades.map((trade) => `
        <div class="trade-item">
          <div><strong>${trade.direction}</strong> ${trade.tradingsymbol}</div>
          <div class="trade-time">Qty: ${trade.quantity} | Entry: ${Number(trade.entry_price).toFixed(2)} | Exit: ${Number(trade.exit_price).toFixed(2)}</div>
          <div class="trade-time" style="color:${Number(trade.pnl || 0) > 0 ? '#0f766e' : (Number(trade.pnl || 0) < 0 ? '#b91c1c' : '#6b7280')}">Realized P&L: ${Number(trade.pnl || 0).toFixed(2)}</div>
          <div class="trade-time">Charges: ${Number(trade.total_charges || 0).toFixed(2)}</div>
          <div class="trade-time">Reason: ${trade.exit_reason || "-"}</div>
          <div class="trade-time">Closed: ${trade.closed_at || "-"}</div>
        </div>
      `).join("")}</div>${trades.length > initialVisible ? `<div class="trade-controls"><button class="trade-button" onclick="expandCompletedTrades()">See More</button><div class="trade-page">${trades.length - initialVisible} more trade(s)</div></div>` : ""}`;
        return;
      }

      const totalPages = Math.max(1, Math.ceil(trades.length / pageSize));
      if (completedTradesPage >= totalPages) {
        completedTradesPage = totalPages - 1;
      }
      const start = completedTradesPage * pageSize;
      const visibleTrades = trades.slice(start, start + pageSize);
      container.innerHTML = `<div class="trade-list">${visibleTrades.map((trade) => `
        <div class="trade-item">
          <div><strong>${trade.direction}</strong> ${trade.tradingsymbol}</div>
          <div class="trade-time">Qty: ${trade.quantity} | Entry: ${Number(trade.entry_price).toFixed(2)} | Exit: ${Number(trade.exit_price).toFixed(2)}</div>
          <div class="trade-time" style="color:${Number(trade.pnl || 0) > 0 ? '#0f766e' : (Number(trade.pnl || 0) < 0 ? '#b91c1c' : '#6b7280')}">Realized P&L: ${Number(trade.pnl || 0).toFixed(2)}</div>
          <div class="trade-time">Charges: ${Number(trade.total_charges || 0).toFixed(2)}</div>
          <div class="trade-time">Reason: ${trade.exit_reason || "-"}</div>
          <div class="trade-time">Closed: ${trade.closed_at || "-"}</div>
        </div>
      `).join("")}</div>
      <div class="trade-controls">
        <div style="display:flex; gap:8px; align-items:center;">
          <button class="trade-button" onclick="collapseCompletedTrades()">See Less</button>
          ${trades.length > pageSize ? `<button class="trade-button" onclick="prevCompletedTradesPage()" ${completedTradesPage === 0 ? "disabled" : ""}>Prev</button><button class="trade-button" onclick="nextCompletedTradesPage(${totalPages})" ${completedTradesPage >= totalPages - 1 ? "disabled" : ""}>Next</button>` : ""}
        </div>
        <div class="trade-page">Page ${completedTradesPage + 1} of ${totalPages}</div>
      </div>`;
    }

    function renderWatchlist(items) {
      const container = document.getElementById("watchlistChips");
      watchlistSymbols = new Set(
        (items || [])
          .filter((item) => item.enabled !== false)
          .map((item) => String(item.symbol || "").toUpperCase())
      );
      if (!items.length) {
        container.textContent = "No watchlist symbols yet.";
        rerenderDailyScan();
        return;
      }
      container.innerHTML = items
        .filter((item) => item.enabled !== false)
        .map((item) => `<div class="chip">${escapeHtml(item.symbol)} | ${escapeHtml(item.interval || '1minute')} <button class="chip-remove" title="Remove from watchlist" onclick='removeWatchSymbol(${JSON.stringify(String(item.symbol || ""))})'>&times;</button></div>`)
        .join("");
      rerenderDailyScan();
    }

    function renderPaperAccount(account) {
      const container = document.getElementById("paperAccount");
      const startingCapital = Number(account.starting_capital || 0);
      const realizedPnl = Number(account.realized_pnl || 0);
      const availableBalance = Number(account.available_balance || 0);
      const capitalCommitted = Number(account.capital_committed || 0);
      const unrealizedPnl = Number(account.unrealized_pnl || 0);
      const currentCapital = Number(account.current_capital || (availableBalance + capitalCommitted));
      const pnlColor = realizedPnl > 0 ? "#0f766e" : (realizedPnl < 0 ? "#b91c1c" : "#1f2937");
      const unrealizedColor = unrealizedPnl > 0 ? "#0f766e" : (unrealizedPnl < 0 ? "#b91c1c" : "#1f2937");
      const realizedClass = realizedPnl > 0 ? "good" : (realizedPnl < 0 ? "bad" : "neutral");
      const unrealizedClass = unrealizedPnl > 0 ? "good" : (unrealizedPnl < 0 ? "bad" : "neutral");
      container.innerHTML = `
        <div class="metric-card neutral">
          <div class="metric-label">Base Capital</div>
          <div class="metric-value">${startingCapital.toFixed(2)}</div>
          <div class="metric-sub">Initial paper capital baseline</div>
        </div>
        <div class="metric-card neutral">
          <div class="metric-label">Trading Balance</div>
          <div class="metric-value">${availableBalance.toFixed(2)}</div>
          <div class="metric-sub">Free funds available for new entries</div>
        </div>
        <div class="metric-card ${realizedClass}">
          <div class="metric-label">Realized P&L</div>
          <div class="metric-value" style="color:${pnlColor};">${realizedPnl.toFixed(2)}</div>
          <div class="metric-sub">Closed paper trades only</div>
        </div>
        <div class="metric-card ${unrealizedClass}">
          <div class="metric-label">Open-Trade P&L</div>
          <div class="metric-value" style="color:${unrealizedColor};">${unrealizedPnl.toFixed(2)}</div>
          <div class="metric-sub">Running profit or loss on active paper trades</div>
        </div>
        <div class="metric-card neutral">
          <div class="metric-label">Capital Reserved</div>
          <div class="metric-value">${capitalCommitted.toFixed(2)}</div>
          <div class="metric-sub">Capital tied up in active positions</div>
        </div>
        <div class="metric-card neutral">
          <div class="metric-label">Current Capital</div>
          <div class="metric-value">${currentCapital.toFixed(2)}</div>
          <div class="metric-sub">Trading balance + capital reserved</div>
        </div>
      `;
    }

    async function refreshStatus() {
      try {
        const data = await api("/api/status");
        renderStatus(data);
      } catch (error) {
        document.getElementById("logTail").textContent = "Dashboard refresh failed: " + error.message;
      }
    }

    async function startEngine() {
      const data = await api("/api/start", "POST", {});
      renderStatus(data);
    }

    async function stopEngine() {
      const data = await api("/api/stop", "POST");
      renderStatus(data);
    }

    async function manualExit(symbol, tradingsymbol) {
      if (!confirm(`Exit paper position ${tradingsymbol || symbol}?`)) {
        return;
      }
      try {
        const data = await api("/api/manual-exit", "POST", {symbol, tradingsymbol});
        document.getElementById("logTail").textContent = `${data.message || "Manual exit queued."}\n\n` + document.getElementById("logTail").textContent;
        setTimeout(refreshStatus, 1200);
      } catch (error) {
        alert(error.message);
      }
    }

    async function newSession() {
      try {
        const data = await api("/api/new-session", "POST");
        renderStatus(data);
      } catch (error) {
        alert(error.message);
      }
    }

    async function addWatchSymbol() {
      const symbolInput = document.getElementById("newSymbol");
      const intervalInput = document.getElementById("newInterval");
      const symbol = symbolInput.value.trim().toUpperCase();
      if (!symbol) {
        alert("Enter a stock symbol first.");
        return;
      }
      if (!selectedSuggestion || selectedSuggestion !== symbol) {
        alert("Please select a valid symbol from the dropdown suggestions.");
        return;
      }
      try {
        const data = await api("/api/watchlist", "POST", {action: "add", symbol, interval: intervalInput.value});
        symbolInput.value = "";
        selectedSuggestion = "";
        hideSuggestions();
        renderStatus(data);
        await refreshDailyScan(false);
        alert("Stock added to watchlist. Changes applied automatically.");
      } catch (error) {
        alert(error.message);
      }
    }

    function hideSuggestions() {
      const list = document.getElementById("symbolSuggestions");
      list.style.display = "none";
      list.innerHTML = "";
    }

    function renderSuggestions(items) {
      const list = document.getElementById("symbolSuggestions");
      if (!items.length) {
        hideSuggestions();
        return;
      }
      list.innerHTML = items.map((symbol) => `<div class="suggest-item" onclick="chooseSuggestion('${symbol}')">${symbol}</div>`).join("");
      list.style.display = "block";
    }

    function chooseSuggestion(symbol) {
      const input = document.getElementById("newSymbol");
      input.value = symbol;
      selectedSuggestion = symbol;
      hideSuggestions();
    }

    async function refreshSymbolSuggestions() {
      const input = document.getElementById("newSymbol");
      const q = input.value.trim().toUpperCase();
      if (!q || q.length < 1) {
        latestSymbolSuggestions = [];
        selectedSuggestion = "";
        hideSuggestions();
        return;
      }
      if (selectedSuggestion && selectedSuggestion !== q) {
        selectedSuggestion = "";
      }
      try {
        const payload = await api(`/api/symbol-suggestions?q=${encodeURIComponent(q)}`);
        latestSymbolSuggestions = Array.isArray(payload.suggestions) ? payload.suggestions : [];
        renderSuggestions(latestSymbolSuggestions);
      } catch (_err) {
        hideSuggestions();
      }
    }

    function expandCompletedTrades() {
      completedTradesExpanded = true;
      completedTradesPage = 0;
      refreshStatus();
    }

    function collapseCompletedTrades() {
      completedTradesExpanded = false;
      completedTradesPage = 0;
      refreshStatus();
    }

    function nextCompletedTradesPage(totalPages) {
      if (completedTradesPage < totalPages - 1) {
        completedTradesPage += 1;
        refreshStatus();
      }
    }

    function prevCompletedTradesPage() {
      if (completedTradesPage > 0) {
        completedTradesPage -= 1;
        refreshStatus();
      }
    }

    function toggleLog() {
      const shell = document.getElementById("logShell");
      const toggle = document.getElementById("logToggle");
      shell.classList.toggle("maximized");
      toggle.textContent = shell.classList.contains("maximized") ? "Restore Log" : "Maximize Log";
    }

    document.addEventListener("click", (event) => {
      const input = document.getElementById("newSymbol");
      const list = document.getElementById("symbolSuggestions");
      if (!input || !list) {
        return;
      }
      if (event.target !== input && !list.contains(event.target)) {
        hideSuggestions();
      }
    });

    document.addEventListener("DOMContentLoaded", () => {
      const symbolInput = document.getElementById("newSymbol");
      if (symbolInput) {
        symbolInput.addEventListener("input", () => {
          if (suggestionFetchTimer) {
            clearTimeout(suggestionFetchTimer);
          }
          suggestionFetchTimer = setTimeout(refreshSymbolSuggestions, 120);
        });
        symbolInput.addEventListener("focus", refreshSymbolSuggestions);
        symbolInput.addEventListener("keydown", (event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            addWatchSymbol();
          }
        });
      }
    });

    document.getElementById("scanUniverse").addEventListener("change", () => refreshDailyScan(false));
    document.getElementById("scanInterval").addEventListener("change", () => refreshDailyScan(false));
    document.getElementById("scanTop").addEventListener("change", () => refreshDailyScan(false));

    refreshStatus();
    refreshLiveTicks();
    refreshDailyScan(false);
    refreshGoldenCrossover(false);
    setInterval(refreshStatus, 3000);
    setInterval(refreshLiveTicks, 1000);
    setInterval(() => refreshDailyScan(false), 60000);
    setInterval(() => refreshGoldenCrossover(false), 600000);
  </script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            html = HTML_PAGE.replace("__CONFIG_PATH__", str(DEFAULT_CONFIG))
            self._respond_html(html)
            return

        if parsed.path == "/api/status":
            self._respond_json(SUPERVISOR.status())
            return
        if parsed.path == "/api/live-ticks":
            self._respond_json(SUPERVISOR.live_ticks())
            return
        if parsed.path == "/api/token-status":
            self._respond_json(SUPERVISOR.zerodha_token_status(SUPERVISOR.broker_health_status()))
            return
        if parsed.path == "/api/signal-quality":
            query = parse_qs(parsed.query)
            period = query.get("period", ["all"])[0]
            self._respond_json(SUPERVISOR.signal_quality_summary(period))
            return
        if parsed.path == "/api/daily-scan":
            query = parse_qs(parsed.query)
            universe = str(query.get("universe", ["nifty250"])[0]).strip().lower()
            requested_interval = str(query.get("interval", ["2m"])[0]).strip().lower()
            top_raw = query.get("top", ["5"])[0]
            try:
                top_n = max(int(top_raw), 1)
            except ValueError:
                top_n = 5
            max_symbols = 30
            try:
                scan_result = _scan_engulfing_with_timeout(
                    universe=universe if universe in {"nifty100", "nifty250"} else "nifty250",
                    interval=requested_interval,
                    max_symbols=max_symbols,
                    min_score=60.0,
                    timeout_sec=35,
                )
            except FuturesTimeoutError:
                payload = {
                    "generated_at": _now_ist_iso(),
                    "interval": requested_interval,
                    "universe": universe if universe in {"nifty100", "nifty250"} else "nifty250",
                    "universe_source": "nse_largemidcap250_api",
                    "universe_count": max_symbols,
                    "market_open_ist": False,
                    "message": "Scanner timed out. Try again or reduce load.",
                    "market_regime": {
                        "symbol": "NIFTY 50",
                        "regime": "scanner_timeout",
                        "close": 0,
                        "ema20": 0,
                        "ema50": 0,
                        "rsi14": 0,
                    },
                    "actionable": [],
                    "watchlist": [
                        {
                            "symbol": "-",
                            "status": "skip",
                            "score": 0,
                            "direction": "-",
                            "close": 0,
                            "reason": "Scanner request timed out before data could be fetched.",
                        }
                    ],
                }
                self._respond_json(payload)
                return
            except Exception as exc:
                payload = {
                    "generated_at": _now_ist_iso(),
                    "interval": requested_interval,
                    "universe": universe if universe in {"nifty100", "nifty250"} else "nifty250",
                    "universe_source": "nse_largemidcap250_api",
                    "universe_count": max_symbols,
                    "market_open_ist": False,
                    "message": f"Scanner data unavailable: {exc}",
                    "market_regime": {
                        "symbol": "NIFTY 50",
                        "regime": "scanner_error",
                        "close": 0,
                        "ema20": 0,
                        "ema50": 0,
                        "rsi14": 0,
                    },
                    "actionable": [],
                    "watchlist": [
                        {
                            "symbol": "-",
                            "status": "skip",
                            "score": 0,
                            "direction": "-",
                            "close": 0,
                            "reason": f"Scanner fetch failed: {exc}",
                        }
                    ],
                }
                self._respond_json(payload)
                return
            signals = list(scan_result.get("actionable", []))
            actionable: list[dict[str, Any]] = []
            watch_rows: list[dict[str, Any]] = list(scan_result.get("watchlist", []))
            for rank, signal in enumerate(signals[:top_n], start=1):
                row = dict(signal)
                side = "BUY"
                direction = str(row.get("direction", "NEUTRAL"))
                setup = f"{requested_interval}_bearish_engulfing" if direction == "BEARISH" else f"{requested_interval}_bullish_engulfing"
                if direction == "BEARISH":
                    setup += "_buy_pe_atm"
                else:
                    setup += "_buy_ce_atm"
                signal_price = float(row.get("signal_price", row.get("close", 0.0)) or 0.0)
                candle_pct = float(row.get("candle_length_pct", 0.0) or 0.0)
                actionable.append(
                    {
                        "rank": rank,
                        "symbol": row["symbol"],
                        "direction": side,
                        "entry": signal_price,
                        "stop_loss": round(signal_price * (1 - max(candle_pct * 0.5, 0.5) / 100.0), 2),
                        "target": round(signal_price * (1 + max(candle_pct * 1.0, 1.0) / 100.0), 2),
                        "score": row["score"],
                        "setup": setup,
                        "reason": row["reason"],
                        "quantity": "-",
                    }
                )
            now_ist = datetime.now(IST)
            market_open = (now_ist.hour, now_ist.minute) >= (9, 15) and (now_ist.hour, now_ist.minute) <= (15, 30)
            no_signal_message = (
                f"No qualifying {requested_interval} engulfing signals in the latest completed candles."
                if market_open
                else f"Market is closed now (IST). No fresh {requested_interval} scan setups to act on."
            )
            if not actionable:
                watch_rows.append(
                    {
                        "symbol": "-",
                        "status": "watch",
                        "score": 0,
                        "direction": "-",
                        "close": 0,
                        "reason": no_signal_message,
                    }
                )
            payload = {
                "generated_at": _now_ist_iso(),
                "interval": requested_interval,
                "universe": universe if universe in {"nifty100", "nifty250"} else "nifty250",
                "universe_source": "nse_largemidcap250_api",
                "universe_count": int(scan_result.get("universe_count", max_symbols)),
                "scanned_count": int(scan_result.get("scanned_count", 0)),
                "market_open_ist": market_open,
                "message": "" if actionable else no_signal_message,
                "market_regime": {
                    "symbol": "NIFTY 50",
                    "regime": "scanner_mode",
                    "close": 0,
                    "ema20": 0,
                    "ema50": 0,
                    "rsi14": 0
                },
                "actionable": actionable,
                "watchlist": watch_rows,
            }
            self._respond_json(payload)
            return
        if parsed.path == "/api/golden-crossover":
            self._respond_json(
                {
                    "generated_at": _now_ist_iso(),
                    "index_name": "NIFTY LARGEMIDCAP 250",
                    "ema_fast": 20,
                    "ema_slow": 200,
                    "stocks": [],
                    "count": 0,
                    "message": "Golden crossover scan is not enabled in OptionTrader yet.",
                }
            )
            return
        if parsed.path == "/api/symbol-suggestions":
            query = parse_qs(parsed.query)
            q = query.get("q", [""])[0]
            suggestions = SUPERVISOR.symbol_suggestions(q)
            self._respond_json({"query": q, "suggestions": suggestions})
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/start":
            payload = self._read_json_body()
            config_path = payload.get("config_path") if isinstance(payload, dict) else None
            mode = payload.get("mode", "paper") if isinstance(payload, dict) else "paper"
            self._respond_json(SUPERVISOR.start(config_path=config_path, mode=mode))
            return

        if parsed.path == "/api/stop":
            self._respond_json(SUPERVISOR.stop())
            return

        if parsed.path == "/api/new-session":
            try:
                self._respond_json(SUPERVISOR.new_session())
            except RuntimeError as exc:
                self._respond_error(HTTPStatus.BAD_REQUEST, str(exc))
            return

        if parsed.path == "/api/watchlist":
            try:
                payload = self._read_json_body()
                action = str(payload.get("action", "add")).strip().lower()
                if action == "remove":
                    self._respond_json(
                        SUPERVISOR.remove_watch_item(symbol=str(payload.get("symbol", "")))
                    )
                    return
                self._respond_json(
                    SUPERVISOR.add_watch_item(
                        symbol=str(payload.get("symbol", "")),
                        exchange=str(payload.get("exchange", "NSE")),
                        interval=str(payload.get("interval", "1minute")),
                    )
                )
            except RuntimeError as exc:
                self._respond_error(HTTPStatus.BAD_REQUEST, str(exc))
            return

        if parsed.path == "/api/exit":
            try:
                payload = self._read_json_body()
                self._respond_json(
                    SUPERVISOR.manual_exit(
                        symbol=str(payload.get("symbol", "")),
                        tradingsymbol=str(payload.get("tradingsymbol", "")),
                    )
                )
            except RuntimeError as exc:
                self._respond_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/manual-exit":
            try:
                payload = self._read_json_body()
                self._respond_json(
                    SUPERVISOR.manual_exit(
                        symbol=str(payload.get("symbol", "")),
                        tradingsymbol=str(payload.get("tradingsymbol", "")),
                    )
                )
            except RuntimeError as exc:
                self._respond_error(HTTPStatus.BAD_REQUEST, str(exc))
            return

        if parsed.path == "/api/zerodha-token":
            try:
                payload = self._read_json_body()
                raw_input = str(payload.get("token_input", payload.get("request_token", "")))
                self._respond_json(SUPERVISOR.refresh_zerodha_token(raw_input))
            except (RuntimeError, ValueError, InputException, TokenException) as exc:
                self._respond_error(HTTPStatus.BAD_REQUEST, str(exc))
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def _respond_html(self, html: str) -> None:
        encoded = html.encode("utf-8")
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            return

    def _respond_json(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            return

    def _respond_error(self, status: HTTPStatus, message: str) -> None:
        encoded = message.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            return


def main() -> int:
    fixed_port = "8877"
    configured_port = os.getenv("ALGOTRADER_DASHBOARD_PORT", fixed_port)
    if configured_port != fixed_port:
        print(
            f"Ignoring ALGOTRADER_DASHBOARD_PORT={configured_port}. "
            f"OptionTrader dashboard is fixed to {fixed_port}."
        )
    port = int(fixed_port)
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    try:
        if not SUPERVISOR.status().get("running", False):
            SUPERVISOR.start(config_path=str(DEFAULT_CONFIG), mode="paper")
    except Exception as exc:
        print(f"Auto-start warning: could not start engine automatically: {exc}")
    print(f"OptionTrader dashboard running at http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            SUPERVISOR.stop()
        except Exception:
            pass
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



