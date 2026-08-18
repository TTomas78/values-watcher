from values_watcher.core.watchlist import (
    PriceLadderTracker,
    PriceTargetTracker,
    detect_stop_volumes,
    mid_price,
)


def test_mid_price():
    assert mid_price([(100.0, 1)], [(101.0, 1)]) == 100.5
    assert mid_price([], [(101.0, 1)]) is None


def test_stop_volumes():
    bids = [(100.0, 600.0), (99.0, 10.0)]
    asks = [(101.0, 700.0), (102.0, 5.0)]
    events = detect_stop_volumes(bids, asks, min_volume=500)
    assert len(events) == 2
    assert events[0] == {"side": "bid", "price": 100.0, "volume": 600.0, "threshold": 500}
    assert events[1]["side"] == "ask"
    assert detect_stop_volumes(bids, asks, min_volume=1000) == []


def test_price_target_crossings():
    tracker = PriceTargetTracker({"BTCUSDT": [63000, 65000]})
    assert tracker.check("BTCUSDT", 64000) == []  # primer chequeo: solo establece estado
    events = tracker.check("BTCUSDT", 65100)      # cruza 65000 hacia arriba
    assert events == [{"symbol": "BTCUSDT", "target": 65000, "price": 65100,
                       "crossed": "up"}]
    assert tracker.check("BTCUSDT", 65200) == []  # sin nuevo cruce
    events = tracker.check("BTCUSDT", 64500)      # cruza 65000 hacia abajo
    assert events[0]["crossed"] == "down" and events[0]["target"] == 65000


def test_price_target_multiple_levels():
    tracker = PriceTargetTracker({"BTCUSDT": [100, 110]})
    tracker.check("BTCUSDT", 95)
    events = tracker.check("BTCUSDT", 115)  # cruza ambos
    assert {e["target"] for e in events} == {100, 110}


LADDERS = {"BTCUSDT": [{"level": 60000, "step_pct": 1.0, "direction": "below"}]}


def test_ladder_single_alert_per_step():
    t = PriceLadderTracker(LADDERS)
    assert t.check("BTCUSDT", 60500) == []       # arriba del nivel: nada
    events = t.check("BTCUSDT", 59900)           # cruza 60000
    assert len(events) == 1
    assert events[0]["threshold"] == 60000 and events[0]["step"] == 0
    assert t.check("BTCUSDT", 59800) == []       # sigue debajo: NO repite
    assert t.check("BTCUSDT", 59500) == []       # aún no llega al -1% (59400)
    events = t.check("BTCUSDT", 59300)           # cruza el -1%
    assert len(events) == 1 and events[0]["step"] == 1
    assert events[0]["threshold"] == 59400
    assert t.check("BTCUSDT", 59350) == []       # no repite el escalón 1


def test_ladder_multiple_steps_in_one_move():
    t = PriceLadderTracker(LADDERS)
    events = t.check("BTCUSDT", 58000)  # cae de golpe varios escalones
    assert len(events) >= 3             # 60000, 59400, 58806...
    assert [e["step"] for e in events] == list(range(len(events)))
    assert t.check("BTCUSDT", 57900) == []  # ya notificados


def test_ladder_rearms_when_price_recovers():
    t = PriceLadderTracker(LADDERS)
    assert len(t.check("BTCUSDT", 59900)) == 1   # escalón 0 notificado
    t.check("BTCUSDT", 60100)                    # vuelve arriba → rearma
    events = t.check("BTCUSDT", 59800)           # nuevo cruce → alerta de nuevo
    assert len(events) == 1 and events[0]["step"] == 0


def test_ladder_direction_above():
    t = PriceLadderTracker({"BTCUSDT": [{"level": 65000, "step_pct": 1.0,
                                         "direction": "above"}]})
    assert t.check("BTCUSDT", 64000) == []
    events = t.check("BTCUSDT", 65700)   # cruza 65000 y el +1% (65650)
    assert len(events) == 2
    assert events[0]["direction"] == "above"
    assert t.check("BTCUSDT", 65800) == []
