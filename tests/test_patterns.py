"""tests/test_patterns.py"""
from values_watcher.core.fvg import Candle
from values_watcher.core.patterns import detect_patterns


def mk(i, o, h, l, c):
    return Candle(open_time=i, open=o, high=h, low=l, close=c)


def flat(n, base=100.0):
    # cuerpo ~30% del rango: sin patrón (open=base, close=base+0.3)
    return [mk(i, base, base + 0.5, base - 0.5, base + 0.3) for i in range(n)]


def test_insufficient_history_returns_empty():
    candles = flat(10)
    assert detect_patterns("BTCUSDT", "1h", candles, min_candles=50) == []


def test_flat_candles_detect_nothing():
    candles = flat(60)
    assert detect_patterns("BTCUSDT", "1h", candles, min_candles=50) == []


def test_bullish_engulfing_detected():
    candles = flat(58, base=100.0)
    candles.append(mk(58, 100, 100.3, 96.8, 97.0))    # vela bajista
    candles.append(mk(59, 96.5, 101.5, 96.4, 101.2))  # envolvente alcista
    found = detect_patterns("BTCUSDT", "1h", candles, min_candles=50)
    names = {p["name"]: p for p in found}
    assert "engulfing" in names
    assert names["engulfing"]["direction"] == "bullish"


def test_bearish_engulfing_detected():
    candles = flat(58, base=100.0)
    candles.append(mk(58, 100, 103.2, 99.7, 103.0))   # vela alcista
    candles.append(mk(59, 103.5, 103.6, 98.5, 98.8))  # envolvente bajista
    found = detect_patterns("BTCUSDT", "1h", candles, min_candles=50)
    names = {p["name"]: p for p in found}
    assert "engulfing" in names
    assert names["engulfing"]["direction"] == "bearish"


def test_config_patterns_defaults(tmp_path):
    import yaml
    from values_watcher.config import load_config
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({
        "patterns": {"enabled": True, "timeframes": ["1h", "4h", "1d"],
                     "min_candles": 50}
    }))
    cfg = load_config(cfg_file)
    assert cfg.patterns.enabled is True
    assert cfg.patterns.timeframes == ["1h", "4h", "1d"]
    assert cfg.patterns.min_candles == 50
