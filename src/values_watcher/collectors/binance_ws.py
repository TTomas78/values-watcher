"""Collector de Binance Futures.

- Order book: WebSocket público depth20@100ms (fstream), reconexión con backoff.
- Velas cerradas: polling REST a fapi /fapi/v1/klines cada `poll_seconds`.
  El stream WS de klines de futuros no entrega datos actualmente; el polling
  REST es exacto para velas cerradas y suficiente para timeframes de 5m/15m.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
import websockets

from values_watcher.core.fvg import Candle
from values_watcher.core.orderbook import BookSnapshot

log = logging.getLogger(__name__)

WS_BASE = "wss://fstream.binance.com/stream"
REST_BASE = "https://fapi.binance.com"

CandleHandler = Callable[[str, str, Candle, float], Awaitable[None]]
BookHandler = Callable[[BookSnapshot], Awaitable[None]]
LiquidationHandler = Callable[["Liquidation"], Awaitable[None]]


def stream_names(symbols: list[str]) -> list[str]:
    names = []
    for sym in symbols:
        s = sym.lower()
        names.append(f"{s}@depth20@100ms")
        names.append(f"{s}@forceOrder")
    return names


def parse_depth(symbol: str, payload: dict) -> BookSnapshot:
    bids = [(float(p), float(q)) for p, q in payload.get("b", [])]
    asks = [(float(p), float(q)) for p, q in payload.get("a", [])]
    ts = int(payload.get("E") or time.time() * 1000)
    return BookSnapshot(symbol=symbol, bids=bids, asks=asks, timestamp=ts)


@dataclass(frozen=True)
class Liquidation:
    symbol: str
    side: str        # "long" | "short" (la posición liquidada)
    price: float     # precio promedio de la orden de liquidación
    quantity: float
    usd: float
    timestamp: int


def parse_force_order(payload: dict) -> Liquidation | None:
    """Parsea un evento forceOrder. side SELL → long liquidado, BUY → short."""
    o = payload.get("o", {})
    if not o:
        return None
    price = float(o.get("ap") or o.get("p") or 0)
    qty = float(o.get("z") or o.get("q") or 0)
    if price <= 0 or qty <= 0:
        return None
    return Liquidation(
        symbol=o.get("s", "").upper(),
        side="long" if o.get("S") == "SELL" else "short",
        price=price,
        quantity=qty,
        usd=price * qty,
        timestamp=int(o.get("T") or payload.get("E") or time.time() * 1000),
    )


def parse_rest_kline(raw: list) -> tuple[Candle, float]:
    """Parsea una kline REST [openTime, o, h, l, c, v, closeTime, ...]."""
    candle = Candle(
        open_time=int(raw[0]),
        open=float(raw[1]),
        high=float(raw[2]),
        low=float(raw[3]),
        close=float(raw[4]),
    )
    return candle, float(raw[5])


class KlinePoller:
    """Pide klines REST por (símbolo, timeframe) y emite solo velas cerradas nuevas."""

    def __init__(
        self,
        symbols: list[str],
        timeframes: list[str],
        on_candle: CandleHandler,
        poll_seconds: int = 30,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.symbols = symbols
        self.timeframes = timeframes
        self.on_candle = on_candle
        self.poll_seconds = poll_seconds
        self._http = http
        self._stop = asyncio.Event()
        self._seen: set[tuple[str, str, int]] = set()

    def stop(self) -> None:
        self._stop.set()

    async def fetch_closed(self, client: httpx.AsyncClient, symbol: str, tf: str) -> list[tuple[Candle, float]]:
        r = await client.get("/fapi/v1/klines",
                             params={"symbol": symbol, "interval": tf, "limit": 5})
        r.raise_for_status()
        now_ms = int(time.time() * 1000)
        closed = []
        for raw in r.json():
            if int(raw[6]) < now_ms:  # closeTime < ahora → vela cerrada
                closed.append(parse_rest_kline(raw))
        return closed

    async def run(self) -> None:
        owns = self._http is None
        client = self._http or httpx.AsyncClient(base_url=REST_BASE, timeout=15.0)
        # warmup: poblar _seen sin emitir para no reprocesar histórico como "nuevo"
        for symbol in self.symbols:
            for tf in self.timeframes:
                try:
                    for candle, _ in await self.fetch_closed(client, symbol, tf):
                        self._seen.add((symbol, tf, candle.open_time))
                except httpx.HTTPError as e:
                    log.warning("Warmup klines %s %s falló: %s", symbol, tf, e)
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                    break
                except asyncio.TimeoutError:
                    pass
                for symbol in self.symbols:
                    for tf in self.timeframes:
                        try:
                            for candle, volume in await self.fetch_closed(client, symbol, tf):
                                key = (symbol, tf, candle.open_time)
                                if key not in self._seen:
                                    self._seen.add(key)
                                    await self.on_candle(symbol, tf, candle, volume)
                        except httpx.HTTPError as e:
                            log.warning("Poll klines %s %s falló: %s", symbol, tf, e)
        finally:
            if owns:
                await client.aclose()


class BinanceCollector:
    """Order book por WS + velas por polling REST."""

    def __init__(
        self,
        symbols: list[str],
        timeframes: list[str],
        on_candle: CandleHandler,
        on_book: BookHandler,
        on_liquidation: "LiquidationHandler | None" = None,
        kline_poll_seconds: int = 30,
    ) -> None:
        self.symbols = symbols
        self.on_book = on_book
        self.on_liquidation = on_liquidation
        self.poller = KlinePoller(symbols, timeframes, on_candle, kline_poll_seconds)
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()
        self.poller.stop()

    async def run(self) -> None:
        await asyncio.gather(self._run_depth(), self.poller.run())

    async def _run_depth(self) -> None:
        streams = "/".join(stream_names(self.symbols))
        url = f"{WS_BASE}?streams={streams}"
        backoff = 1
        while not self._stop.is_set():
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    log.info("Conectado a Binance WS (%d streams depth)", len(streams.split('/')))
                    backoff = 1
                    await self._listen(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._stop.is_set():
                    break
                log.warning("WS caído (%s); reconectando en %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _listen(self, ws) -> None:
        async for raw in ws:
            if self._stop.is_set():
                return
            msg = json.loads(raw)
            stream = msg.get("stream", "")
            data = msg.get("data", {})
            if "@depth" in stream:
                symbol = stream.split("@")[0].upper()
                await self.on_book(parse_depth(symbol, data))
            elif "@forceOrder" in stream and self.on_liquidation:
                liq = parse_force_order(data)
                if liq:
                    await self.on_liquidation(liq)
