"""Detección de Fair Value Gaps (FVG) sobre velas OHLC.

Un FVG es un patrón de 3 velas donde la vela del medio deja un "hueco":
- Bullish: el low de la vela 3 queda por encima del high de la vela 1.
- Bearish: el high de la vela 3 queda por debajo del low de la vela 1.
El gap es el rango [high1, low3] (bullish) o [high3, low1] (bearish).
Un gap queda "mitigado" cuando el precio vuelve a operar dentro de él.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Direction(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"


class FvgStatus(str, Enum):
    OPEN = "open"
    MITIGATED = "mitigated"


@dataclass(frozen=True)
class Candle:
    open_time: int  # ms epoch
    open: float
    high: float
    low: float
    close: float


@dataclass
class Fvg:
    symbol: str
    timeframe: str
    direction: Direction
    top: float
    bottom: float
    formed_at: int  # open_time de la vela 3 (la que confirma el patrón)
    status: FvgStatus = FvgStatus.OPEN
    mitigated_at: int | None = None

    def check_mitigation(self, candle: Candle) -> bool:
        """Marca el gap como mitigado si la vela opera dentro del rango."""
        if self.status == FvgStatus.MITIGATED:
            return False
        if candle.low <= self.top and candle.high >= self.bottom:
            self.status = FvgStatus.MITIGATED
            self.mitigated_at = candle.open_time
            return True
        return False


def detect_fvg(c1: Candle, c2: Candle, c3: Candle, symbol: str, timeframe: str) -> Fvg | None:
    """Detecta un FVG en tres velas consecutivas. Devuelve None si no hay gap."""
    if c3.low > c1.high:
        return Fvg(
            symbol=symbol,
            timeframe=timeframe,
            direction=Direction.BULLISH,
            top=c3.low,
            bottom=c1.high,
            formed_at=c3.open_time,
        )
    if c3.high < c1.low:
        return Fvg(
            symbol=symbol,
            timeframe=timeframe,
            direction=Direction.BEARISH,
            top=c1.low,
            bottom=c3.high,
            formed_at=c3.open_time,
        )
    return None


class FvgTracker:
    """Mantiene los FVG abiertos de un símbolo/timeframe y actualiza mitigaciones."""

    def __init__(self, symbol: str, timeframe: str, max_open: int = 50) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.max_open = max_open
        self._window: list[Candle] = []
        self.open_fvgs: list[Fvg] = []

    def on_candle_closed(self, candle: Candle) -> list[Fvg]:
        """Procesa una vela cerrada. Devuelve eventos: FVGs nuevos o recién mitigados."""
        events: list[Fvg] = []
        for fvg in self.open_fvgs:
            if fvg.check_mitigation(candle):
                events.append(fvg)
        self.open_fvgs = [f for f in self.open_fvgs if f.status == FvgStatus.OPEN]

        self._window.append(candle)
        if len(self._window) > 3:
            self._window.pop(0)
        if len(self._window) == 3:
            fvg = detect_fvg(*self._window, self.symbol, self.timeframe)
            if fvg is not None:
                self.open_fvgs.append(fvg)
                if len(self.open_fvgs) > self.max_open:
                    self.open_fvgs.pop(0)
                events.append(fvg)
        return events
