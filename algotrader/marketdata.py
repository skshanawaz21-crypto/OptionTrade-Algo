from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd


def completed_intraday_candles(
    candles: pd.DataFrame,
    interval: str,
    as_of: datetime,
) -> pd.DataFrame:
    normalized = interval.lower().strip().replace("_", "").replace("-", "")
    minute_aliases = {
        "1m": 1,
        "2m": 2,
        "5m": 5,
        "15m": 15,
        "30m": 30,
        "60m": 60,
    }
    if normalized.endswith("minute"):
        raw_minutes = normalized.removesuffix("minute")
        minutes = int(raw_minutes) if raw_minutes.isdigit() else 0
    else:
        minutes = minute_aliases.get(normalized, 0)
    if minutes <= 0:
        return candles.copy()
    if candles.empty or "date" not in candles.columns:
        return candles.iloc[0:0].copy()

    timestamps = pd.to_datetime(candles["date"], errors="coerce")
    as_of_timestamp = pd.Timestamp(as_of)
    candle_timezone = timestamps.dt.tz
    if candle_timezone is not None:
        if as_of_timestamp.tzinfo is None:
            as_of_timestamp = as_of_timestamp.tz_localize(candle_timezone)
        else:
            as_of_timestamp = as_of_timestamp.tz_convert(candle_timezone)
    elif as_of_timestamp.tzinfo is not None:
        as_of_timestamp = as_of_timestamp.tz_convert("Asia/Kolkata").tz_localize(None)

    completed = timestamps.notna() & (
        timestamps + pd.Timedelta(minutes=minutes) <= as_of_timestamp
    )
    return candles.loc[completed].copy().reset_index(drop=True)


@dataclass
class CandleSnapshot:
    candles: pd.DataFrame
    just_started_new_candle: bool


class LocalCandleStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def record_price(
        self,
        exchange: str,
        symbol: str,
        interval: str,
        price: float,
        timestamp: datetime,
    ) -> CandleSnapshot:
        path = self._file_path(exchange, symbol, interval)
        df = self._load(path)
        bucket = self._bucket_start(timestamp, interval)
        just_started_new_candle = False

        if df.empty or pd.Timestamp(df.iloc[-1]["date"]) != bucket:
            row = {
                "date": bucket,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 0.0,
            }
            df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
            just_started_new_candle = True
        else:
            last_index = df.index[-1]
            df.at[last_index, "high"] = max(float(df.at[last_index, "high"]), price)
            df.at[last_index, "low"] = min(float(df.at[last_index, "low"]), price)
            df.at[last_index, "close"] = price

        df = df.sort_values("date").reset_index(drop=True)
        df.to_csv(path, index=False)
        return CandleSnapshot(candles=df.copy(), just_started_new_candle=just_started_new_candle)

    def _file_path(self, exchange: str, symbol: str, interval: str) -> Path:
        safe_symbol = symbol.replace(" ", "_")
        return self.root / f"{exchange}_{safe_symbol}_{interval}.csv"

    def _load(self, path: Path) -> pd.DataFrame:
        if not path.exists():
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        df = pd.read_csv(path, parse_dates=["date"])
        df["date"] = pd.to_datetime(df["date"], errors="coerce", utc=True).dt.tz_convert(None)
        expected = ["date", "open", "high", "low", "close", "volume"]
        for column in expected:
            if column not in df.columns:
                df[column] = 0.0 if column != "date" else pd.NaT
        return df[expected]

    def _bucket_start(self, timestamp: datetime, interval: str) -> pd.Timestamp:
        normalized = interval.lower().strip()
        if normalized.endswith("minute"):
            minute_value = int(normalized.replace("minute", ""))
            floored_minute = (timestamp.minute // minute_value) * minute_value
            return pd.Timestamp(
                timestamp.replace(minute=floored_minute, second=0, microsecond=0)
            )
        if normalized == "day":
            return pd.Timestamp(timestamp.replace(hour=0, minute=0, second=0, microsecond=0))
        raise ValueError(f"Unsupported interval for local candle store: {interval}")
