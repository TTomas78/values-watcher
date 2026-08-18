from values_watcher.core.fvg import Candle, Direction, Fvg, FvgStatus, FvgTracker, detect_fvg


def c(t: int, o: float, h: float, l: float, cl: float) -> Candle:
    return Candle(open_time=t, open=o, high=h, low=l, close=cl)


def test_bullish_fvg_detected():
    c1 = c(0, 100, 101, 99, 100)
    c2 = c(1, 100, 105, 100, 104)
    c3 = c(2, 104, 106, 102, 105)  # low 102 > high1 101
    fvg = detect_fvg(c1, c2, c3, "BTCUSDT", "5m")
    assert fvg is not None
    assert fvg.direction == Direction.BULLISH
    assert fvg.bottom == 101
    assert fvg.top == 102
    assert fvg.formed_at == 2
    assert fvg.status == FvgStatus.OPEN


def test_bearish_fvg_detected():
    c1 = c(0, 100, 101, 99, 100)
    c2 = c(1, 99, 100, 95, 96)
    c3 = c(2, 96, 98, 94, 95)  # high 98 < low1 99
    fvg = detect_fvg(c1, c2, c3, "BTCUSDT", "5m")
    assert fvg is not None
    assert fvg.direction == Direction.BEARISH
    assert fvg.top == 99
    assert fvg.bottom == 98


def test_no_fvg_when_wicks_overlap():
    c1 = c(0, 100, 101, 99, 100)
    c2 = c(1, 100, 105, 100, 104)
    c3 = c(2, 104, 106, 100.5, 105)  # low toca el rango de c1
    assert detect_fvg(c1, c2, c3, "BTCUSDT", "5m") is None


def test_mitigation():
    fvg = Fvg("BTCUSDT", "5m", Direction.BULLISH, top=102, bottom=101, formed_at=2)
    assert not fvg.check_mitigation(c(3, 105, 106, 103, 104))
    assert fvg.status == FvgStatus.OPEN
    assert fvg.check_mitigation(c(4, 103, 103, 100, 101))  # entra al gap
    assert fvg.status == FvgStatus.MITIGATED
    assert fvg.mitigated_at == 4
    assert not fvg.check_mitigation(c(5, 100, 100, 99, 99))  # no repite


def test_tracker_emits_new_and_mitigated():
    tracker = FvgTracker("BTCUSDT", "5m")
    assert tracker.on_candle_closed(c(0, 100, 101, 99, 100)) == []
    assert tracker.on_candle_closed(c(1, 100, 105, 100, 104)) == []
    events = tracker.on_candle_closed(c(2, 104, 106, 102, 105))
    assert len(events) == 1 and events[0].direction == Direction.BULLISH
    assert len(tracker.open_fvgs) == 1

    # vela que vuelve al gap [101, 102]
    events = tracker.on_candle_closed(c(3, 103, 103, 100, 101.5))
    assert len(events) == 1 and events[0].status == FvgStatus.MITIGATED
    assert tracker.open_fvgs == []
