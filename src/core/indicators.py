# src/core/indicators.py
"""
Technical indicator calculations.
All functions are pure: they take a DataFrame and return a float.
No side effects, no global state.

DataFrame expected columns: time (UTC datetime64), open, high, low, close, tick_volume
"""
from __future__ import annotations

import pandas as pd
import MetaTrader5 as mt5


# ─────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────

def fetch_m1_rates(symbol: str, bars: int = 300) -> pd.DataFrame:
    """
    Fetch M1 OHLCV candles from MT5.
    bars: how many M1 candles to fetch (300 = 5 hours, enough for VWAP + ATR).
    """
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, bars)
    if rates is None:
        raise RuntimeError(f"MT5 returned no rates for {symbol} — is the symbol selected?")
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df


# ─────────────────────────────────────────────
# ATR  (Average True Range)
# ─────────────────────────────────────────────

def compute_atr(df: pd.DataFrame, period: int) -> float:
    """
    Classic Wilder-style ATR using a simple rolling mean of True Range.
    Used for:
      - sizing the initial SL distance (entry ± ATR × atr_init_mult)
      - driving the chandelier / atr_trailing stop updates
    """
    high  = df["high"]
    low   = df["low"]
    close = df["close"]
    prev  = close.shift(1)

    tr = pd.concat(
        [high - low, (high - prev).abs(), (low - prev).abs()],
        axis=1,
    ).max(axis=1)

    atr_val = tr.rolling(period).mean().iloc[-1]
    if pd.isna(atr_val):
        # Not enough bars: fall back to simple average of last `period` ranges
        atr_val = (high - low).tail(period).mean()

    return float(atr_val)


# ─────────────────────────────────────────────
# RSI  (Relative Strength Index)
# ─────────────────────────────────────────────

def compute_rsi(df: pd.DataFrame, period: int = 14) -> float:
    """
    Standard RSI(period) on M1 close prices.

    Used in hybrid direction decision:
      RSI > rsi_overbought  →  momentum is bearish  (crowd sells may be correct)
      RSI < rsi_oversold    →  momentum is bullish   (crowd buys may be correct)
      RSI in middle zone    →  no strong momentum    (fade the crowd → inverse)

    Returns NaN-safe float; falls back to 50 if insufficient data.
    """
    close = df["close"]
    delta = close.diff()

    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()

    rs    = avg_gain / avg_loss.replace(0.0, float("nan"))
    rsi   = 100.0 - (100.0 / (1.0 + rs))
    value = float(rsi.iloc[-1])

    return 50.0 if pd.isna(value) else value


# ─────────────────────────────────────────────
# VWAP  (Volume-Weighted Average Price)
# ─────────────────────────────────────────────

def compute_vwap(df: pd.DataFrame) -> float:
    """
    Intraday VWAP anchored to today's UTC midnight.

    Why intraday?  VWAP only has meaningful "above / below" semantics within
    the same session — a multi-day rolling VWAP drifts too slowly to be useful
    for scalping decisions on M1.

    Falls back to the full passed DataFrame if fewer than 2 bars exist today
    (e.g. if bot starts just after midnight).

    Used in hybrid direction decision:
      price significantly ABOVE vwap  →  bullish bias
      price significantly BELOW vwap  →  bearish bias

    Combined with RSI:
      SELL cluster + RSI overbought + price above VWAP
        → momentum confirms selling pressure → go WITH cluster (sell)
      BUY cluster + RSI oversold + price below VWAP
        → momentum confirms buying pressure / bounce → go WITH cluster (buy)
      Otherwise → INVERSE (fade the crowd)
    """
    now_utc     = pd.Timestamp.now(tz="UTC")
    today_start = now_utc.normalize()   # UTC midnight

    today = df[df["time"] >= today_start].copy()
    if len(today) < 2:
        today = df.copy()   # not enough today-bars, use full window

    typical_price = (today["high"] + today["low"] + today["close"]) / 3.0
    volume        = today["tick_volume"].replace(0, 1)   # guard against 0-volume bars

    vwap_series = (typical_price * volume).cumsum() / volume.cumsum()
    value       = float(vwap_series.iloc[-1])

    return value if not pd.isna(value) else float(today["close"].iloc[-1])
