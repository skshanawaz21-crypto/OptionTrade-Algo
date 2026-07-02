from __future__ import annotations

import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.rolling(period).mean()


def enrich_indicators(df: pd.DataFrame) -> pd.DataFrame:
    enriched = df.copy()
    enriched["ema20"] = ema(enriched["close"], 20)
    enriched["ema50"] = ema(enriched["close"], 50)
    enriched["rsi14"] = rsi(enriched["close"], 14)
    enriched["atr14"] = atr(enriched, 14)
    enriched["momentum"] = enriched["close"].pct_change(5)
    enriched["prev_high"] = enriched["high"].shift(1)
    enriched["prev_low"] = enriched["low"].shift(1)
    return enriched.dropna().reset_index(drop=True)
