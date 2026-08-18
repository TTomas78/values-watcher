# values-watcher

Monitor de cripto en tiempo real: velas y libro de órdenes de Binance Futures, heatmap del libro vía Kiyotaka, detección de Fair Value Gaps (FVG) y alertas por HTTP a una API propia.

## Componentes

- **Collectors**
  - `collectors/binance_ws.py` — WebSocket público de Binance Futures: klines 5m/15m y depth20@100ms por símbolo, con reconexión automática.
  - `collectors/kiyotaka.py` — heatmap del order book vía REST de Kiyotaka (`/v1/points`, `BLOCK_BOOK_SNAPSHOT_AGG`). Requiere API key; sin key queda deshabilitado.
- **Core**
  - `core/fvg.py` — detección de FVG (patrón de 3 velas sin solape de mechas) con estados `open`/`mitigated`.
  - `core/orderbook.py` — detección de paredes de liquidez e imbalance bid/ask.
- **Alertas** — `alerts/`: POST JSON a tu API con API key, dedup por ventana de tiempo, retry con cola acotada.
- **API + dashboard** — FastAPI (`api/main.py`) + frontend estático (`frontend/`) con gráfico de velas, zonas FVG, order book e imbalance en vivo.
- **Storage** — SQLite (`data/values_watcher.db`): velas, FVGs, snapshots del libro, heatmaps y alertas.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env   # completar valores
```

Variables de entorno (`.env`):

| Variable | Descripción |
|---|---|
| `KIYOTAKA_API_KEY` | API key de kiyotaka.ai (opcional; sin key no hay heatmap) |
| `NOTIFY_API_URL` | URL de tu API de notificaciones |
| `NOTIFY_API_KEY` | API key para autenticarse |
| `NOTIFY_AUTH_HEADER` | Header de auth con `{key}` como placeholder. Default: `Authorization: Bearer {key}` |

Configuración de símbolos, timeframes y umbrales: `config.yaml`.

## Correr

```bash
.venv/bin/python -m values_watcher.app
```

Dashboard: http://127.0.0.1:8000

Endpoints: `/api/health`, `/api/candles`, `/api/fvgs`, `/api/orderbook/{symbol}`, `/api/heatmap/{symbol}`, `/api/liquidations/{symbol}`, `/api/alerts`, `/ws` (eventos en vivo).

## Eventos y reglas adicionales

Además de FVG, paredes e imbalance, el monitor genera estos eventos (configurables en `config.yaml`):

- **Liquidaciones** — stream `forceOrder` de Binance: alerta por liquidación individual grande (`min_alert_usd`) y clusters por bucket de precio (`bucket_usd`, ventana `window_hours`). Se consultan en `/api/liquidations/{symbol}`.
- **Reglas por activo** (`watchlist`) — cruces de precio (`price_target`, con escalera `price_ladder` para avisos por cada % adicional) y volúmenes de parada absolutos (`stop_volume`), editables en caliente desde el bot de Telegram.

Eventos completos: `fvg_new`, `fvg_mitigated`, `wall`, `imbalance`, `large_order`, `order_blocks`, `price_target`, `stop_volume`, `liquidation`, `price_ladder` (lista en `alerts.enabled_events`).

## Formato de alerta (POST a `NOTIFY_API_URL`)

Esquema de la API de notificaciones (Telegram):

```json
{
  "title": "[fvg_new] BTCUSDT",
  "service": "values-watcher",
  "severity": "info",
  "detail": "{\"symbol\": \"BTCUSDT\", \"timeframe\": \"5m\", \"direction\": \"bullish\", \"top\": 102.0, \"bottom\": 101.0}"
}
```

- Auth: header `x-api-key: <NOTIFY_API_KEY>` (configurable vía `NOTIFY_AUTH_HEADER`).
- Severidades: `fvg_new`/`fvg_mitigated` → `info`; `wall`/`imbalance` → `warning`.

Eventos: `fvg_new`, `fvg_mitigated`, `wall`, `imbalance`.

## Tests

```bash
.venv/bin/python -m pytest -q
```

## Bot de comandos Telegram (gestión de alertas)

Además de las notificaciones salientes, la app puede recibir comandos por Telegram
(long polling de la Bot API). Configurar `TELEGRAM_BOT_TOKEN` (crear bot con
@BotFather) y `TELEGRAM_CHAT_ID` (solo ese chat puede comandar) en `.env`.

Comandos:

- `/alertas` — lista la vigilancia actual
- `/alerta BTC 60000` — nivel simple (avisa una vez al cruzar)
- `/alerta BTC 60000 below 1%` — escalera: avisa al cruzar 60k y por cada 1% adicional abajo, sin spam
- `/alerta BTC 65000 above 1%` — escalera hacia arriba
- `/borrar BTC 60000` — quita reglas en ese nivel
- `/pausa` / `/reanudar` — pausa/reanuda el envío de notificaciones
- `/status` — estado del sistema

Los cambios se aplican en caliente (sin reiniciar) y se persisten en
`data/watch_overrides.json`.

## Nota sobre value gaps

TapeSurf no expone API pública; los FVG se calculan localmente desde velas de Binance (mismo concepto).
