"""Collector de Kiyotaka: heatmap del libro de órdenes vía REST.

Flujo (según docs kiyotaka.ai):
1. GET /v1/block-sizes?exchange=BINANCE_FUTURES&rawSymbol=BTCUSDT → blockSize HD (×5)
2. GET /v1/points?type=BLOCK_BOOK_SNAPSHOT_AGG&...&blockSize=<HD> → puntos del heatmap

Auth: header X-Kiyotaka-Key. Sin key, el collector queda deshabilitado.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.kiyotaka.ai"


class KiyotakaCollector:
    def __init__(
        self,
        api_key: str,
        symbols: list[str],
        db,
        poll_seconds: int = 60,
        interval: str = "MINUTE",
        period: int = 1140,
        max_depth: int = 1000,
        block_size_target: int | None = None,
        large_order_thresholds: dict[str, float] | None = None,
        critical_multiplier: float = 3.0,
        order_blocks_enabled: bool = True,
        order_blocks_interval_minutes: int = 15,
        order_blocks_range_usd: float = 2000,
        order_blocks_min_volume: float = 300,
        on_event=None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.symbols = symbols
        self.db = db
        self.poll_seconds = poll_seconds
        self.interval = interval
        self.period = period
        self.max_depth = max_depth
        self.block_size_target = block_size_target
        self.large_order_thresholds = large_order_thresholds or {}
        self.critical_multiplier = critical_multiplier
        self.order_blocks_enabled = order_blocks_enabled
        self.order_blocks_interval_s = order_blocks_interval_minutes * 60
        self.order_blocks_range_usd = order_blocks_range_usd
        self.order_blocks_min_volume = order_blocks_min_volume
        self._last_summary: dict[str, float] = {}
        self.on_event = on_event
        self._http = http
        self._stop = asyncio.Event()
        self._block_sizes: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def stop(self) -> None:
        self._stop.set()

    def _client(self) -> httpx.AsyncClient:
        if self._http:
            return self._http
        return httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"X-Kiyotaka-Key": self.api_key},
            timeout=20.0,
        )

    async def get_block_size(self, client: httpx.AsyncClient, symbol: str) -> int:
        """Block size para el símbolo, con cache.

        Si hay block_size_target (p.ej. 25 USD), se usa el múltiplo del raw más
        cercano (raw=3 → 24). Si no, HD por defecto (raw × 5).
        """
        if symbol in self._block_sizes:
            return self._block_sizes[symbol]
        r = await client.get("/v1/block-sizes",
                             params={"exchange": "BINANCE_FUTURES", "rawSymbol": symbol})
        r.raise_for_status()
        raw = _extract_block_size(r.json())
        if raw <= 0:
            # Tier free sin datos para este símbolo (p.ej. ETH/SOL → blockSizes [0]):
            # lo marcamos como no soportado y no pedimos /points (evita 429).
            log.warning("Kiyotaka sin datos para %s (blockSizes=%s); símbolo omitido",
                        symbol, raw)
            self._block_sizes[symbol] = 0
            return 0
        if self.block_size_target:
            bs = max(raw, round(self.block_size_target / raw) * raw)
        else:
            bs = raw * 5
        self._block_sizes[symbol] = bs
        return bs

    async def fetch_heatmap(self, client: httpx.AsyncClient, symbol: str) -> dict | None:
        block_size = await self.get_block_size(client, symbol)
        if block_size == 0:
            return None  # símbolo no soportado por el tier
        r = await client.get("/v1/points", params={
            "type": "BLOCK_BOOK_SNAPSHOT_AGG",
            "exchange": "BINANCE_FUTURES",
            "rawSymbol": symbol,
            "interval": self.interval,
            "period": self.period,
            "blockSize": block_size,
            "maxDepth": self.max_depth,
            "sortDirection": "SORT_DIRECTION_DESC",
        })
        r.raise_for_status()
        return r.json()

    async def run(self) -> None:
        if not self.enabled:
            log.warning("Kiyotaka deshabilitado: falta KIYOTAKA_API_KEY")
            return
        owns = self._http is None
        client = self._client()
        try:
            while not self._stop.is_set():
                for symbol in self.symbols:
                    try:
                        heatmap = await self.fetch_heatmap(client, symbol)
                        if heatmap is None:
                            continue  # símbolo no soportado por el tier
                        await self.db.insert_heatmap(
                            symbol, int(time.time() * 1000), json.dumps(heatmap))
                        log.info("Heatmap %s guardado", symbol)
                        threshold = self.large_order_thresholds.get(symbol)
                        if threshold and self.on_event:
                            for block in find_large_blocks(
                                    heatmap, threshold, self.critical_multiplier):
                                log.info("BLOQUE GRANDE %s: %s", symbol, block)
                                await self.on_event("large_order", {
                                    "symbol": symbol, **block})
                        await self._maybe_emit_summary(client, symbol, heatmap)
                    except httpx.HTTPStatusError as e:
                        if e.response.status_code == 429:
                            log.warning("Kiyotaka 429 (cuota agotada); backoff 60s")
                            await asyncio.sleep(60)
                        else:
                            log.warning("Error Kiyotaka %s: %s", symbol, e)
                    except httpx.HTTPError as e:
                        log.warning("Error Kiyotaka %s: %s", symbol, e)
                    await asyncio.sleep(5)  # espaciar requests (rate limit del tier free)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            if owns:
                await client.aclose()

    async def _maybe_emit_summary(self, client: httpx.AsyncClient, symbol: str,
                                  heatmap: dict) -> None:
        """Emite el resumen order_blocks si pasó el intervalo configurado."""
        if not self.on_event or not self.order_blocks_enabled:
            return
        now = time.time()
        if now - self._last_summary.get(symbol, 0) < self.order_blocks_interval_s:
            return
        price = await _fetch_price(client, symbol)
        if price is None:
            return
        bids, asks = iter_blocks(heatmap)
        summary = build_order_blocks_summary(
            symbol, price, bids, asks,
            range_usd=self.order_blocks_range_usd,
            min_volume=self.order_blocks_min_volume,
        )
        self._last_summary[symbol] = now
        log.info("Resumen order_blocks %s emitido", symbol)
        await self.on_event("order_blocks", summary)


async def _fetch_price(client: httpx.AsyncClient, symbol: str) -> float | None:
    """Precio actual del símbolo vía ticker de Binance Futures."""
    try:
        r = await client.get("https://fapi.binance.com/fapi/v1/ticker/price",
                             params={"symbol": symbol})
        r.raise_for_status()
        return float(r.json()["price"])
    except (httpx.HTTPError, KeyError, ValueError) as e:
        log.warning("No se pudo obtener precio de %s: %s", symbol, e)
        return None


def _extract_block_size(data) -> int:
    """Extrae el block size raw de /v1/block-sizes.

    Forma real de la API: {"blockSizes": [3]}. También acepta variantes.
    """
    if isinstance(data, dict):
        for key in ("blockSize", "block_size", "size"):
            if key in data:
                return int(data[key])
        for key in ("blockSizes", "block_sizes", "data"):
            if key in data:
                return _extract_block_size(data[key])
    if isinstance(data, (list, tuple)) and data:
        first = data[0]
        if isinstance(first, (int, float)):
            return int(first)
        return _extract_block_size(first)
    raise ValueError(f"No se pudo extraer block size de: {data}")


def build_order_blocks_summary(
    symbol: str,
    price: float,
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    range_usd: float = 2000,
    min_volume: float = 300,
) -> dict:
    """Resumen de bloques grandes cerca del precio: soportes y resistencias.

    Devuelve {"symbol", "price", "text", "above", "below"} listo para notificar.
    """
    below = sorted(
        ((p, v) for p, v in bids if price - range_usd <= p <= price and v >= min_volume),
        reverse=True)
    above = sorted(
        ((p, v) for p, v in asks if price <= p <= price + range_usd and v >= min_volume),
        reverse=True)

    def fmt(levels):
        return ("\n".join(f"  {p:>10,.0f}  →  {v:,.1f} BTC" for p, v in levels)) or "  —"

    def fmt_k(v: float) -> str:
        return f"{v / 1000:,.1f}k" if v >= 1000 else f"{v:,.0f}"

    total_above = sum(v for p, v in asks if price <= p <= price + range_usd)
    total_below = sum(v for p, v in bids if price - range_usd <= p <= price)
    text = (
        f"🐋 Order blocks {symbol} (±{range_usd:,.0f} USD)\n\n"
        f"🔴 RESISTENCIA (asks ≥{min_volume:,.0f} BTC)\n{fmt(above)}\n\n"
        f"⚡ Precio actual: {price:,.1f}\n\n"
        f"🟢 SOPORTE (bids ≥{min_volume:,.0f} BTC)\n{fmt(below)}\n\n"
        f"Totales (todas las órdenes ±{range_usd:,.0f} USD): "
        f"{fmt_k(total_above)} BTC arriba / {fmt_k(total_below)} BTC abajo"
    )
    return {
        "symbol": symbol,
        "price": price,
        "text": text,
        "above": [{"price": p, "volume": round(v, 3)} for p, v in above],
        "below": [{"price": p, "volume": round(v, 3)} for p, v in below],
    }


def iter_blocks(heatmap: dict) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Extrae (bids, asks) del punto más reciente del heatmap.

    Estructura real: {"series": [{"points": [{"Point": {"bids": [p, q, p, q...],
    "asks": [...]}}]}]} con puntos ordenados del más nuevo al más viejo.
    """
    series = heatmap.get("series") or []
    if not series:
        return [], []
    points = series[0].get("points") or []
    if not points:
        return [], []
    point = points[0].get("Point", points[0])

    def pairs(flat) -> list[tuple[float, float]]:
        return [(float(flat[i]), float(flat[i + 1]))
                for i in range(0, len(flat) - 1, 2)]

    return pairs(point.get("bids") or []), pairs(point.get("asks") or [])


def find_large_blocks(heatmap: dict, threshold: float,
                      critical_multiplier: float = 3.0) -> list[dict]:
    """Bloques del snapshot más reciente con volumen >= threshold.

    Severidad: warning a partir del umbral, critical a partir de
    threshold × critical_multiplier (mientras más grande, más grave).
    """
    events = []
    bids, asks = iter_blocks(heatmap)
    for side, blocks in (("bid", bids), ("ask", asks)):
        for price, volume in blocks:
            if volume >= threshold:
                events.append({
                    "side": side,
                    "price": price,
                    "volume": round(volume, 3),
                    "threshold": threshold,
                    "severity": "critical" if volume >= threshold * critical_multiplier
                                else "warning",
                })
    return events


# Venues del reporte de flujo: (nombre, tipo, exchange, formato de símbolo).
# {base} = símbolo sin el sufijo USDT (BTCUSDT → BTC).
FLOW_VENUES = [
    ("Binance", "spot", "BINANCE", "{base}USDT"),
    ("Coinbase", "spot", "COINBASE", "{base}-USD"),
    ("Bybit", "spot", "BYBIT_SPOT", "{base}USDT"),
    ("OKX", "spot", "OKEX", "{base}-USDT"),
    ("Binance", "futures", "BINANCE_FUTURES", "{base}USDT"),
    ("Bybit", "futures", "BYBIT", "{base}USDT"),
    ("OKX", "futures", "OKEX_SWAP", "{base}-USDT-SWAP"),
]


async def fetch_trade_flow(client: httpx.AsyncClient, symbol: str,
                           minutes: int = 60,
                           spacing: float = 0.5) -> list[dict]:
    """Volumen taker buy/sell por venue (spot y futuros) vía TRADE_AGG.

    Devuelve una fila por venue: {"name", "kind" (spot|futures),
    "buy", "sell"} con volúmenes en unidades del activo. Venues sin
    datos en el tier devuelven buy=sell=0.
    """
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    now = int(time.time())
    period = minutes * 60
    rows: list[dict] = []
    for name, kind, exchange, fmt in FLOW_VENUES:
        for attempt in range(3):
            try:
                r = await client.get("/v1/points", params={
                    "type": "TRADE_AGG",
                    "exchange": exchange,
                    "rawSymbol": fmt.format(base=base),
                    "interval": "MINUTE",
                    "from": now - period,
                    "period": period,
                    "sortDirection": "SORT_DIRECTION_DESC",
                })
                r.raise_for_status()
                break
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt < 2:
                    # Cuota compartida con el heatmap: esperar y reintentar
                    await asyncio.sleep(max(spacing, 20))
                    continue
                raise
        buy = sell = 0.0
        for serie in r.json().get("series") or []:
            side = (serie.get("id") or {}).get("side")
            vol = sum(float((p.get("Point") or p).get("volume") or 0)
                      for p in serie.get("points") or [])
            if side == "BUY":
                buy += vol
            elif side == "SELL":
                sell += vol
        rows.append({"name": name, "kind": kind, "buy": buy, "sell": sell})
        if spacing:
            await asyncio.sleep(spacing)  # cuidar cuota del tier free
    return rows


def build_flow_summary(symbol: str, minutes: int, rows: list[dict]) -> str:
    """Texto del flujo spot vs futuros: quién compra y quién vende, por venue."""
    base = symbol[:-4] if symbol.endswith("USDT") else symbol

    def line(name: str, buy: float, sell: float) -> str:
        delta = buy - sell
        icon = "🟢" if delta > 0 else "🔴" if delta < 0 else "⚪"
        accion = "comprando" if delta > 0 else "vendiendo" if delta < 0 else "neutral"
        return (f"{icon} {name}: {accion} {abs(delta):,.1f} {base} netos"
                f" ({buy:,.1f}/{sell:,.1f})")

    sections = []
    for kind, title in (("spot", "SPOT"), ("futures", "FUTUROS")):
        venues = [r for r in rows if r["kind"] == kind]
        total_buy = sum(r["buy"] for r in venues)
        total_sell = sum(r["sell"] for r in venues)
        lines = [line(r["name"], r["buy"], r["sell"]) for r in venues]
        lines.append(line("TOTAL", total_buy, total_sell))
        sections.append(f"{title}\n" + "\n".join(lines))
    return (f"🌊 Flujo {symbol} (últimos {minutes} min, taker)\n\n"
            + "\n\n".join(sections))


# Venues de futuros para open interest y funding: (nombre, exchange, formato).
OI_VENUES = [
    ("Binance", "BINANCE_FUTURES", "{base}USDT"),
    ("Bybit", "BYBIT", "{base}USDT"),
    ("OKX", "OKEX_SWAP", "{base}-USDT-SWAP"),
]


async def fetch_open_interest(client: httpx.AsyncClient, symbol: str,
                              hours: int = 2,
                              spacing: float = 0.5) -> list[dict]:
    """Open interest y funding por venue de futuros.

    Devuelve una fila por venue: {"name", "oi_now", "oi_change_pct",
    "funding_pct" (por intervalo de funding, en %)}. oi en unidades
    del activo; None cuando el venue no tiene datos en el tier.
    """
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    now = int(time.time())
    period = hours * 3600

    async def points(tipo: str, exchange: str, raw: str) -> list[dict]:
        for attempt in range(3):
            try:
                r = await client.get("/v1/points", params={
                    "type": tipo, "exchange": exchange, "rawSymbol": raw,
                    "interval": "HOUR", "from": now - period, "period": period,
                    "sortDirection": "SORT_DIRECTION_DESC",
                })
                r.raise_for_status()
                break
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt < 2:
                    await asyncio.sleep(max(spacing, 20))
                    continue
                raise
        series = r.json().get("series") or []
        if not series:
            return []
        return [p.get("Point") or p for p in series[0].get("points") or []]

    rows: list[dict] = []
    for name, exchange, fmt in OI_VENUES:
        raw = fmt.format(base=base)
        oi_pts = await points("OPEN_INTEREST_AGG", exchange, raw)
        if spacing:
            await asyncio.sleep(spacing)
        fund_pts = await points("FUNDING_RATE_AGG", exchange, raw)
        if spacing:
            await asyncio.sleep(spacing)
        oi_now = float(oi_pts[0]["close"]) if oi_pts else None
        oi_change = None
        if oi_pts and len(oi_pts) > 1:
            first = float(oi_pts[-1]["close"])
            if first:
                oi_change = (float(oi_pts[0]["close"]) - first) / first * 100
        funding = (float(fund_pts[0]["rateClose"]) * 100) if fund_pts else None
        rows.append({"name": name, "oi_now": oi_now,
                     "oi_change_pct": oi_change, "funding_pct": funding})
    return rows


def build_oi_summary(symbol: str, hours: int, rows: list[dict]) -> str:
    """Texto de open interest + funding por venue."""
    base = symbol[:-4] if symbol.endswith("USDT") else symbol

    def fmt_funding(f: float | None) -> str:
        if f is None:
            return "s/d"
        icon = "🟢" if f > 0 else "🔴" if f < 0 else "⚪"
        return f"{icon} {f:+.4f}%"

    lines = []
    total = 0.0
    for r in rows:
        if r["oi_now"] is None:
            lines.append(f"⚪ {r['name']}: sin datos")
            continue
        total += r["oi_now"]
        change = f"{r['oi_change_pct']:+.2f}%" if r["oi_change_pct"] is not None else "—"
        arrow = "📈" if (r["oi_change_pct"] or 0) > 0 else "📉"
        lines.append(f"{arrow} {r['name']}: {r['oi_now']:,.0f} {base} ({change} en {hours}h)"
                     f" · funding {fmt_funding(r['funding_pct'])}")
    lines.append(f"TOTAL OI: {total:,.0f} {base}")
    return (f"📊 Open interest {symbol}\n\n" + "\n".join(lines)
            + "\n\nFunding +: longs pagan (sesgo long) · −: shorts pagan (sesgo short)")


def build_oi_summary_with_flow(symbol: str, hours: int, rows: list[dict],
                               flow_rows: list[dict],
                               flow_5m: list[dict] | None = None) -> str:
    """Texto de open interest + funding + tendencia spot/futuros."""
    base = symbol[:-4] if symbol.endswith("USDT") else symbol

    def fmt_funding(f: float | None) -> str:
        if f is None:
            return "s/d"
        icon = "🟢" if f > 0 else "🔴" if f < 0 else "⚪"
        return f"{icon} {f:+.4f}%"

    lines = []
    total = 0.0
    for r in rows:
        if r["oi_now"] is None:
            lines.append(f"⚪ {r['name']}: sin datos")
            continue
        total += r["oi_now"]
        change = f"{r['oi_change_pct']:+.2f}%" if r["oi_change_pct"] is not None else "—"
        arrow = "📈" if (r["oi_change_pct"] or 0) > 0 else "📉"
        lines.append(f"{arrow} {r['name']}: {r['oi_now']:,.0f} {base} ({change} en {hours}h)"
                     f" · funding {fmt_funding(r['funding_pct'])}")
    lines.append(f"TOTAL OI: {total:,.0f} {base}")

    def tendencia(buy: float, sell: float) -> tuple[str, str]:
        if buy + sell == 0:
            return "⚪", "sin datos"
        ratio = buy / (buy + sell)
        if ratio > 0.55:
            return "🟢", "COMPRANDO"
        elif ratio < 0.45:
            return "🔴", "VENDIENDO"
        return "🟡", "NEUTRO"

    def flow_section(flow: list[dict], minutes: int) -> list[str]:
        spot_buy = sum(r["buy"] for r in flow if r["kind"] == "spot")
        spot_sell = sum(r["sell"] for r in flow if r["kind"] == "spot")
        fut_buy = sum(r["buy"] for r in flow if r["kind"] == "futures")
        fut_sell = sum(r["sell"] for r in flow if r["kind"] == "futures")
        spot_icon, spot_txt = tendencia(spot_buy, spot_sell)
        fut_icon, fut_txt = tendencia(fut_buy, fut_sell)
        return [
            f"⏱ Últimos {minutes} min",
            f"{spot_icon} SPOT: {spot_txt} ({spot_buy:,.0f}/{spot_sell:,.0f} {base})",
            f"{fut_icon} FUTUROS: {fut_txt} ({fut_buy:,.0f}/{fut_sell:,.0f} {base})",
        ]

    sections = ["\n\n📈 Tendencia\n" + "\n".join(flow_section(flow_rows, 15))]
    if flow_5m:
        sections.append("\n".join(flow_section(flow_5m, 5)))

    return (f"📊 Open interest {symbol}\n\n" + "\n".join(lines)
            + "".join(sections)
            + "\n\nFunding +: longs pagan (sesgo long) · −: shorts pagan (sesgo short)")
