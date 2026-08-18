import httpx
import pytest

from values_watcher.collectors.kiyotaka import (
    KiyotakaCollector,
    _extract_block_size,
    build_flow_summary,
    build_oi_summary,
    build_order_blocks_summary,
    fetch_open_interest,
    fetch_trade_flow,
    find_large_blocks,
    iter_blocks,
)
from values_watcher.storage.db import Database


@pytest.fixture
async def db(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    yield db
    await db.close()


def mock_http():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/block-sizes":
            assert request.headers["X-Kiyotaka-Key"] == "kiyo-key"
            assert request.url.params["rawSymbol"] == "BTCUSDT"
            return httpx.Response(200, json={"blockSize": 5})
        if request.url.path == "/v1/points":
            params = request.url.params
            assert params["type"] == "BLOCK_BOOK_SNAPSHOT_AGG"
            assert params["blockSize"] == "25"  # HD = 5 × raw
            return httpx.Response(200, json={"points": [[1, 2, 3]]})
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             base_url="https://api.kiyotaka.ai",
                             headers={"X-Kiyotaka-Key": "kiyo-key"})


async def test_fetch_heatmap_uses_hd_block_size(db):
    collector = KiyotakaCollector("kiyo-key", ["BTCUSDT"], db, http=mock_http())
    client = collector._client()
    heatmap = await collector.fetch_heatmap(client, "BTCUSDT")
    assert heatmap == {"points": [[1, 2, 3]]}
    # cache: segunda llamada no vuelve a pedir block-sizes
    assert collector._block_sizes["BTCUSDT"] == 25


def test_extract_block_size_variants():
    assert _extract_block_size({"blockSize": 5}) == 5
    assert _extract_block_size({"blockSizes": [3]}) == 3  # forma real de la API
    assert _extract_block_size({"data": {"block_size": 5}}) == 5
    assert _extract_block_size([{"blockSize": 5}]) == 5
    with pytest.raises(ValueError):
        _extract_block_size({})


def test_disabled_without_key():
    assert not KiyotakaCollector("", ["BTCUSDT"], None).enabled


HEATMAP = {
    "series": [{
        "id": {"type": "BLOCK_BOOK_SNAPSHOT_AGG", "interval": "MINUTE"},
        "points": [
            {"Point": {
                "bids": [63900, 25.3, 63875, 350.0, 63850, 950.5, 63825, 10.0],
                "asks": [63925, 12.0, 63950, 420.0],
            }},
            {"Point": {"bids": [63900, 1.0], "asks": []}},  # punto viejo, ignorado
        ],
    }],
}


def test_iter_blocks_takes_newest_point():
    bids, asks = iter_blocks(HEATMAP)
    assert bids[1] == (63875.0, 350.0)
    assert asks == [(63925.0, 12.0), (63950.0, 420.0)]
    assert iter_blocks({}) == ([], [])
    assert iter_blocks({"series": [{"points": []}]}) == ([], [])


def test_find_large_blocks_threshold_and_severity():
    events = find_large_blocks(HEATMAP, threshold=300, critical_multiplier=3.0)
    assert len(events) == 3
    by_price = {e["price"]: e for e in events}
    assert by_price[63875.0]["severity"] == "warning"   # 350 ≥ 300
    assert by_price[63850.0]["severity"] == "critical"  # 950.5 ≥ 900
    assert by_price[63950.0]["side"] == "ask"
    assert 63900.0 not in by_price  # 25.3 < 300


def test_find_large_blocks_none():
    assert find_large_blocks(HEATMAP, threshold=10000) == []


def mock_http_target_blocksize():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/block-sizes":
            return httpx.Response(200, json={"blockSizes": [3]})
        if request.url.path == "/v1/points":
            assert request.url.params["blockSize"] == "24"  # 25 → múltiplo de 3 más cercano
            return httpx.Response(200, json={"series": []})
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             base_url="https://api.kiyotaka.ai",
                             headers={"X-Kiyotaka-Key": "k"})


async def test_block_size_target_snaps_to_raw_multiple(db):
    collector = KiyotakaCollector("k", ["BTCUSDT"], db, block_size_target=25,
                                  http=mock_http_target_blocksize())
    bs = await collector.get_block_size(collector._client(), "BTCUSDT")
    assert bs == 24


def test_order_blocks_summary():
    bids = [(63900.0, 350.0), (63500.0, 1200.0), (61000.0, 500.0), (63950.0, 10.0)]
    asks = [(64100.0, 400.0), (66000.0, 800.0)]
    s = build_order_blocks_summary("BTCUSDT", 63972.8, bids, asks,
                                   range_usd=2000, min_volume=300)
    # dentro de rango: bids 63900, 63500 (61000 fuera), 63950 descartado por volumen
    assert [b["price"] for b in s["below"]] == [63900.0, 63500.0]
    assert [a["price"] for a in s["above"]] == [64100.0]
    assert "63,900" in s["text"] and "1,200.0 BTC" in s["text"]
    assert "RESISTENCIA" in s["text"] and "SOPORTE" in s["text"]
    # totales incluyen todas las órdenes del rango (63950 suma pese a ser chica)
    assert "Totales (todas las órdenes ±2,000 USD): 400 BTC arriba / 1.6k BTC abajo" in s["text"]


def mock_flow_http():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/points"
        assert request.url.params["type"] == "TRADE_AGG"
        exchange = request.url.params["exchange"]
        if exchange == "COINBASE":
            assert request.url.params["rawSymbol"] == "BTC-USD"
        vol = {"BINANCE": {"BUY": 100.0, "SELL": 150.0},
               "COINBASE": {"BUY": 50.0, "SELL": 40.0},
               "BYBIT_SPOT": {"BUY": 0.0, "SELL": 0.0},
               "OKEX": {"BUY": 10.0, "SELL": 30.0},
               "BINANCE_FUTURES": {"BUY": 900.0, "SELL": 400.0},
               "BYBIT": {"BUY": 200.0, "SELL": 250.0},
               "OKEX_SWAP": {"BUY": 0.0, "SELL": 0.0}}[exchange]
        series = [{"id": {"side": side},
                   "points": [{"Point": {"volume": v}}]}
                  for side, v in vol.items()]
        return httpx.Response(200, json={"series": series})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             base_url="https://api.kiyotaka.ai",
                             headers={"X-Kiyotaka-Key": "k"})


async def test_fetch_trade_flow():
    rows = await fetch_trade_flow(mock_flow_http(), "BTCUSDT", minutes=60,
                                  spacing=0)
    by_name = {(r["name"], r["kind"]): r for r in rows}
    assert by_name[("Binance", "spot")]["sell"] == 150.0
    assert by_name[("Coinbase", "spot")]["buy"] == 50.0
    assert by_name[("Bybit", "futures")]["sell"] == 250.0
    assert by_name[("OKX", "futures")]["buy"] == 0.0  # venue sin datos


def test_build_flow_summary():
    rows = [
        {"name": "Binance", "kind": "spot", "buy": 100.0, "sell": 150.0},
        {"name": "Coinbase", "kind": "spot", "buy": 50.0, "sell": 40.0},
        {"name": "Binance", "kind": "futures", "buy": 900.0, "sell": 400.0},
        {"name": "Bybit", "kind": "futures", "buy": 200.0, "sell": 250.0},
    ]
    text = build_flow_summary("BTCUSDT", 60, rows)
    assert "SPOT" in text and "FUTUROS" in text
    assert "Binance: vendiendo 50.0 BTC netos" in text
    assert "TOTAL: vendiendo 40.0 BTC netos" in text  # spot: 150-190
    assert "TOTAL: comprando 450.0 BTC netos" in text  # fut: 1100-650
    assert "🔴" in text and "🟢" in text


def mock_oi_http():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/points"
        tipo = request.url.params["type"]
        if tipo == "OPEN_INTEREST_AGG":
            pts = [{"Point": {"close": 1100.0}}, {"Point": {"close": 1000.0}}]
        elif tipo == "FUNDING_RATE_AGG":
            pts = [{"Point": {"rateClose": 0.0001}}]
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"series": [{"points": pts}]})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             base_url="https://api.kiyotaka.ai",
                             headers={"X-Kiyotaka-Key": "k"})


async def test_fetch_open_interest():
    rows = await fetch_open_interest(mock_oi_http(), "BTCUSDT", hours=6,
                                     spacing=0)
    assert len(rows) == 3  # Binance, Bybit, OKX
    assert rows[0]["oi_now"] == 1100.0
    assert rows[0]["oi_change_pct"] == pytest.approx(10.0)
    assert rows[0]["funding_pct"] == pytest.approx(0.01)


def test_build_oi_summary():
    rows = [
        {"name": "Binance", "oi_now": 1100.0, "oi_change_pct": 10.0,
         "funding_pct": 0.01},
        {"name": "Bybit", "oi_now": 500.0, "oi_change_pct": -2.0,
         "funding_pct": -0.005},
        {"name": "OKX", "oi_now": None, "oi_change_pct": None,
         "funding_pct": None},
    ]
    text = build_oi_summary("BTCUSDT", 6, rows)
    assert "Binance: 1,100 BTC (+10.00% en 6h)" in text
    assert "funding 🟢 +0.0100%" in text
    assert "funding 🔴 -0.0050%" in text
    assert "OKX: sin datos" in text
    assert "TOTAL OI: 1,600 BTC" in text
