from values_watcher.core.orderbook import (
    BookSnapshot,
    compute_imbalance,
    detect_walls,
    is_extreme,
)


def make_book(bids, asks) -> BookSnapshot:
    return BookSnapshot(symbol="BTCUSDT", bids=bids, asks=asks, timestamp=0)


def test_detect_walls():
    bids = [(100.0, 1.0), (99.0, 1.0), (98.0, 20.0), (97.0, 1.0)]  # pared en 98
    asks = [(101.0, 1.0), (102.0, 1.0), (103.0, 1.0)]
    walls = detect_walls(make_book(bids, asks), multiplier=5.0)
    assert len(walls) == 1
    assert walls[0].side == "bid"
    assert walls[0].price == 98.0
    assert walls[0].multiple == 20.0


def test_no_walls_when_flat():
    levels = [(100.0 - i, 1.0) for i in range(5)]
    walls = detect_walls(make_book(levels, [(101.0, 1.0), (102.0, 1.0), (103.0, 1.0)]))
    assert walls == []


def test_walls_need_min_levels():
    walls = detect_walls(make_book([(100.0, 100.0)], [(101.0, 1.0)]))
    assert walls == []


def test_imbalance():
    imb = compute_imbalance(make_book([(100.0, 3.0)], [(101.0, 1.0)]))
    assert imb is not None
    assert imb.ratio == 0.75
    assert is_extreme(imb, threshold=0.6)

    imb2 = compute_imbalance(make_book([(100.0, 1.0)], [(101.0, 1.0)]))
    assert not is_extreme(imb2, threshold=0.6)

    # extremo del lado ask
    imb3 = compute_imbalance(make_book([(100.0, 1.0)], [(101.0, 4.0)]))
    assert is_extreme(imb3, threshold=0.7)


def test_imbalance_empty_book():
    assert compute_imbalance(make_book([], [])) is None
