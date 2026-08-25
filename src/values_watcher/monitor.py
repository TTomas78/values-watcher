"""Pipeline en vivo: conecta collectors con detección (FVG, order book) y storage.

Los eventos detectados se pasan a un handler de eventos (alertas en Fase 3).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from values_watcher.core.fvg import Candle, Fvg, FvgStatus, FvgTracker
from values_watcher.alerts.chart import render_fvg_chart, render_fvg_mitigated
from values_watcher.alerts.telegram_photo import send_photo, send_photo_reply
from values_watcher.core.orderbook import BookSnapshot, Wall, Imbalance, compute_imbalance, detect_walls
from values_watcher.core.patterns import detect_patterns
from values_watcher.core.watchlist import (
    PriceLadderTracker,
    PriceTargetTracker,
    detect_stop_volumes,
    mid_price,
)
from values_watcher.storage.db import Database

log = logging.getLogger(__name__)

EventHandler = Callable[[str, dict], Awaitable[None]]  # (event_type, payload)


async def noop_event_handler(event_type: str, payload: dict) -> None:
    log.info("EVENTO %s: %s", event_type, payload)


class LiveMonitor:
    def __init__(
        self,
        symbols: list[str],
        timeframes: list[str],
        db: Database,
        wall_multiplier: float = 5.0,
        imbalance_threshold: float = 0.6,
        on_event: EventHandler = noop_event_handler,
        snapshot_every: int = 50,
        watch: dict[str, dict] | None = None,
        liq_min_alert_usd: float = 50_000,
        liq_critical_multiplier: float = 5.0,
        pattern_timeframes: list[str] | None = None,
        pattern_min_candles: int = 50,
        telegram_token: str = "",
        telegram_chat_id: str = "",
    ) -> None:
        self.db = db
        self.wall_multiplier = wall_multiplier
        self.imbalance_threshold = imbalance_threshold
        self.on_event = on_event
        self.snapshot_every = snapshot_every
        self.trackers = {
            (s, tf): FvgTracker(s, tf) for s in symbols for tf in timeframes
        }
        watch = watch or {}
        self.stop_volumes = {s: w.get("stop_volume") for s, w in watch.items()
                             if w.get("stop_volume")}
        self.price_tracker = PriceTargetTracker(
            {s: w.get("price_targets", []) for s, w in watch.items()})
        self.ladder_tracker = PriceLadderTracker(
            {s: w.get("price_ladders", []) for s, w in watch.items()})
        self.liq_min_alert_usd = liq_min_alert_usd
        self.liq_critical_multiplier = liq_critical_multiplier
        self.pattern_timeframes = set(pattern_timeframes or [])
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id
        self._fvg_message_ids: dict[tuple, int] = {}  # (symbol, tf, formed_at) -> message_id
        self.pattern_min_candles = pattern_min_candles
        self._pattern_buffers: dict[tuple[str, str], list[Candle]] = {}
        self._book_count = 0

    async def on_liquidation(self, liq) -> None:
        """Liquidación individual: siempre se persiste; evento si supera el umbral USD."""
        await self.db.insert_liquidation(liq)
        if liq.usd >= self.liq_min_alert_usd:
            await self.on_event("liquidation", {
                "symbol": liq.symbol, "side": liq.side, "price": liq.price,
                "quantity": liq.quantity, "usd": round(liq.usd, 2),
                "severity": "critical"
                    if liq.usd >= self.liq_min_alert_usd * self.liq_critical_multiplier
                    else "warning",
                "timestamp": liq.timestamp,
            })

    async def on_candle(self, symbol: str, tf: str, candle: Candle, volume: float) -> None:
        await self.db.insert_candle(symbol, tf, candle, volume)
        if tf in self.pattern_timeframes:
            buf = self._pattern_buffers.setdefault((symbol, tf), [])
            buf.append(candle)
            del buf[:-self.pattern_min_candles]
            for p in detect_patterns(symbol, tf, buf, self.pattern_min_candles):
                await self.on_event("pattern", {
                    "symbol": symbol, "timeframe": tf, "pattern": p["name"],
                    "direction": p["direction"], "close": candle.close,
                    "open_time": candle.open_time,
                })
        tracker = self.trackers.get((symbol, tf))
        if tracker is None:
            return
        for fvg in tracker.on_candle_closed(candle):
            if fvg.status == FvgStatus.MITIGATED:
                await self.db.mark_fvg_mitigated(fvg)
                # Foto de mitigación: reply al mensaje original
                key = (fvg.symbol, fvg.timeframe, fvg.formed_at)
                reply_id = self._fvg_message_ids.pop(key, None)
                if self.telegram_token and self.telegram_chat_id and reply_id:
                    try:
                        # Buscar las 3 velas originales + la que mitigó
                        candles = await self._get_fvg_candles(fvg)
                        if candles:
                            png = render_fvg_mitigated(fvg, candles[:3], candles[3])
                            direction_es = "ALCISTA" if fvg.direction.value == "bullish" else "BAJISTA"
                            caption = f"{fvg.symbol} {fvg.timeframe} — FVG {direction_es} MITIGADO {fvg.bottom:,.1f}–{fvg.top:,.1f}"
                            await send_photo_reply(
                                self.telegram_token, int(self.telegram_chat_id),
                                png, caption, reply_id
                            )
                    except Exception as e:
                        log.warning("Error enviando gráfico FVG mitigado: %s", e)
            else:
                await self.db.insert_fvg(fvg)
                # Foto del gap abierto + guardar message_id para reply futuro
                if self.telegram_token and self.telegram_chat_id:
                    try:
                        png = render_fvg_chart(fvg, list(tracker._window))
                        direction_es = "ALCISTA" if fvg.direction.value == "bullish" else "BAJISTA"
                        caption = f"{fvg.symbol} {fvg.timeframe} — FVG {direction_es} {fvg.bottom:,.1f}–{fvg.top:,.1f}"
                        msg_id = await send_photo(self.telegram_token, int(self.telegram_chat_id), png, caption)
                        if msg_id:
                            key = (fvg.symbol, fvg.timeframe, fvg.formed_at)
                            self._fvg_message_ids[key] = msg_id
                    except Exception as e:
                        log.warning("Error enviando gráfico FVG: %s", e)

    async def _get_fvg_candles(self, fvg: Fvg) -> list[Candle] | None:
        """Busca las 3 velas del FVG + la vela que lo mitigó."""
        # formed_at es open_time de la vela 3
        # mitigated_at es open_time de la vela que rellenó
        from values_watcher.storage.db import Candle as DBCandle
        rows = await self.db.fetch_all(
            "SELECT open_time, open, high, low, close FROM candles "
            "WHERE symbol = ? AND timeframe = ? AND open_time <= ? "
            "ORDER BY open_time DESC LIMIT 4",
            (fvg.symbol, fvg.timeframe, fvg.mitigated_at or fvg.formed_at)
        )
        if len(rows) < 4:
            return None
        # Invertir para tener orden cronológico
        rows = list(reversed(rows))
        return [
            Candle(open_time=r[0], open=r[1], high=r[2], low=r[3], close=r[4])
            for r in rows
        ]

    async def on_book(self, book: BookSnapshot) -> None:
        self._book_count += 1
        if self._book_count % self.snapshot_every == 0:
            await self.db.insert_snapshot(
                book.symbol, book.timestamp,
                json.dumps(book.bids), json.dumps(book.asks),
            )
        for wall in detect_walls(book, self.wall_multiplier):
            await self.on_event("wall", {
                "symbol": book.symbol, "side": wall.side, "price": wall.price,
                "quantity": wall.quantity, "multiple": round(wall.multiple, 2),
                "timestamp": book.timestamp,
            })
        imb: Imbalance | None = compute_imbalance(book)
        if imb and (imb.ratio >= self.imbalance_threshold
                    or imb.ratio <= 1 - self.imbalance_threshold):
            await self.on_event("imbalance", {
                "symbol": book.symbol, "ratio": round(imb.ratio, 3),
                "bid_total": round(imb.bid_total, 2), "ask_total": round(imb.ask_total, 2),
                "timestamp": book.timestamp,
            })
        min_vol = self.stop_volumes.get(book.symbol)
        if min_vol:
            for ev in detect_stop_volumes(book.bids, book.asks, min_vol):
                await self.on_event("stop_volume", {
                    "symbol": book.symbol, **ev, "timestamp": book.timestamp})
        price = mid_price(book.bids, book.asks)
        if price is not None:
            for ev in self.price_tracker.check(book.symbol, price):
                await self.on_event("price_target", {**ev, "timestamp": book.timestamp})
            for ev in self.ladder_tracker.check(book.symbol, price):
                await self.on_event("price_ladder", {**ev, "timestamp": book.timestamp})


def _fvg_payload(fvg: Fvg) -> dict:
    return {
        "symbol": fvg.symbol, "timeframe": fvg.timeframe,
        "direction": fvg.direction.value, "top": fvg.top, "bottom": fvg.bottom,
        "formed_at": fvg.formed_at, "status": fvg.status.value,
        "mitigated_at": fvg.mitigated_at,
    }
