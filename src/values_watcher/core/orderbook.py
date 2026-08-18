"""Análisis del libro de órdenes: paredes de liquidez e imbalance bid/ask."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median


@dataclass(frozen=True)
class BookSnapshot:
    symbol: str
    bids: list[tuple[float, float]]  # (precio, cantidad)
    asks: list[tuple[float, float]]
    timestamp: int  # ms epoch


@dataclass(frozen=True)
class Wall:
    side: str  # "bid" | "ask"
    price: float
    quantity: float
    multiple: float  # cuántas veces la mediana


@dataclass(frozen=True)
class Imbalance:
    ratio: float  # bids / (bids + asks), 0..1
    bid_total: float
    ask_total: float


def detect_walls(book: BookSnapshot, multiplier: float = 5.0) -> list[Wall]:
    """Niveles cuya cantidad supera `multiplier` × la mediana de su lado."""
    walls: list[Wall] = []
    for side, levels in (("bid", book.bids), ("ask", book.asks)):
        quantities = [q for _, q in levels if q > 0]
        if len(quantities) < 3:
            continue
        med = median(quantities)
        if med <= 0:
            continue
        for price, qty in levels:
            if qty >= multiplier * med:
                walls.append(Wall(side=side, price=price, quantity=qty, multiple=qty / med))
    return walls


def compute_imbalance(book: BookSnapshot) -> Imbalance | None:
    """Ratio bid/(bid+ask) del volumen total visible. None si el libro está vacío."""
    bid_total = sum(q for _, q in book.bids)
    ask_total = sum(q for _, q in book.asks)
    total = bid_total + ask_total
    if total <= 0:
        return None
    return Imbalance(ratio=bid_total / total, bid_total=bid_total, ask_total=ask_total)


def is_extreme(imbalance: Imbalance, threshold: float = 0.6) -> bool:
    """True si el imbalance supera el umbral en cualquier dirección."""
    return imbalance.ratio >= threshold or imbalance.ratio <= 1 - threshold
