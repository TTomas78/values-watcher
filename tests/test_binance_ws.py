import time

import httpx
import pytest

from values_watcher.collectors.binance_ws import (
    KlinePoller,
    parse_depth,
    parse_rest_kline,
    stream_names,
)


def test_stream_names():
    names = stream_names(["BTCUSDT", "ETHUSDT"])
    assert names == ["btcusdt@depth20@100ms", "btcusdt@forceOrder",
                     "ethusdt@depth20@100ms", "ethusdt@forceOrder"]


def test_parse_depth():
    payload = {"e": "depthUpdate", "E": 1700000000123,
               "b": [["42000.0", "1.5"], ["41999.0", "2.0"]],
               "a": [["42001.0", "1.0"]]}
    book = parse_depth("BTCUSDT", payload)
    assert book.symbol == "BTCUSDT"
    assert book.bids[0] == (42000.0, 1.5)
    assert book.asks == [(42001.0, 1.0)]
    assert book.timestamp == 1700000000123


def test_parse_rest_kline():
    raw = [1700000000000, "42000.1", "42100.0", "41900.0", "42050.5",
           "123.456", 1700000299999, "x", 1, "0", "0", "0"]
    candle, volume = parse_rest_kline(raw)
    assert candle.open_time == 1700000000000
    assert candle.close == 42050.5
    assert volume == 123.456


def klines_http(now_ms: int):
    """HTTP mockeado: 3 velas, la última aún abierta."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/klines"
        t0 = now_ms - 3 * 300_000
        rows = [
            [t0, "1", "2", "0.5", "1.5", "10", t0 + 299_999],
            [t0 + 300_000, "1.5", "2", "1", "1.8", "11", t0 + 599_999],
            [t0 + 600_000, "1.8", "2", "1.5", "1.9", "12", now_ms + 300_000],  # abierta
        ]
        return httpx.Response(200, json=rows)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             base_url="https://fapi.binance.com")


async def test_poller_emits_only_closed_new():
    now_ms = int(time.time() * 1000)
    emitted = []

    async def on_candle(symbol, tf, candle, volume):
        emitted.append((symbol, tf, candle.open_time))

    poller = KlinePoller(["BTCUSDT"], ["5m"], on_candle, poll_seconds=999,
                         http=klines_http(now_ms))
    client = poller._http
    # warmup manual: marca las cerradas como vistas
    for candle, _ in await poller.fetch_closed(client, "BTCUSDT", "5m"):
        poller._seen.add(("BTCUSDT", "5m", candle.open_time))
    closed = await poller.fetch_closed(client, "BTCUSDT", "5m")
    assert len(closed) == 2  # la abierta se descarta
    # segunda pasada: nada nuevo
    for candle, volume in closed:
        key = ("BTCUSDT", "5m", candle.open_time)
        if key not in poller._seen:
            await on_candle("BTCUSDT", "5m", candle, volume)
    assert emitted == []


def test_stream_names_includes_force_order():
    names = stream_names(["BTCUSDT"])
    assert "btcusdt@forceOrder" in names
    assert "btcusdt@depth20@100ms" in names


FORCE_ORDER = {
    "e": "forceOrder", "E": 1700000000000,
    "o": {"s": "BTCUSDT", "S": "SELL", "o": "LIMIT", "f": "IOC", "q": "0.5",
          "p": "63000.0", "ap": "63010.0", "X": "FILLED", "l": "0.5", "z": "0.5",
          "T": 1700000000001},
}


def test_parse_force_order_long_liquidated():
    from values_watcher.collectors.binance_ws import parse_force_order
    liq = parse_force_order(FORCE_ORDER)
    assert liq is not None
    assert liq.symbol == "BTCUSDT"
    assert liq.side == "long"  # SELL = long liquidado
    assert liq.price == 63010.0
    assert liq.usd == 63010.0 * 0.5


def test_parse_force_order_short_side():
    from values_watcher.collectors.binance_ws import parse_force_order
    payload = {**FORCE_ORDER, "o": {**FORCE_ORDER["o"], "S": "BUY"}}
    assert parse_force_order(payload).side == "short"


def test_parse_force_order_invalid():
    from values_watcher.collectors.binance_ws import parse_force_order
    assert parse_force_order({}) is None
    bad = {**FORCE_ORDER, "o": {**FORCE_ORDER["o"], "ap": "0", "p": "0"}}
    assert parse_force_order(bad) is None
