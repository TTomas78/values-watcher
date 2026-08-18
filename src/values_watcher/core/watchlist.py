"""Watchlist por activo: objetivos de precio y volúmenes de parada absolutos.

- price_target: alerta cuando el mid price cruza un nivel configurado.
- stop_volume: alerta cuando un nivel del libro vivo supera un volumen absoluto.
"""

from __future__ import annotations


def mid_price(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> float | None:
    if not bids or not asks:
        return None
    return (bids[0][0] + asks[0][0]) / 2


def detect_stop_volumes(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    min_volume: float,
) -> list[dict]:
    """Niveles del libro con volumen >= umbral absoluto."""
    events = []
    for side, levels in (("bid", bids), ("ask", asks)):
        for price, qty in levels:
            if qty >= min_volume:
                events.append({"side": side, "price": price, "volume": round(qty, 3),
                               "threshold": min_volume})
    return events


class PriceTargetTracker:
    """Detecta cruces de niveles de precio configurados por símbolo."""

    def __init__(self, targets: dict[str, list[float]]) -> None:
        # posición actual del precio respecto de cada nivel: "above" | "below" | None
        self._state: dict[tuple[str, float], str] = {}
        self.targets = {s: sorted(set(ts)) for s, ts in targets.items()}

    def check(self, symbol: str, price: float) -> list[dict]:
        """Devuelve eventos por cada nivel cruzado desde el último chequeo."""
        events = []
        for target in self.targets.get(symbol, []):
            key = (symbol, target)
            side = "above" if price >= target else "below"
            prev = self._state.get(key)
            if prev is not None and prev != side:
                events.append({
                    "symbol": symbol,
                    "target": target,
                    "price": round(price, 2),
                    "crossed": "up" if side == "above" else "down",
                })
            self._state[key] = side
        return events


class PriceLadderTracker:
    """Escalera de precios: nivel inicial + pasos porcentuales, una alerta por escalón.

    Ejemplo (direction="below", level=60000, step_pct=1):
    - El precio cruza 60000 hacia abajo → alerta (escalón 0, umbral 60000)
    - Cada 1% adicional abajo (59400, 58806, ...) → una alerta por escalón
    - Mientras siga debajo, NO repite alertas de escalones ya notificados
    - Si el precio vuelve arriba del nivel, la escalera se rearma (vuelve a
      poder alertar el escalón 0 en el próximo cruce)
    """

    def __init__(self, ladders: dict[str, list[dict]]) -> None:
        # ladders: {"BTCUSDT": [{"level": 60000, "step_pct": 1.0, "direction": "below"}]}
        self.ladders = ladders
        # estado por (symbol, índice de ladder): próximo umbral a notificar
        self._next: dict[tuple[str, int], float] = {}
        self._step: dict[tuple[str, int], int] = {}

    def check(self, symbol: str, price: float) -> list[dict]:
        events = []
        for i, ladder in enumerate(self.ladders.get(symbol, [])):
            level = float(ladder["level"])
            step = float(ladder.get("step_pct", 1.0)) / 100
            direction = ladder.get("direction", "below")
            key = (symbol, i)
            next_threshold = self._next.get(key, level)

            if direction == "below":
                if price >= level:
                    # rearma la escalera cuando el precio vuelve arriba del nivel
                    if key in self._next:
                        del self._next[key]
                        self._step[key] = 0
                    continue
                n = self._step.get(key, 0)
                while price < next_threshold:
                    events.append({
                        "symbol": symbol, "level": level, "step": n,
                        "threshold": round(next_threshold, 2),
                        "price": round(price, 2), "direction": "below",
                    })
                    n += 1
                    next_threshold = next_threshold * (1 - step)
                self._next[key] = next_threshold
                self._step[key] = n
            else:  # above
                if price <= level:
                    if key in self._next:
                        del self._next[key]
                        self._step[key] = 0
                    continue
                n = self._step.get(key, 0)
                while price > next_threshold:
                    events.append({
                        "symbol": symbol, "level": level, "step": n,
                        "threshold": round(next_threshold, 2),
                        "price": round(price, 2), "direction": "above",
                    })
                    n += 1
                    next_threshold = next_threshold * (1 + step)
                self._next[key] = next_threshold
                self._step[key] = n
        return events
