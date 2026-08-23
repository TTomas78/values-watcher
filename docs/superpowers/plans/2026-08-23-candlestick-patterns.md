# Candlestick Patterns (TA-Lib) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detectar 8 patrones clásicos de velas (TA-Lib) en velas cerradas 1h/4h/1d y alertarlos por el canal notify estándar, con toggle `/patterns on|off` en el bot de Telegram.

**Architecture:** Módulo puro `core/patterns.py` (funciones TA-Lib sobre velas cerradas) invocado desde `LiveMonitor.on_candle` cuando el timeframe está en `patterns.timeframes`. El evento `pattern` fluye por `on_event` → `AlertRules` (dedup) → `AlertClient`. Los timeframes de patrones se suman a las suscripciones del collector/warmup en `app.py`, separados de los tfs de FVG.

**Tech Stack:** Python 3.12, TA-Lib (lib C del sistema + wrapper `TA-Lib`), numpy, pydantic (config), pytest + pytest-asyncio + respx.

**Spec:** `docs/superpowers/specs/2026-08-23-candlestick-patterns-design.md`

**Nota sobre commits:** el usuario no autorizó commits de git; ejecutar todos los pasos de código y tests pero OMITIR los pasos "Commit".

---

### Task 1: Dependencia TA-Lib

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Instalar la lib C del sistema**

Run: `brew install ta-lib`
Expected: instalación exitosa (o "already installed").

- [ ] **Step 2: Agregar dependencia a pyproject.toml**

En la lista `dependencies` agregar `"TA-Lib>=0.4.28"` y `"numpy>=1.26"` (verificar si numpy ya está; si está, solo TA-Lib).

- [ ] **Step 3: Instalar en el venv**

Run: `.venv/bin/pip install TA-Lib numpy`
Expected: instalación OK; `.venv/bin/python -c "import talib; print(talib.__version__)"` imprime versión.

---

### Task 2: Detector puro `core/patterns.py`

**Files:**
- Create: `src/values_watcher/core/patterns.py`
- Test: `tests/test_patterns.py`

- [ ] **Step 1: Escribir los tests que fallan**

```python
"""tests/test_patterns.py"""
from values_watcher.core.fvg import Candle
from values_watcher.core.patterns import detect_patterns


def mk(i, o, h, l, c):
    return Candle(open_time=i, open=o, high=h, low=l, close=c)


def flat(n, base=100.0):
    return [mk(i, base, base + 0.4, base - 0.4, base) for i in range(n)]


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
```

- [ ] **Step 2: Correr tests y verlos fallar**

Run: `.venv/bin/python -m pytest tests/test_patterns.py -q`
Expected: FAIL con `ModuleNotFoundError: values_watcher.core.patterns`.

- [ ] **Step 3: Implementación mínima**

```python
"""Detección de patrones de velas clásicos (TA-Lib) sobre velas cerradas."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import talib

from values_watcher.core.fvg import Candle

# Subset clásico de reversión: nombre -> función TA-Lib
PATTERNS: dict[str, Callable] = {
    "engulfing": talib.CDLENGULFING,
    "hammer": talib.CDLHAMMER,
    "hanging_man": talib.CDLHANGINGMAN,
    "morning_star": talib.CDLMORNINGSTAR,
    "evening_star": talib.CDLEVENINGSTAR,
    "doji": talib.CDLDOJI,
    "three_white_soldiers": talib.CDL3WHITESOLDIERS,
    "three_black_crows": talib.CDL3BLACKCROWS,
}


def detect_patterns(symbol: str, timeframe: str, candles: list[Candle],
                    min_candles: int = 50) -> list[dict]:
    """Evalúa la última vela del array. [] si no hay historial suficiente."""
    if len(candles) < min_candles:
        return []
    o = np.array([c.open for c in candles])
    h = np.array([c.high for c in candles])
    lo = np.array([c.low for c in candles])
    cl = np.array([c.close for c in candles])
    found = []
    for name, fn in PATTERNS.items():
        score = int(fn(o, h, lo, cl)[-1])
        if score == 0:
            continue
        direction = ("neutral" if name == "doji"
                     else "bullish" if score > 0 else "bearish")
        found.append({"name": name, "direction": direction, "score": score})
    return found
```

- [ ] **Step 4: Correr tests y verlos pasar**

Run: `.venv/bin/python -m pytest tests/test_patterns.py -q`
Expected: 4 passed.

---

### Task 3: Config `PatternsConfig`

**Files:**
- Modify: `src/values_watcher/config.py`
- Modify: `config.yaml`
- Test: `tests/test_api.py` no — crear test inline en `tests/test_patterns.py`

- [ ] **Step 1: Test que falla**

Agregar a `tests/test_patterns.py`:

```python
def test_config_patterns_defaults(tmp_path, monkeypatch):
    import yaml
    from values_watcher.config import load_config
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({
        "patterns": {"enabled": True, "timeframes": ["1h", "4h", "1d"], "min_candles": 50}
    }))
    cfg = load_config(cfg_file)
    assert cfg.patterns.enabled is True
    assert cfg.patterns.timeframes == ["1h", "4h", "1d"]
    assert cfg.patterns.min_candles == 50
```

- [ ] **Step 2: Verificar que falla**

Run: `.venv/bin/python -m pytest tests/test_patterns.py::test_config_patterns_defaults -q`
Expected: FAIL (`AttributeError: 'AppConfig' object has no attribute 'patterns'`).

- [ ] **Step 3: Implementar**

En `src/values_watcher/config.py`, después de `LiquidationsConfig`:

```python
class PatternsConfig(BaseModel):
    enabled: bool = True
    timeframes: list[str] = ["1h", "4h", "1d"]
    min_candles: int = 50   # historial mínimo de velas cerradas para evaluar
```

Y en `AppConfig` agregar el campo:

```python
    patterns: PatternsConfig = PatternsConfig()
```

En `config.yaml` agregar al final:

```yaml
# Patrones de velas (TA-Lib) sobre velas cerradas
patterns:
  enabled: true
  timeframes: [1h, 4h, 1d]
  min_candles: 50
```

Y en `alerts.enabled_events` de `config.yaml` agregar `- pattern`.

- [ ] **Step 4: Verificar que pasa**

Run: `.venv/bin/python -m pytest tests/test_patterns.py -q`
Expected: 5 passed.

---

### Task 4: Dedup key `pattern` en rules.py

**Files:**
- Modify: `src/values_watcher/alerts/rules.py`
- Test: `tests/test_alerts.py`

- [ ] **Step 1: Test que falla**

Agregar a `tests/test_alerts.py`:

```python
def test_dedup_key_pattern():
    p = {"symbol": "BTCUSDT", "timeframe": "1h", "pattern": "engulfing",
         "open_time": 123456}
    assert dedup_key("pattern", p) == "pattern:BTCUSDT:1h:engulfing:123456"
```

- [ ] **Step 2: Verificar que falla**

Run: `.venv/bin/python -m pytest tests/test_alerts.py::test_dedup_key_pattern -q`
Expected: FAIL (la key cae en el fallback JSON genérico).

- [ ] **Step 3: Implementar**

En `src/values_watcher/alerts/rules.py`, dentro de `dedup_key`, antes del fallback final:

```python
    if event_type == "pattern":
        return (f"pattern:{payload['symbol']}:{payload['timeframe']}"
                f":{payload['pattern']}:{payload['open_time']}")
```

- [ ] **Step 4: Verificar que pasa**

Run: `.venv/bin/python -m pytest tests/test_alerts.py -q`
Expected: all passed.

---

### Task 5: Integración en `LiveMonitor`

**Files:**
- Modify: `src/values_watcher/monitor.py`
- Test: `tests/test_monitor.py`

- [ ] **Step 1: Test que falla**

Agregar a `tests/test_monitor.py`:

```python
async def test_monitor_emits_pattern_on_configured_timeframe(db):
    events = []

    async def handler(t, p):
        events.append((t, p))

    mon = LiveMonitor(["BTCUSDT"], ["5m"], db, on_event=handler,
                      pattern_timeframes=["1h"], pattern_min_candles=50)
    base = [c(i, 100, 100.4, 99.6, 100) for i in range(58)]
    base.append(c(58, 100, 100.3, 96.8, 97.0))
    base.append(c(59, 96.5, 101.5, 96.4, 101.2))  # engulfing alcista
    for candle in base:
        await mon.on_candle("BTCUSDT", "1h", candle, 10)

    patterns = [p for t, p in events if t == "pattern"]
    assert any(p["pattern"] == "engulfing" and p["direction"] == "bullish"
               and p["timeframe"] == "1h" for p in patterns)


async def test_monitor_ignores_pattern_on_other_timeframes(db):
    events = []

    async def handler(t, p):
        events.append((t, p))

    mon = LiveMonitor(["BTCUSDT"], ["5m"], db, on_event=handler,
                      pattern_timeframes=["1h"], pattern_min_candles=50)
    base = [c(i, 100, 100.4, 99.6, 100) for i in range(58)]
    base.append(c(58, 100, 100.3, 96.8, 97.0))
    base.append(c(59, 96.5, 101.5, 96.4, 101.2))
    for candle in base:
        await mon.on_candle("BTCUSDT", "5m", candle, 10)  # tf no configurado

    assert not [e for e in events if e[0] == "pattern"]
```

- [ ] **Step 2: Verificar que fallan**

Run: `.venv/bin/python -m pytest tests/test_monitor.py -q -k pattern`
Expected: FAIL (`TypeError: unexpected keyword argument 'pattern_timeframes'`).

- [ ] **Step 3: Implementar**

En `src/values_watcher/monitor.py`:

1. Import arriba: `from values_watcher.core.patterns import detect_patterns`
2. En `__init__`, agregar parámetros `pattern_timeframes: list[str] | None = None, pattern_min_candles: int = 50` y al cuerpo:

```python
        self.pattern_timeframes = set(pattern_timeframes or [])
        self.pattern_min_candles = pattern_min_candles
        self._pattern_buffers: dict[tuple[str, str], list[Candle]] = {}
```

3. En `on_candle`, después de `await self.db.insert_candle(...)` y antes del chequeo de tracker:

```python
        if tf in self.pattern_timeframes:
            buf = self._pattern_buffers.setdefault((symbol, tf), [])
            buf.append(candle)
            del buf[:-self.pattern_min_candles]
            for p in detect_patterns(symbol, tf, buf, self.pattern_min_candles):
                await self.on_event("pattern", {
                    "symbol": symbol, "timeframe": tf, "pattern": p["name"],
                    "direction": p["direction"], "close": candle.close,
                    "open_time": candle.open_time,
                })
```

- [ ] **Step 4: Verificar que pasan**

Run: `.venv/bin/python -m pytest tests/test_monitor.py -q`
Expected: all passed.

---

### Task 6: Wiring en `app.py`

**Files:**
- Modify: `src/values_watcher/app.py`

- [ ] **Step 1: Timeframes unidos para collector y warmup**

En `run()`, después de cargar `cfg`:

```python
    pattern_tfs = cfg.patterns.timeframes if cfg.patterns.enabled else []
    all_timeframes = list(dict.fromkeys(cfg.timeframes + pattern_tfs))
```

- [ ] **Step 2: Estado del toggle de patrones**

Cambiar la línea de `alerts_state` por:

```python
    alerts_state = {"enabled": cfg.alerts.enabled, "patterns": True}
```

- [ ] **Step 3: Filtro en `on_event`**

Dentro de `on_event`, inmediatamente después del chequeo global `if not alerts_state["enabled"]: return`:

```python
        if event_type == "pattern" and not alerts_state.get("patterns", True):
            return
```

(El broadcast al dashboard queda antes de ambos chequeos, como hoy.)

- [ ] **Step 4: Pasar config al monitor principal y al de warmup**

En la construcción de `monitor`:

```python
        pattern_timeframes=pattern_tfs,
        pattern_min_candles=cfg.patterns.min_candles,
```

En la construcción de `muted` (warmup): los mismos dos argumentos.

Después de `monitor.trackers = muted.trackers` agregar:

```python
    monitor._pattern_buffers = muted._pattern_buffers  # idem buffers de patrones
```

- [ ] **Step 5: Suscribir los nuevos timeframes**

`BinanceCollector(cfg.symbols, cfg.timeframes, ...)` → `BinanceCollector(cfg.symbols, all_timeframes, ...)`.
`KlinePoller(cfg.symbols, cfg.timeframes, muted.on_candle)` → `KlinePoller(cfg.symbols, all_timeframes, muted.on_candle)`.
Y el loop de warmup `for tf in cfg.timeframes:` → `for tf in all_timeframes:`.

- [ ] **Step 6: Suite completa**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all passed.

---

### Task 7: Comando `/patterns` del bot

**Files:**
- Modify: `src/values_watcher/alerts/telegram_bot.py`
- Test: `tests/test_telegram_bot.py`

- [ ] **Step 1: Test que falla**

Agregar a `tests/test_telegram_bot.py`:

```python
async def test_patterns_toggle(store, db):
    bot, _, state = make_bot(store, db)
    assert state.get("patterns", True) is True or "patterns" not in state

    reply = await bot._dispatch("patterns", "off")
    assert "pausadas" in reply
    assert state["patterns"] is False

    reply = await bot._dispatch("patterns", "")
    assert "pausadas" in reply

    reply = await bot._dispatch("patterns", "on")
    assert "activadas" in reply
    assert state["patterns"] is True
```

- [ ] **Step 2: Verificar que falla**

Run: `.venv/bin/python -m pytest tests/test_telegram_bot.py::test_patterns_toggle -q`
Expected: FAIL (el comando cae en help/unknown).

- [ ] **Step 3: Implementar**

En `BOT_COMMANDS` agregar antes de `("help", ...)`:

```python
    ("patterns", "Patrones de velas: /patterns [on|off]"),
```

En `_dispatch`, junto a `pause`/`resume`:

```python
        if cmd == "patterns":
            arg = args.strip().lower()
            if arg == "on":
                self.alerts_state["patterns"] = True
                return "🕯 Notificaciones de patrones activadas."
            if arg == "off":
                self.alerts_state["patterns"] = False
                return "🔕 Notificaciones de patrones pausadas."
            estado = "activas" if self.alerts_state.get("patterns", True) else "pausadas"
            return f"Patrones: {estado}. Usá /patterns on u /patterns off."
```

En el comando `status`, cambiar el return por:

```python
            estado = "activas" if self.alerts_state.get("enabled") else "pausadas"
            pat = "activas" if self.alerts_state.get("patterns", True) else "pausadas"
            return f"values-watcher OK · notificaciones {estado} · patrones {pat}"
```

- [ ] **Step 4: Verificar que pasa**

Run: `.venv/bin/python -m pytest tests/test_telegram_bot.py -q`
Expected: all passed.

---

### Task 8: Verificación final

- [ ] **Step 1: Suite completa**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all passed (84+ tests).

- [ ] **Step 2: Smoke de arranque**

Run: `.venv/bin/python -c "from values_watcher.app import run; print('import ok')"`
Expected: `import ok` (verifica que el wiring de app.py no rompe imports).

- [ ] **Step 3: Reportar al usuario**

Listar qué se agregó y recordar que para activar: `alerts.enabled: true` y `pattern` ya está en `enabled_events`. Los commits quedan pendientes de su autorización.
