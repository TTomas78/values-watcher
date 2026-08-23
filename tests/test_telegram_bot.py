import json

import pytest

from values_watcher.alerts.telegram_bot import (
    TelegramCommandBot,
    WatchStore,
    parse_alert_command,
    parse_symbol,
)
from values_watcher.core.fvg import Candle, Direction, Fvg
from values_watcher.core.orderbook import BookSnapshot
from values_watcher.monitor import LiveMonitor
from values_watcher.storage.db import Database


def test_parse_simple_target():
    spec = parse_alert_command("BTC 60000")
    assert spec == {"symbol": "BTCUSDT", "type": "target", "level": 60000.0}


def test_parse_ladder():
    spec = parse_alert_command("btc 60000 below 1%")
    assert spec == {"symbol": "BTCUSDT", "type": "ladder", "level": 60000.0,
                    "direction": "below", "step_pct": 1.0}
    spec2 = parse_alert_command("ETH 2000 above 0.5%")
    assert spec2["symbol"] == "ETHUSDT" and spec2["direction"] == "above"


def test_parse_invalid():
    assert parse_alert_command("BTC") is None
    assert parse_alert_command("") is None


def test_parse_symbol():
    assert parse_symbol("") == "BTCUSDT"
    assert parse_symbol("eth") == "ETHUSDT"
    assert parse_symbol("SOLUSDT") == "SOLUSDT"


@pytest.fixture
def store(tmp_path):
    return WatchStore(str(tmp_path / "overrides.json"))


def test_store_add_remove_persist(store, tmp_path):
    store.add({"symbol": "BTCUSDT", "type": "target", "level": 60000.0})
    store.add({"symbol": "BTCUSDT", "type": "ladder", "level": 65000.0,
               "direction": "above", "step_pct": 1.0})
    store.add({"symbol": "BTCUSDT", "type": "target", "level": 60000.0})  # duplicado
    assert store.rules["BTCUSDT"]["price_targets"] == [60000.0]
    assert len(store.rules["BTCUSDT"]["price_ladders"]) == 1

    reloaded = WatchStore(str(tmp_path / "overrides.json"))
    assert reloaded.rules["BTCUSDT"]["price_targets"] == [60000.0]

    assert store.remove("BTCUSDT", 60000.0) == 1
    assert store.remove("BTCUSDT", 99999.0) == 0
    assert store.remove("ETHUSDT", 1.0) == 0


def test_store_describe(store):
    assert "Sin reglas" in store.describe()
    store.add({"symbol": "BTCUSDT", "type": "ladder", "level": 60000.0,
               "direction": "below", "step_pct": 1.0})
    text = store.describe()
    assert "BTCUSDT" in text and "escalera" in text


@pytest.fixture
async def db(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    yield db
    await db.close()


def make_bot(store, db):
    monitor = LiveMonitor(["BTCUSDT"], ["5m"], db)
    state = {"enabled": True}
    bot = TelegramCommandBot("token", "123", store, monitor, state,
                             db=db, symbols=["BTCUSDT"])
    return bot, monitor, state


async def test_setalert_applies_to_monitor(store, db):
    bot, monitor, _ = make_bot(store, db)
    reply = await bot._dispatch("setalert", "BTC 60000 below 1%")
    assert "✅" in reply
    assert monitor.ladder_tracker.ladders["BTCUSDT"] == [
        {"level": 60000.0, "step_pct": 1.0, "direction": "below"}]

    events = []

    async def collect(t, p):
        events.append((t, p))
    monitor.on_event = collect
    await monitor.on_book(BookSnapshot("BTCUSDT", [(59900.0, 1.0), (59800.0, 1.0)],
                                       [(59910.0, 1.0), (59920.0, 1.0)], 1))
    assert any(t == "price_ladder" for t, _ in events)


async def test_delalert(store, db):
    bot, monitor, _ = make_bot(store, db)
    await bot._dispatch("setalert", "BTC 60000")
    assert monitor.price_tracker.targets["BTCUSDT"] == [60000.0]
    reply = await bot._dispatch("delalert", "BTC 60000")
    assert "1" in reply
    assert monitor.price_tracker.targets.get("BTCUSDT", []) == []


async def test_pause_resume_status(store, db):
    bot, _, state = make_bot(store, db)
    assert "pausadas" in await bot._dispatch("pause", "")
    assert state["enabled"] is False
    assert "reanudadas" in await bot._dispatch("resume", "")
    assert state["enabled"] is True
    assert "activas" in await bot._dispatch("status", "")
    assert "no reconocido" in await bot._dispatch("sarasa", "")


HEATMAP = {"series": [{"points": [{"Point": {
    "bids": [63900, 350.0, 63500, 1200.0],
    "asks": [64100, 400.0],
}}]}]}


async def test_orderblocks_command(store, db):
    await db.insert_heatmap("BTCUSDT", 1000, json.dumps(HEATMAP))
    bot, _, _ = make_bot(store, db)

    async def fake_price(symbol):
        return 63972.8
    bot._fetch_price = fake_price
    reply = await bot._dispatch("orderblocks", "btc")
    assert "Order blocks BTCUSDT" in reply
    assert "63,900" in reply and "1,200.0 BTC" in reply
    assert "64,100" in reply


async def test_orderblocks_sin_datos(store, db):
    bot, _, _ = make_bot(store, db)
    reply = await bot._dispatch("orderblocks", "ETH")
    assert "Sin heatmap" in reply


async def test_fvgs_command(store, db):
    await db.insert_fvg(Fvg("BTCUSDT", "5m", Direction.BULLISH, top=64100,
                            bottom=64000, formed_at=1000))
    bot, _, _ = make_bot(store, db)
    reply = await bot._dispatch("fvgs", "BTC")
    assert "🟢" in reply and "abierto" in reply


async def test_liquidaciones_command(store, db):
    import time
    from values_watcher.collectors.binance_ws import Liquidation
    now = int(time.time() * 1000)
    await db.insert_liquidation(Liquidation("BTCUSDT", "long", 63400, 100, 6_340_000, now))
    bot, _, _ = make_bot(store, db)
    reply = await bot._dispatch("liquidaciones", "BTC")
    assert "$6.34M" in reply and "🔻" in reply


async def test_handle_ignores_other_chats(store, db):
    import httpx
    bot, _, _ = make_bot(store, db)
    sent = []

    def handler(request):
        sent.append(request.url.path)
        return httpx.Response(200, json={"ok": True})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                               base_url="https://api.telegram.org/bottoken")
    upd = {"update_id": 1, "message": {"text": "/pause", "chat": {"id": 999}}}
    await bot._handle(client, upd)
    assert sent == []  # otro chat: no responde
    upd2 = {"update_id": 2, "message": {"text": "/status@valuewatcher_bot",
                                        "chat": {"id": 123}}}
    await bot._handle(client, upd2)
    assert sent == ["/bottoken/sendMessage"]


async def test_send_alert_with_button(store, db):
    import httpx
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})
    bot, _, _ = make_bot(store, db)
    bot._http = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                  base_url="https://api.telegram.org/bottoken")
    await bot.send_alert("price_ladder", {
        "symbol": "BTCUSDT", "level": 60000, "step": 1, "threshold": 59400,
        "price": 59350, "direction": "below"})
    body = sent[0]
    assert "59,400" in body["text"] and "BTCUSDT" in body["text"]
    button = body["reply_markup"]["inline_keyboard"][0][0]
    assert button["callback_data"] == "delrule:BTCUSDT:60000"


async def test_callback_removes_rule(store, db):
    import httpx
    calls = []

    def handler(request):
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})
    bot, monitor, _ = make_bot(store, db)
    await bot._dispatch("setalert", "BTC 60000")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                               base_url="https://api.telegram.org/bottoken")
    upd = {"update_id": 5, "callback_query": {
        "id": "cb1", "data": "delrule:BTCUSDT:60000.0",
        "message": {"message_id": 42, "chat": {"id": 123}, "text": "alerta..."}}}
    await bot._handle(client, upd)
    paths = [p for p, _ in calls]
    assert "/bottoken/answerCallbackQuery" in paths
    assert "/bottoken/editMessageText" in paths
    assert "ALERTA DESACTIVADA" in calls[1][1]["text"]
    # la regla QUEDA en el store pero desactivada, y sale del monitor
    assert store.rules["BTCUSDT"]["price_targets"] == [60000.0]
    assert 60000.0 in store.rules["BTCUSDT"]["disabled_targets"]
    assert monitor.price_tracker.targets.get("BTCUSDT", []) == []
    # /setalert del mismo nivel la reactiva
    await bot._dispatch("setalert", "BTC 60000")
    assert store.rules["BTCUSDT"]["disabled_targets"] == []
    assert monitor.price_tracker.targets["BTCUSDT"] == [60000.0]


async def test_callback_other_chat_ignored(store, db):
    import httpx
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"ok": True})
    bot, _, _ = make_bot(store, db)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                               base_url="https://api.telegram.org/bottoken")
    upd = {"update_id": 6, "callback_query": {
        "id": "cb2", "data": "delrule:BTCUSDT:60000.0",
        "message": {"message_id": 1, "chat": {"id": 999}, "text": "x"}}}
    await bot._handle(client, upd)
    assert calls == []


async def test_flujo_command(store, db):
    import httpx

    def handler(request):
        exchange = request.url.params["exchange"]
        vol = {"BINANCE": {"BUY": 10.0, "SELL": 25.0},
               "COINBASE": {"BUY": 0.0, "SELL": 0.0},
               "BYBIT_SPOT": {"BUY": 0.0, "SELL": 0.0},
               "OKEX": {"BUY": 0.0, "SELL": 0.0},
               "BINANCE_FUTURES": {"BUY": 80.0, "SELL": 30.0},
               "BYBIT": {"BUY": 0.0, "SELL": 0.0},
               "OKEX_SWAP": {"BUY": 0.0, "SELL": 0.0}}[exchange]
        series = [{"id": {"side": s}, "points": [{"Point": {"volume": v}}]}
                  for s, v in vol.items()]
        return httpx.Response(200, json={"series": series})

    bot, _, _ = make_bot(store, db)
    bot.kiyotaka_key = "k"
    bot._http = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                  base_url="https://api.kiyotaka.ai")
    reply = await bot._dispatch("flujo", "BTC 30")
    assert "Binance: vendiendo 15.0 BTC netos" in reply
    assert "TOTAL: comprando 50.0 BTC netos" in reply  # futuros


async def test_flujo_sin_key(store, db):
    bot, _, _ = make_bot(store, db)
    reply = await bot._dispatch("flujo", "")
    assert "sin API key" in reply


async def test_orderblocks_live_fetch(store, db):
    import httpx

    def handler(request):
        if "fapi.binance.com" in str(request.url):
            return httpx.Response(200, json={"price": "64000.0"})
        if request.url.path == "/v1/block-sizes":
            return httpx.Response(200, json={"blockSizes": [3]})
        if request.url.path == "/v1/points":
            point = {"Point": {"bids": [63900, 500, 63000, 400],
                               "asks": [64500, 600, 65200, 450]}}
            return httpx.Response(200, json={"series": [{"points": [point]}]})
        return httpx.Response(404)

    bot, _, _ = make_bot(store, db)
    bot.kiyotaka_key = "k"
    bot._http = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                  base_url="https://api.kiyotaka.ai")
    reply = await bot._dispatch("orderblocks", "BTC")
    assert "RESISTENCIA" in reply and "SOPORTE" in reply
    assert "63,900" in reply and "64,500" in reply
    assert "Precio actual: 64,000" in reply


async def test_oi_command(store, db):
    import httpx

    def handler(request):
        tipo = request.url.params["type"]
        if tipo == "OPEN_INTEREST_AGG":
            pts = [{"Point": {"close": 2100.0}}, {"Point": {"close": 2000.0}}]
        else:  # FUNDING_RATE_AGG
            pts = [{"Point": {"rateClose": 0.0002}}]
        return httpx.Response(200, json={"series": [{"points": pts}]})

    bot, _, _ = make_bot(store, db)
    bot.kiyotaka_key = "k"
    bot._http = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                  base_url="https://api.kiyotaka.ai")
    reply = await bot._dispatch("oi", "BTC 12")
    assert "Open interest BTCUSDT" in reply
    assert "2,100 BTC (+5.00% en 12h)" in reply
    assert "TOTAL OI: 6,300 BTC" in reply


async def test_patterns_toggle(store, db):
    bot, _, state = make_bot(store, db)

    reply = await bot._dispatch("patterns", "off")
    assert "pausadas" in reply
    assert state["patterns"] is False

    reply = await bot._dispatch("patterns", "")
    assert "pausadas" in reply

    reply = await bot._dispatch("patterns", "on")
    assert "activadas" in reply
    assert state["patterns"] is True
