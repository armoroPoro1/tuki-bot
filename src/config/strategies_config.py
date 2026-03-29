"""
Trading configuration.

Edit `STRATEGIES_CONFIG` to control which strategies run on which symbols.

Notes:
- Keys can be written as `BTCUSDT` or `BTC/USDT`. The bot will normalize them.
- `allocation` is used to scale `RISK_PER_TRADE_PCT` per strategy.
- This project currently opens at most 1 position per symbol at a time.
"""

from __future__ import annotations

from typing import Any


# Example config based on the groups you described.
# If you want multiple strategies per symbol, add more dicts to the list.
#
# allocation is split equally across all coins here (1/9 each).
_EQUAL_ALLOC = 1.0 / 9.0

STRATEGIES_CONFIG: dict[str, list[dict[str, Any]]] = {
    # BTC / ETH / BNB: EMA Cross (Trend) - less frequent, follow trend
    "BTCUSDT": [
        {
            "strategy_id": "BTC_TREND_EMA_CROSS",
            "type": "EMA_CROSS",
            "params": {"fast_ema": 50, "slow_ema": 200},
            "allocation": _EQUAL_ALLOC,
        }
    ],
    "ETHUSDT": [
        {
            "strategy_id": "ETH_TREND_EMA_CROSS",
            "type": "EMA_CROSS",
            "params": {"fast_ema": 50, "slow_ema": 200},
            "allocation": _EQUAL_ALLOC,
        }
    ],
    "BNBUSDT": [
        {
            "strategy_id": "BNB_TREND_EMA_CROSS",
            "type": "EMA_CROSS",
            "params": {"fast_ema": 50, "slow_ema": 200},
            "allocation": _EQUAL_ALLOC,
        }
    ],

    # SOL / SUI / AVAX: RSI + Bollinger Bands - buy dip / sell top
    "SOLUSDT": [
        {
            "strategy_id": "SOL_RSI_BB_DIP_TOP",
            "type": "RSI_BOLLINGER",
            "params": {
                "rsi_period": 14,
                "buy_level": 30,
                "sell_level": 70,
                "bb_period": 20,
                "bb_std": 2,
            },
            "allocation": _EQUAL_ALLOC,
        }
    ],
    "SUIUSDT": [
        {
            "strategy_id": "SUI_RSI_BB_DIP_TOP",
            "type": "RSI_BOLLINGER",
            "params": {
                "rsi_period": 14,
                "buy_level": 30,
                "sell_level": 70,
                "bb_period": 20,
                "bb_std": 2,
            },
            "allocation": _EQUAL_ALLOC,
        }
    ],
    "AVAXUSDT": [
        {
            "strategy_id": "AVAX_RSI_BB_DIP_TOP",
            "type": "RSI_BOLLINGER",
            "params": {
                "rsi_period": 14,
                "buy_level": 30,
                "sell_level": 70,
                "bb_period": 20,
                "bb_std": 2,
            },
            "allocation": _EQUAL_ALLOC,
        }
    ],

    # TAO / RENDER / DOGE: Breakout + Volume - follow momentum
    "TAOUSDT": [
        {
            "strategy_id": "TAO_BREAKOUT_VOLUME",
            "type": "BREAKOUT_VOLUME",
            "params": {
                "breakout_lookback": 20,
                "volume_ma_period": 20,
                "volume_multiplier": 1.5,
                "allow_short": True,
            },
            "allocation": _EQUAL_ALLOC,
        }
    ],
    "RENDERUSDT": [
        {
            "strategy_id": "RENDER_BREAKOUT_VOLUME",
            "type": "BREAKOUT_VOLUME",
            "params": {
                "breakout_lookback": 20,
                "volume_ma_period": 20,
                "volume_multiplier": 1.5,
                "allow_short": True,
            },
            "allocation": _EQUAL_ALLOC,
        }
    ],
    "DOGEUSDT": [
        {
            "strategy_id": "DOGE_BREAKOUT_VOLUME",
            "type": "BREAKOUT_VOLUME",
            "params": {
                "breakout_lookback": 20,
                "volume_ma_period": 20,
                "volume_multiplier": 1.5,
                "allow_short": True,
            },
            "allocation": _EQUAL_ALLOC,
        }
    ],
}

