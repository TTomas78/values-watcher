import pytest

from values_watcher.core.fvg import Candle
from values_watcher.core.orderbook import BookSnapshot
from values_watcher.monitor import LiveMonitor
from values_watcher.storage.db import Database


@pytest.fixture
async def db(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    yield db
    await db.close()


def c(t, o, h, l, cl):
    return Candle(open_time=t, open=o, high=h, low=l, close=cl)


async def test_monitor_detects_and_stores_fvg(db):
    events = []

    async def handler(t, p):
        events.append((t, p))

    mon = LiveMonitor(["BTCUSDT"], ["5m"], db, on_event=handler)
    await mon.on_candle("BTCUSDT", "5m", c(0, 100, 101, 99, 100), 10)
    await mon.on_candle("BTCUSDT", "5m", c(1, 100, 105, 100, 104), 10)
    await mon.on_candle("BTCUSDT", "5m", c(2, 104, 106, 102, 105), 10)

    assert [e[0] for e in events] == ["fvg_new"]
    cur = await db.conn.execute("SELECT direction, top, bottom, status FROM fvgs")
    row = await cur.fetchone()
    assert row == ("bullish", 102.0, 101.0, "open")

    await mon.on_candle("BTCUSDT", "5m", c(3, 103, 103, 100, 101.5), 10)
    assert [e[0] for e in events] == ["fvg_new", "fvg_mitigated"]
    cur = await db.conn.execute("SELECT status FROM fvgs")
    assert (await cur.fetchone())[0] == "mitigated"


async def test_monitor_wall_and_imbalance_events(db):
    events = []

    async def handler(t, p):
        events.append((t, p))

    mon = LiveMonitor(["BTCUSDT"], ["5m"], db, wall_multiplier=5.0,
                      imbalance_threshold=0.6, on_event=handler)
    book = BookSnapshot(
        symbol="BTCUSDT",
        bids=[(100.0, 1.0), (99.0, 1.0), (98.0, 50.0), (97.0, 1.0)],
        asks=[(101.0, 1.0), (102.0, 1.0), (103.0, 1.0)],
        timestamp=123,
    )
    await mon.on_book(book)
    types = [t for t, _ in events]
    assert "wall" in types
    assert "imbalance" in types  # 53 vs 3 → ratio muy alto
    wall = next(p for t, p in events if t == "wall")
    assert wall["price"] == 98.0


async def test_monitor_watchlist_events(db):
    events = []

    async def handler(t, p):
        events.append((t, p))

    mon = LiveMonitor(["BTCUSDT"], ["5m"], db, on_event=handler,
                      watch={"BTCUSDT": {"price_targets": [100.0], "stop_volume": 50.0}})
    # primer libro: mid 100.5 (estado inicial), pared bid de 60
    await mon.on_book(BookSnapshot("BTCUSDT", [(100.0, 60.0), (99.0, 1.0)],
                                   [(101.0, 1.0), (102.0, 1.0)], 1))
    # segundo libro: mid cae a 99.5 → cruza 100 hacia abajo
    await mon.on_book(BookSnapshot("BTCUSDT", [(99.0, 1.0), (98.0, 1.0)],
                                   [(100.0, 1.0), (101.0, 1.0)], 2))
    types = [t for t, _ in events]
    assert "stop_volume" in types
    assert "price_target" in types
    pt = next(p for t, p in events if t == "price_target")
    assert pt["target"] == 100.0 and pt["crossed"] == "down"
    sv = next(p for t, p in events if t == "stop_volume")
    assert sv["price"] == 100.0 and sv["volume"] == 60.0


async def test_monitor_liquidation_event_and_storage(db):
    from values_watcher.collectors.binance_ws import Liquidation
    events = []

    async def handler(t, p):
        events.append((t, p))

    mon = LiveMonitor(["BTCUSDT"], ["5m"], db, on_event=handler,
                      liq_min_alert_usd=50_000, liq_critical_multiplier=5.0)
    liq_small = Liquidation("BTCUSDT", "long", 63000.0, 0.1, 6300.0, 1)
    liq_big = Liquidation("BTCUSDT", "short", 63500.0, 5.0, 317_500.0, 2)
    await mon.on_liquidation(liq_small)
    await mon.on_liquidation(liq_big)

    cur = await db.conn.execute("SELECT COUNT(*) FROM liquidations")
    assert (await cur.fetchone())[0] == 2  # ambas persistidas
    assert [e[0] for e in events] == ["liquidation"]  # solo la grande alerta
    assert events[0][1]["severity"] == "critical"     # 317k ≥ 250k
    assert events[0][1]["side"] == "short"

    clusters = await db.liquidation_clusters("BTCUSDT", since_ms=0, bucket_usd=50)
    assert len(clusters) == 2
    assert clusters[0]["usd"] == 317_500.0  # ordenado por USD desc
