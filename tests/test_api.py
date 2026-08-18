import pytest
from httpx import ASGITransport, AsyncClient

from values_watcher.api.main import create_app
from values_watcher.core.fvg import Candle
from values_watcher.core.fvg import Direction, Fvg
from values_watcher.storage.db import Database


@pytest.fixture
async def client(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    await db.insert_candle("BTCUSDT", "5m", Candle(1000, 1, 2, 0.5, 1.5), 10.0)
    await db.insert_fvg(Fvg("BTCUSDT", "5m", Direction.BULLISH, top=102, bottom=101,
                            formed_at=2000))
    await db.insert_snapshot("BTCUSDT", 3000, "[[100, 1]]", "[[101, 2]]")
    await db.insert_heatmap("BTCUSDT", 4000, '{"points": []}')
    app = create_app(db)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c
    await db.close()


async def test_health(client):
    r = await client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


async def test_candles(client):
    r = await client.get("/api/candles?symbol=BTCUSDT&timeframe=5m")
    data = r.json()
    assert len(data) == 1
    assert data[0]["close"] == 1.5
    assert data[0]["time"] == 1


async def test_fvgs(client):
    r = await client.get("/api/fvgs?symbol=BTCUSDT&timeframe=5m")
    data = r.json()
    assert data[0]["direction"] == "bullish"
    assert data[0]["status"] == "open"


async def test_orderbook(client):
    r = await client.get("/api/orderbook/BTCUSDT")
    data = r.json()
    assert data["bids"] == [[100, 1]]


async def test_orderbook_empty(client):
    r = await client.get("/api/orderbook/DOGEUSDT")
    assert r.json()["bids"] == []


async def test_heatmap(client):
    r = await client.get("/api/heatmap/BTCUSDT")
    assert r.json()["data"] == {"points": []}


async def test_index_served(client):
    r = await client.get("/")
    assert r.status_code == 200 and "values-watcher" in r.text
