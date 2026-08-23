import asyncio

import httpx
import pytest
import respx

from values_watcher.alerts.client import AlertClient, build_payload
from values_watcher.alerts.rules import AlertRules, dedup_key
from values_watcher.storage.db import Database


@pytest.fixture
async def db(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    yield db
    await db.close()


@respx.mock
async def test_client_posts_with_auth_header():
    route = respx.post("https://api.example.com/notify").mock(
        return_value=httpx.Response(200))
    client = AlertClient("https://api.example.com/notify", "secret-key", max_retries=1)
    ok = await client._post_with_retry({"event": "test"})
    assert ok
    assert route.called
    assert route.calls[0].request.headers["Authorization"] == "Bearer secret-key"


@respx.mock
async def test_client_custom_auth_header():
    route = respx.post("https://api.example.com/notify").mock(
        return_value=httpx.Response(200))
    client = AlertClient("https://api.example.com/notify", "k",
                         auth_header="X-API-Key: {key}", max_retries=1)
    await client._post_with_retry({"event": "x"})
    assert route.calls[0].request.headers["X-API-Key"] == "k"


@respx.mock
async def test_client_retries_then_fails():
    respx.post("https://api.example.com/notify").mock(
        return_value=httpx.Response(500))
    client = AlertClient("https://api.example.com/notify", "k", max_retries=2)
    # parcheamos sleep para no esperar backoff real
    orig_sleep = asyncio.sleep
    import values_watcher.alerts.client as mod
    mod.asyncio.sleep = lambda s: orig_sleep(0)
    try:
        ok = await client._post_with_retry({"event": "x"})
    finally:
        mod.asyncio.sleep = orig_sleep
    assert not ok


async def test_queue_full_discards():
    client = AlertClient("https://api.example.com/notify", "k", max_queue=1)
    assert client.enqueue({"event": "a"})
    assert not client.enqueue({"event": "b"})  # cola llena


def test_client_disabled_without_url():
    assert not AlertClient("", "k").enabled
    assert not AlertClient("https://x.com", "").enabled


def test_build_payload_shape():
    p = build_payload("fvg_new", {"symbol": "BTCUSDT", "top": 102, "bottom": 101})
    assert p["title"] == "[fvg_new] BTCUSDT"
    assert p["service"] == "values-watcher"
    assert p["severity"] == "info"
    assert '"top": 102' in p["detail"]
    assert build_payload("wall", {"symbol": "ETHUSDT"})["severity"] == "warning"


def test_dedup_keys():
    fvg = {"symbol": "BTCUSDT", "timeframe": "5m", "formed_at": 123}
    assert dedup_key("fvg_new", fvg) == "fvg_new:BTCUSDT:5m:123"
    wall = {"symbol": "ETHUSDT", "side": "bid", "price": 3000.0}
    assert dedup_key("wall", wall) == "wall:ETHUSDT:bid"
    assert dedup_key("imbalance", {"symbol": "SOLUSDT", "ratio": 0.7}).endswith(":bid")
    assert dedup_key("imbalance", {"symbol": "SOLUSDT", "ratio": 0.3}).endswith(":ask")
    lo = {"symbol": "BTCUSDT", "side": "bid", "price": 63850.0}
    assert dedup_key("large_order", lo) == "large_order:BTCUSDT:bid"


@respx.mock
async def test_rules_dedup(db):
    respx.post("https://api.example.com/notify").mock(return_value=httpx.Response(200))
    client = AlertClient("https://api.example.com/notify", "k")
    rules = AlertRules(client, db, ["fvg_new"], dedup_minutes=30)
    payload = {"symbol": "BTCUSDT", "timeframe": "5m", "formed_at": 123}

    assert await rules.handle("fvg_new", payload)
    assert not await rules.handle("fvg_new", payload)  # duplicado
    assert not await rules.handle("wall", {"symbol": "BTCUSDT"})  # no habilitado


@respx.mock
async def test_rules_dedup_wall_price_ticks(db):
    """Un muro que se mueve centavos no debe re-alertar dentro de la ventana."""
    respx.post("https://api.example.com/notify").mock(return_value=httpx.Response(200))
    client = AlertClient("https://api.example.com/notify", "k")
    rules = AlertRules(client, db, ["wall"], dedup_minutes=30)

    assert await rules.handle("wall", {"symbol": "BTCUSDT", "side": "ask",
                                       "price": 63932.4, "quantity": 10, "multiple": 5.0})
    assert not await rules.handle("wall", {"symbol": "BTCUSDT", "side": "ask",
                                           "price": 63932.5, "quantity": 11, "multiple": 5.1})
    # otro lado sí alerta
    assert await rules.handle("wall", {"symbol": "BTCUSDT", "side": "bid",
                                       "price": 63930.1, "quantity": 8, "multiple": 4.2})


@respx.mock
async def test_rules_dedup_large_order_price_ticks(db):
    """Órdenes grandes en el mismo lado no re-alertan por ticks de precio."""
    respx.post("https://api.example.com/notify").mock(return_value=httpx.Response(200))
    client = AlertClient("https://api.example.com/notify", "k")
    rules = AlertRules(client, db, ["large_order"], dedup_minutes=30)

    assert await rules.handle("large_order", {"symbol": "BTCUSDT", "side": "bid",
                                              "price": 63850.0, "quantity": 350})
    assert not await rules.handle("large_order", {"symbol": "BTCUSDT", "side": "bid",
                                                  "price": 63850.5, "quantity": 320})
    assert await rules.handle("large_order", {"symbol": "BTCUSDT", "side": "ask",
                                              "price": 63855.0, "quantity": 400})


@respx.mock
async def test_rules_dedup_stop_volume_price_ticks(db):
    """Volumen de parada en el mismo lado no re-alerta por ticks de precio."""
    respx.post("https://api.example.com/notify").mock(return_value=httpx.Response(200))
    client = AlertClient("https://api.example.com/notify", "k")
    rules = AlertRules(client, db, ["stop_volume"], dedup_minutes=30)

    assert await rules.handle("stop_volume", {"symbol": "BTCUSDT", "side": "bid",
                                              "price": 63800.0, "quantity": 600})
    assert not await rules.handle("stop_volume", {"symbol": "BTCUSDT", "side": "bid",
                                                  "price": 63800.5, "quantity": 550})
    assert await rules.handle("stop_volume", {"symbol": "BTCUSDT", "side": "ask",
                                              "price": 63900.0, "quantity": 700})


async def test_rules_no_client(db):
    client = AlertClient("", "")  # deshabilitado
    rules = AlertRules(client, db, ["fvg_new"])
    assert not await rules.handle("fvg_new",
                                  {"symbol": "BTCUSDT", "timeframe": "5m", "formed_at": 1})
    cur = await db.conn.execute("SELECT COUNT(*) FROM alerts")
    assert (await cur.fetchone())[0] == 1  # igual queda registrado el intento


def test_build_payload_order_blocks_uses_text():
    p = build_payload("order_blocks", {"symbol": "BTCUSDT", "text": "🐋 resumen"})
    assert p["severity"] == "info"
    assert p["detail"] == "🐋 resumen"


def test_dedup_key_pattern():
    p = {"symbol": "BTCUSDT", "timeframe": "1h", "pattern": "engulfing",
         "open_time": 123456}
    assert dedup_key("pattern", p) == "pattern:BTCUSDT:1h:engulfing:123456"
