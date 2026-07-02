from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import requests
import yfinance as yf


NSE_INDEX_API_BASE = "https://www.nseindia.com/api/equity-stockIndices?index="
POPULAR_NSE_SYMBOLS = [
    "RELIANCE",
    "TCS",
    "HDFCBANK",
    "ICICIBANK",
    "INFY",
    "SBIN",
    "BHARTIARTL",
    "ITC",
    "LT",
    "KOTAKBANK",
    "AXISBANK",
    "BAJFINANCE",
    "TATAMOTORS",
    "TATAPOWER",
    "JSWINFRA",
    "SWIGGY",
    "AUBANK",
    "CIPLA",
    "BIOCON",
    "SUNPHARMA",
    "NIFTY",
    "BANKNIFTY",
]


@dataclass
class ScannerSignal:
    symbol: str
    direction: str
    score: float
    reason: str
    signal_price: float
    candle_length: float
    candle_length_pct: float
    volume_ratio: float


_SYMBOL_CACHE: list[str] = []
_SYMBOL_CACHE_TS: datetime | None = None


def fetch_nifty250_symbols(timeout_sec: int = 20) -> list[str]:
    global _SYMBOL_CACHE, _SYMBOL_CACHE_TS
    now = datetime.now()
    if _SYMBOL_CACHE and _SYMBOL_CACHE_TS and (now - _SYMBOL_CACHE_TS) < timedelta(hours=6):
        return _SYMBOL_CACHE

    session = requests.Session()
    headers = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "accept": "application/json,text/plain,*/*",
        "accept-language": "en-US,en;q=0.9",
        "referer": "https://www.nseindia.com/market-data/live-equity-market",
    }
    try:
        session.get("https://www.nseindia.com", headers=headers, timeout=timeout_sec)
        url = NSE_INDEX_API_BASE + requests.utils.quote("NIFTY LARGEMIDCAP 250")
        response = session.get(url, headers=headers, timeout=timeout_sec)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return _SYMBOL_CACHE or sorted(set(POPULAR_NSE_SYMBOLS))

    symbols: list[str] = []
    for row in payload.get("data", []):
        symbol = str(row.get("symbol", "")).strip().upper()
        if symbol and not symbol.startswith("NIFTY "):
            symbols.append(symbol)
    _SYMBOL_CACHE = sorted(set(symbols))
    _SYMBOL_CACHE_TS = now
    return _SYMBOL_CACHE


def scan_engulfing(
    *,
    universe: str = "nifty250",
    interval: str = "2m",
    max_symbols: int = 80,
    min_score: float = 60.0,
) -> dict[str, Any]:
    symbols = _symbols_for_universe(universe)
    if max_symbols > 0:
        symbols = symbols[:max_symbols]
    candles_by_symbol = _download_batch(symbols, interval)
    actionable: list[dict[str, Any]] = []
    watchlist: list[dict[str, Any]] = []
    for symbol in symbols:
        candles = candles_by_symbol.get(symbol, pd.DataFrame())
        candidate = _score_engulfing(symbol, candles, interval)
        if not candidate:
            continue
        if candidate["status"] == "actionable" and float(candidate["score"]) >= min_score:
            actionable.append(candidate)
        else:
            watchlist.append(candidate)
    actionable.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
    watchlist.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
    return {
        "interval": interval,
        "universe": universe,
        "universe_count": len(symbols),
        "scanned_count": len(candles_by_symbol),
        "actionable": actionable,
        "watchlist": watchlist[:20],
    }


def _download_2m(symbol: str) -> pd.DataFrame:
    ticker = f"{symbol}.NS"
    data = yf.download(
        ticker,
        period="5d",
        interval="2m",
        progress=False,
        auto_adjust=False,
        threads=False,
    )
    if data is None or data.empty:
        return pd.DataFrame()
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = [col[0] for col in data.columns]
    required = ["Open", "High", "Low", "Close", "Volume"]
    for column in required:
        if column not in data.columns:
            return pd.DataFrame()
    df = data.rename(
        columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    )
    return df[["open", "high", "low", "close", "volume"]].dropna().reset_index(drop=True)


def _symbols_for_universe(universe: str) -> list[str]:
    mode = universe.strip().lower()
    symbols = fetch_nifty250_symbols()
    if not symbols:
        symbols = sorted(set(POPULAR_NSE_SYMBOLS))
    preferred = [symbol for symbol in POPULAR_NSE_SYMBOLS if symbol in symbols]
    symbols = preferred + [symbol for symbol in symbols if symbol not in preferred]
    if mode == "nifty100":
        return symbols[:100]
    return symbols


def _interval_settings(interval: str) -> tuple[str, str]:
    normalized = interval.strip().lower()
    mapping = {
        "2m": ("2m", "5d"),
        "5m": ("5m", "5d"),
        "15m": ("15m", "1mo"),
        "1h": ("60m", "3mo"),
        "1d": ("1d", "1y"),
    }
    return mapping.get(normalized, ("2m", "5d"))


def _download_batch(symbols: list[str], interval: str) -> dict[str, pd.DataFrame]:
    if not symbols:
        return {}
    yf_interval, period = _interval_settings(interval)
    ticker_map = {f"{symbol}.NS": symbol for symbol in symbols if symbol not in {"NIFTY", "BANKNIFTY"}}
    if "NIFTY" in symbols:
        ticker_map["^NSEI"] = "NIFTY"
    if "BANKNIFTY" in symbols:
        ticker_map["^NSEBANK"] = "BANKNIFTY"
    if not ticker_map:
        return {}
    try:
        data = yf.download(
            tickers=list(ticker_map.keys()),
            period=period,
            interval=yf_interval,
            progress=False,
            auto_adjust=False,
            threads=True,
            group_by="ticker",
            timeout=8,
        )
    except Exception:
        return {}
    output: dict[str, pd.DataFrame] = {}
    if data is None or data.empty:
        return output
    if isinstance(data.columns, pd.MultiIndex):
        for ticker, symbol in ticker_map.items():
            if ticker not in data.columns.get_level_values(0):
                continue
            frame = data[ticker].copy()
            normalized = _normalize_downloaded_frame(frame)
            if not normalized.empty:
                output[symbol] = normalized
    elif len(ticker_map) == 1:
        symbol = next(iter(ticker_map.values()))
        normalized = _normalize_downloaded_frame(data.copy())
        if not normalized.empty:
            output[symbol] = normalized
    return output


def _normalize_downloaded_frame(data: pd.DataFrame) -> pd.DataFrame:
    if data is None or data.empty:
        return pd.DataFrame()
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = [col[0] for col in data.columns]
    required = ["Open", "High", "Low", "Close", "Volume"]
    for column in required:
        if column not in data.columns:
            return pd.DataFrame()
    df = data.rename(
        columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    )
    return df[["open", "high", "low", "close", "volume"]].dropna().reset_index(drop=True)


def _score_engulfing(symbol: str, candles: pd.DataFrame, interval: str) -> dict[str, Any] | None:
    if candles.empty or len(candles.index) < 2:
        return None
    prev = candles.iloc[-2]
    curr = candles.iloc[-1]
    prev_open = float(prev["open"])
    prev_close = float(prev["close"])
    curr_open = float(curr["open"])
    curr_close = float(curr["close"])
    curr_high = float(curr["high"])
    curr_low = float(curr["low"])
    prev_volume = float(prev.get("volume", 0.0) or 0.0)
    curr_volume = float(curr.get("volume", 0.0) or 0.0)
    candle_length = abs(curr_close - curr_open)
    candle_length_pct = (candle_length / max(curr_open, 1e-9)) * 100.0
    volume_ratio = (curr_volume / prev_volume) if prev_volume > 0 else 0.0

    bullish = (
        prev_close < prev_open
        and curr_close > curr_open
        and curr_open <= prev_close
        and curr_close >= prev_open
    )
    bearish = (
        prev_close > prev_open
        and curr_close < curr_open
        and curr_open >= prev_close
        and curr_close <= prev_open
    )
    direction = "BULLISH" if bullish else ("BEARISH" if bearish else "NEUTRAL")
    body_score = min(candle_length_pct * 18.0, 25.0)
    range_pct = ((curr_high - curr_low) / max(curr_open, 1e-9)) * 100.0
    range_score = min(range_pct * 7.0, 15.0)
    volume_score = min(max(volume_ratio - 1.0, 0.0) * 20.0, 20.0)
    score = 35.0 + body_score + range_score + volume_score
    if bullish or bearish:
        score += 20.0
    score = max(min(score, 99.0), 0.0)
    status = "actionable" if bullish or bearish else "watch"
    if status == "actionable":
        reason = (
            f"{interval} {direction.lower()} engulfing candle, "
            f"body {candle_length_pct:.2f}%, volume ratio {volume_ratio:.2f}"
        )
    else:
        reason = (
            f"No confirmed engulfing candle; latest body {candle_length_pct:.2f}%, "
            f"range {range_pct:.2f}%, volume ratio {volume_ratio:.2f}"
        )
    return {
        "symbol": symbol,
        "status": status,
        "direction": direction,
        "score": round(score, 2),
        "signal_price": round(curr_close, 2),
        "close": round(curr_close, 2),
        "candle_length": round(candle_length, 4),
        "candle_length_pct": round(candle_length_pct, 4),
        "volume_ratio": round(volume_ratio, 4),
        "reason": reason,
    }


def _detect_signal(symbol: str, candles: pd.DataFrame) -> ScannerSignal | None:
    if len(candles) < 4:
        return None
    c3 = candles.iloc[-4]  # 2 candles ago block start
    c2 = candles.iloc[-3]
    c1 = candles.iloc[-2]
    c0 = candles.iloc[-1]  # latest completed signal candle

    # Bearish reversal pattern (user-provided shape adapted to 2m candles)
    bearish = (
        float(c2["close"]) > float(c2["open"]) and
        float(c1["open"]) >= float(c2["close"]) and
        float(c1["close"]) > float(c1["open"]) and
        float(c0["open"]) > float(c1["close"]) and
        float(c0["close"]) < float(c1["open"]) and
        float(c0["close"]) < float(c0["open"])
    )

    # Bullish mirror pattern for balanced strategy
    bullish = (
        float(c2["close"]) < float(c2["open"]) and
        float(c1["open"]) <= float(c2["close"]) and
        float(c1["close"]) < float(c1["open"]) and
        float(c0["open"]) < float(c1["close"]) and
        float(c0["close"]) > float(c1["open"]) and
        float(c0["close"]) > float(c0["open"])
    )

    volume_ratio = 0.0
    prev_vol = float(c1["volume"])
    if prev_vol > 0:
        volume_ratio = float(c0["volume"]) / prev_vol
    if volume_ratio < 1.2:
        return None

    if not bearish and not bullish:
        return None

    candle_length = abs(float(c0["close"]) - float(c0["open"]))
    if candle_length <= 0:
        return None
    candle_length_pct = (candle_length / max(float(c0["open"]), 1e-9)) * 100.0
    body_strength = min(candle_length_pct * 20.0, 25.0)
    volume_strength = min((volume_ratio - 1.2) * 40.0, 25.0)
    score = 55.0 + body_strength + volume_strength
    score = max(min(score, 99.0), 0.0)

    direction = "BEARISH" if bearish else "BULLISH"
    reason = (
        f"2m {'bearish' if bearish else 'bullish'} reversal pattern with volume ratio "
        f"{volume_ratio:.2f} and candle length {candle_length_pct:.2f}%"
    )
    return ScannerSignal(
        symbol=symbol,
        direction=direction,
        score=score,
        reason=reason,
        signal_price=float(c0["close"]),
        candle_length=candle_length,
        candle_length_pct=candle_length_pct,
        volume_ratio=volume_ratio,
    )


def scan_nifty250_2m(max_symbols: int = 250, min_score: float = 60.0) -> list[ScannerSignal]:
    result = scan_engulfing(
        universe="nifty250",
        interval="2m",
        max_symbols=max_symbols,
        min_score=min_score,
    )
    signals: list[ScannerSignal] = []
    for row in result.get("actionable", []):
        signals.append(
            ScannerSignal(
                symbol=str(row["symbol"]),
                direction=str(row["direction"]),
                score=float(row["score"]),
                reason=str(row["reason"]),
                signal_price=float(row["signal_price"]),
                candle_length=float(row["candle_length"]),
                candle_length_pct=float(row["candle_length_pct"]),
                volume_ratio=float(row["volume_ratio"]),
            )
        )
    signals.sort(key=lambda item: item.score, reverse=True)
    return signals


def signal_to_dict(signal: ScannerSignal) -> dict[str, Any]:
    return {
        "symbol": signal.symbol,
        "direction": signal.direction,
        "score": round(signal.score, 2),
        "reason": signal.reason,
        "signal_price": round(signal.signal_price, 2),
        "candle_length": round(signal.candle_length, 4),
        "candle_length_pct": round(signal.candle_length_pct, 4),
        "volume_ratio": round(signal.volume_ratio, 4),
    }
