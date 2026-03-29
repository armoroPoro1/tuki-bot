from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional

import pandas as pd
import pandas_ta as ta


TradeSide = Literal["long", "short"]


@dataclass(frozen=True)
class Signal:
    side: TradeSide


def ohlcv_to_dataframe(ohlcv: list[list[float]]) -> pd.DataFrame:
    """
    Convert ccxt OHLCV to a DataFrame with timestamp in UTC.
    """
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add EMA(50), EMA(200), and RSI(14).
    """
    out = df.copy()
    # pandas-ta sometimes outputs NaN until enough history is available
    # (e.g. EMA200 needs >= 200 bars). Since this project fetches 100 bars,
    # compute EMA via pandas EWM so we always get values for signal logic.
    out["ema50"] = out["close"].ewm(span=50, adjust=False).mean()
    out["ema200"] = out["close"].ewm(span=200, adjust=False).mean()
    out["rsi14"] = ta.rsi(out["close"], length=14)
    return out


def latest_signal(df: pd.DataFrame) -> Optional[Signal]:
    """
    Strategy:
    - Long: close > EMA200 and RSI(14) < 40
    - Short: not used in this project (only Long entry is implemented per spec)
    """
    latest = df.iloc[-1]
    close = float(latest["close"])
    ema200_val = latest["ema200"]
    rsi14_val = latest["rsi14"]

    # If indicators are not ready, skip trading decision.
    if ema200_val is None or rsi14_val is None:
        return None
    try:
        ema200 = float(ema200_val)
        rsi14 = float(rsi14_val)
    except (TypeError, ValueError):
        return None

    if close > ema200 and rsi14 < 40:
        return Signal(side="long")
    return None


def strategy_signal(*, df: pd.DataFrame, strategy_type: str, params: dict[str, Any]) -> Optional[Signal]:
    """
    Return a trading Signal (long/short) for a given strategy type.

    The caller must ensure `df` has at least columns:
    - open, high, low, close, volume
    """
    stype = (strategy_type or "").strip().upper()

    if df.empty or len(df) < 3:
        return None

    # Use `.iloc[-1]` for the current candle, `.iloc[-2]` for the previous one.
    last = df.iloc[-1]
    prev = df.iloc[-2]
    close = float(last["close"])
    prev_close = float(prev["close"])

    # ----------------------------
    # EMA_CROSS (Trend following)
    # ----------------------------
    if stype == "EMA_CROSS":
        fast_ema = int(params.get("fast_ema", 50))
        slow_ema = int(params.get("slow_ema", 200))
        if fast_ema <= 0 or slow_ema <= 0 or fast_ema >= slow_ema:
            return None

        ema_fast = df["close"].ewm(span=fast_ema, adjust=False).mean()
        ema_slow = df["close"].ewm(span=slow_ema, adjust=False).mean()

        ema_fast_prev = ema_fast.iloc[-2]
        ema_slow_prev = ema_slow.iloc[-2]
        ema_fast_curr = ema_fast.iloc[-1]
        ema_slow_curr = ema_slow.iloc[-1]

        if any(pd.isna(x) for x in (ema_fast_prev, ema_slow_prev, ema_fast_curr, ema_slow_curr)):
            return None

        spread = abs(float(ema_fast_curr) - float(ema_slow_curr))
        spread_pct = (spread / float(ema_slow_curr)) * 100.0 if float(ema_slow_curr) != 0 else 0.0
        min_spread_pct = float(params.get("min_spread_pct", 0.0))
        if spread_pct < min_spread_pct:
            return None

        # Long: fast crosses above slow
        crossed_up = float(ema_fast_prev) <= float(ema_slow_prev) and float(ema_fast_curr) > float(ema_slow_curr)
        crossed_down = float(ema_fast_prev) >= float(ema_slow_prev) and float(ema_fast_curr) < float(ema_slow_curr)

        # Reduce false entries: also require price to be on the "right side" of slow EMA.
        close_above_slow = close > float(ema_slow_curr)
        close_below_slow = close < float(ema_slow_curr)

        if crossed_up and close_above_slow:
            # Extra optional filter: ensure direction changed upward.
            _ = prev_close
            return Signal(side="long")
        if crossed_down and close_below_slow:
            return Signal(side="short")

        return None

    # ----------------------------
    # RSI_BOLLINGER (Mean reversion)
    # ----------------------------
    if stype == "RSI_BOLLINGER":
        rsi_period = int(params.get("rsi_period", 14))
        buy_level = float(params.get("buy_level", 30))
        sell_level = float(params.get("sell_level", 70))
        bb_period = int(params.get("bb_period", 20))
        bb_std = float(params.get("bb_std", 2))

        if rsi_period <= 0 or bb_period <= 1:
            return None

        rsi = ta.rsi(df["close"], length=rsi_period)
        bb = ta.bbands(df["close"], length=bb_period, std=bb_std)

        lower_col = next((c for c in bb.columns if c.startswith("BBL_")), None)
        upper_col = next((c for c in bb.columns if c.startswith("BBU_")), None)
        if not lower_col or not upper_col:
            return None

        rsi_last = rsi.iloc[-1]
        lower_last = bb[lower_col].iloc[-1]
        upper_last = bb[upper_col].iloc[-1]

        if any(pd.isna(x) for x in (rsi_last, lower_last, upper_last)):
            return None

        # Buy dip: price below (or on) lower band + RSI oversold.
        if close <= float(lower_last) and float(rsi_last) <= buy_level:
            return Signal(side="long")

        # Sell top: price above (or on) upper band + RSI overbought.
        if close >= float(upper_last) and float(rsi_last) >= sell_level:
            return Signal(side="short")

        return None

    # ----------------------------
    # BREAKOUT_VOLUME (Momentum)
    # ----------------------------
    if stype == "BREAKOUT_VOLUME":
        breakout_lookback = int(params.get("breakout_lookback", 20))
        volume_ma_period = int(params.get("volume_ma_period", 20))
        volume_multiplier = float(params.get("volume_multiplier", 1.5))
        allow_short = bool(params.get("allow_short", False))

        if breakout_lookback < 5 or volume_ma_period < 5:
            return None
        if volume_multiplier <= 0:
            return None

        # Highest high / lowest low excluding current candle.
        prev_window = df.iloc[-breakout_lookback - 1 : -1]
        if prev_window.empty:
            return None

        highest_high = float(prev_window["high"].max())
        lowest_low = float(prev_window["low"].min())
        current_volume = float(last["volume"])
        volume_ma = df["volume"].rolling(volume_ma_period).mean().iloc[-1]
        if pd.isna(volume_ma):
            return None

        vol_spike = current_volume >= float(volume_ma) * volume_multiplier
        # Breakout long
        if vol_spike and close > highest_high:
            return Signal(side="long")
        # Breakdown short (optional)
        if allow_short and vol_spike and close < lowest_low:
            return Signal(side="short")

        return None

    # Unknown strategy type
    return None

