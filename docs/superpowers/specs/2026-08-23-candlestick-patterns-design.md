# Detección de patrones de velas con TA-Lib — Diseño

Fecha: 2026-08-23
Estado: aprobado por el usuario (enfoque 1, subset A, canal notify estándar, timeframes 1h/4h/1d)

## Objetivo

Detectar patrones de velas clásicos de reversión sobre velas **cerradas** de 1h, 4h y 1d,
y alertarlos por el canal de notificaciones estándar, reutilizando la infraestructura
existente de eventos y dedup.

## Decisiones tomadas

- **Librería:** TA-Lib (requiere la lib C del sistema: `brew install ta-lib` en macOS).
- **Timeframes:** 1h, 4h, 1d — configurados en una sección `patterns:` propia, separada
  de `timeframes` (que queda en 5m/15m para FVGs). No se activan FVGs en los nuevos tfs.
- **Patrones (subset A):** engulfing, hammer, hanging man, morning star, evening star,
  doji, three white soldiers, three black crows.
- **Canal de alerta:** evento `pattern` por `AlertRules`/`AlertClient` (notify API),
  con dedup de 30 min. No va por el bot de Telegram ni lleva botón 🗑.
- **Toggle por Telegram:** comando `/patterns on|off` que pausa/reanuda solo las
  notificaciones de patrones, igual que `/pause` y `/resume` lo hacen globalmente.

## Config (`config.yaml`)

```yaml
patterns:
  enabled: true
  timeframes: [1h, 4h, 1d]
  min_candles: 50   # historial mínimo requerido para evaluar
```

`pattern` se agrega a `alerts.enabled_events`.

## Componentes

### `src/values_watcher/core/patterns.py` (nuevo)

Funciones puras, sin I/O ni estado:

```python
PATTERNS: dict[str, Callable]  # nombre -> función TA-Lib

def detect_patterns(symbol: str, timeframe: str, candles: list[Candle]) -> list[dict]
```

- Evalúa únicamente la **última vela** del array (TA-Lib devuelve la señal por vela;
  se toma el último valor != 0).
- Devuelve `[{"name": ..., "direction": "bullish"|"bearish"|"neutral", "score": int}]`.
- Requiere `len(candles) >= min_candles`; si no, devuelve `[]`.

Mapa TA-Lib: `CDLENGULFING`, `CDLHAMMER`, `CDLHANGINGMAN`, `CDLMORNINGSTAR`,
`CDLEVENINGSTAR`, `CDLDOJI`, `CDL3WHITESOLDIERS`, `CDL3BLACKCROWS`.
Dirección: score > 0 → bullish, < 0 → bearish, doji → neutral.

### `config.py`

Nuevo modelo `PatternsConfig(enabled, timeframes, min_candles)` colgado de `Config`.

### `monitor.py`

En `on_candle`, cuando la vela **cierra** y su timeframe está en `pattern_timeframes`:
mantiene un buffer de las últimas `min_candles` velas cerradas por (symbol, tf),
corre `detect_patterns` y por cada hallazgo emite:

```python
await self.on_event("pattern", {
    "symbol": symbol, "timeframe": tf, "pattern": name,
    "direction": direction, "close": candle.close, "open_time": candle.open_time,
})
```

### `app.py`

- El `BinanceCollector` y el warmup suscriben la unión de `cfg.timeframes` y
  `cfg.patterns.timeframes` (deduplicada, orden estable).
- El monitor recibe `pattern_timeframes=cfg.patterns.timeframes` si `patterns.enabled`,
  si no, lista vacía.
- El monitor de warmup (muted) recibe los mismos tfs: el warmup precarga el buffer
  sin emitir alertas (igual que con FVGs).

### `rules.py`

- `dedup_key("pattern", payload)` → `pattern:{symbol}:{timeframe}:{pattern}:{open_time}`.
  Cada ocurrencia alerta una sola vez; el mismo patrón en otra vela vuelve a avisar.
- `build_payload`: severidad `info` para `pattern` (default actual).

### Toggle por Telegram (`app.py` + `telegram_bot.py`)

- `app.py`: `alerts_state` gana la clave `"patterns": True`. En `on_event`, los eventos
  `pattern` se descartan si `alerts_state.get("patterns", True)` es `False` (además del
  chequeo global `"enabled"` ya existente). El broadcast al dashboard sigue ocurriendo:
  el toggle solo corta la notificación.
- `telegram_bot.py`: comando `/patterns on|off` (sin args → estado actual) que muta
  `alerts_state["patterns"]`, igual en espíritu a `/pause` y `/resume` (runtime only,
  no persiste entre reinicios, igual que ellos). `/status` reporta también
  "patrones activas/pausadas".

## Manejo de errores

- Si TA-Lib no está instalado, el import falla al arrancar con mensaje claro
  (dependencia declarada en `pyproject.toml`).
- Historial insuficiente → `detect_patterns` devuelve `[]` silenciosamente.

## Tests (`tests/test_patterns.py`, TDD)

1. Engulfing alcista sintético detectado con dirección bullish.
2. Engulfing bajista sintético detectado con dirección bearish.
3. Velas planas/aleatorias → sin detecciones.
4. Historial < min_candles → `[]`.
5. Monitor emite `pattern` solo en tfs configurados y solo al cierre de vela.
6. `dedup_key("pattern", ...)` estable.
7. `/patterns off` → `on_event` no envía notificaciones `pattern`; `/patterns on` las reanuda.
8. Suite completa en verde.

## Fuera de alcance (YAGNI)

- Persistir patrones en DB.
- Mostrar patrones en el dashboard / API.
- Persistir el estado de `/patterns` entre reinicios (runtime only, como `/pause`).
- Filtros por dirección o por patrón en config.
- Más patrones de TA-Lib.
