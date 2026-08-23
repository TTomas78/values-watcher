"""Detección de patrones de velas clásicos (TA-Lib) sobre velas cerradas."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import talib

from values_watcher.core.fvg import Candle

# Subset clásico de reversión: nombre -> función TA-Lib
PATTERNS: dict[str, Callable] = {
    "engulfing": talib.CDLENGULFING,
    "hammer": talib.CDLHAMMER,
    "hanging_man": talib.CDLHANGINGMAN,
    "morning_star": talib.CDLMORNINGSTAR,
    "evening_star": talib.CDLEVENINGSTAR,
    "doji": talib.CDLDOJI,
    "three_white_soldiers": talib.CDL3WHITESOLDIERS,
    "three_black_crows": talib.CDL3BLACKCROWS,
}


def detect_patterns(symbol: str, timeframe: str, candles: list[Candle],
                    min_candles: int = 50) -> list[dict]:
    """Evalúa la última vela del array. [] si no hay historial suficiente."""
    if len(candles) < min_candles:
        return []
    o = np.array([c.open for c in candles], dtype=float)
    h = np.array([c.high for c in candles], dtype=float)
    lo = np.array([c.low for c in candles], dtype=float)
    cl = np.array([c.close for c in candles], dtype=float)
    found = []
    for name, fn in PATTERNS.items():
        score = int(fn(o, h, lo, cl)[-1])
        if score == 0:
            continue
        direction = ("neutral" if name == "doji"
                     else "bullish" if score > 0 else "bearish")
        found.append({"name": name, "direction": direction, "score": score})
    return found
