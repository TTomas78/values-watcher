"""Bot de comandos por Telegram (Bot API, long polling).

Comandos (registrados con setMyCommands al iniciar):

  /setalert BTC 60000            → nivel simple (avisa una vez al cruzarlo)
  /setalert BTC 60000 below 1%   → escalera: nivel + pasos % (sin spam)
  /setalert BTC 65000 above 1%   → escalera hacia arriba
  /delalert BTC 60000            → quita reglas en ese nivel
  /alerts                        → vigilancia actual
  /orderblocks [BTC]             → order blocks de Kiyotaka (±2000 USD, ≥300 BTC)
  /flujo [BTC] [min]             → flujo taker spot vs futuros por exchange
  /oi [BTC] [horas]              → open interest y funding por exchange
  /fvgs [BTC]                    → últimos fair value gaps detectados
  /liquidaciones [BTC]           → clusters de liquidaciones 24h
  /precio                        → precio actual de todos los símbolos
  /pause                         → pausa notificaciones
  /resume                        → reanuda notificaciones
  /status                        → estado del sistema
  /help                          → ayuda

Solo responde al chat id autorizado (TELEGRAM_CHAT_ID). Los cambios de
vigilancia se aplican en caliente y persisten en data/watch_overrides.json.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx

from values_watcher.collectors.kiyotaka import (
    build_oi_summary_with_flow,
    KiyotakaCollector, build_flow_summary, build_oi_summary,
    build_order_blocks_summary, fetch_open_interest, fetch_trade_flow,
    iter_blocks)

log = logging.getLogger(__name__)

API = "https://api.telegram.org"

BOT_COMMANDS = [
    ("setalert", "Alerta: /setalert BTC 60000 [below|above 1%]"),
    ("delalert", "Quitar: /delalert BTC 60000"),
    ("alerts", "Ver vigilancia actual"),
    ("orderblocks", "Order blocks Kiyotaka: /orderblocks [BTC]"),
    ("flujo", "Spot vs futuros: /flujo [BTC] [min]"),
    ("oi", "Open interest y funding: /oi [BTC] [horas]"),
    ("fvgs", "Últimos FVGs: /fvgs [BTC]"),
    ("liquidaciones", "Clusters de liquidaciones: /liquidaciones [BTC]"),
    ("precio", "Precio actual de los símbolos"),
    ("pause", "Pausar notificaciones"),
    ("resume", "Reanudar notificaciones"),
    ("patterns", "Patrones de velas: /patterns [on|off]"),
    ("status", "Estado del sistema"),
    ("help", "Ayuda"),
]


def parse_symbol(arg: str, default: str = "BTCUSDT") -> str:
    arg = arg.strip().upper()
    if not arg:
        return default
    return arg if arg.endswith("USDT") else arg + "USDT"


def parse_alert_command(args: str) -> dict | None:
    """'BTC 60000 [below|above 1%]' → spec de nivel o escalera."""
    m = re.match(r"^\s*([A-Za-z]+)\s+([\d.]+)\s*(?:(below|above)\s+([\d.]+)\s*%?\s*)?$",
                 args.strip())
    if not m:
        return None
    symbol = m.group(1).upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    level = float(m.group(2))
    if m.group(3):
        return {"symbol": symbol, "type": "ladder", "level": level,
                "direction": m.group(3), "step_pct": float(m.group(4))}
    return {"symbol": symbol, "type": "target", "level": level}


class WatchStore:
    """Vigilancia en caliente: estado mutable + persistencia en JSON."""

    def __init__(self, path: str = "data/watch_overrides.json") -> None:
        self.path = path
        self.rules: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path) as f:
                self.rules = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.rules = {}

    def save(self) -> None:
        with open(self.path, "w") as f:
            json.dump(self.rules, f, indent=2)

    def add(self, spec: dict) -> None:
        symbol = spec["symbol"]
        rule = self.rules.setdefault(symbol, {"price_targets": [], "price_ladders": []})
        if spec["type"] == "target":
            if spec["level"] not in rule["price_targets"]:
                rule["price_targets"].append(spec["level"])
            # reactivar si estaba desactivada
            if spec["level"] in rule.get("disabled_targets", []):
                rule["disabled_targets"].remove(spec["level"])
        else:
            ladder = {"level": spec["level"], "step_pct": spec["step_pct"],
                      "direction": spec["direction"]}
            if ladder not in rule["price_ladders"]:
                rule["price_ladders"].append(ladder)
            rule["disabled_ladders"] = [l for l in rule.get("disabled_ladders", [])
                                        if l != ladder]
        self.save()

    def set_enabled(self, symbol: str, level: float, enabled: bool) -> int:
        """(Des)activa reglas en un nivel. Devuelve cuántas reglas tocó."""
        rule = self.rules.get(symbol)
        if not rule:
            return 0
        n = 0
        dis_t = rule.setdefault("disabled_targets", [])
        if level in rule.get("price_targets", []):
            if not enabled and level not in dis_t:
                dis_t.append(level)
            if enabled and level in dis_t:
                dis_t.remove(level)
            n += 1
        dis_l = rule.setdefault("disabled_ladders", [])
        for ladder in rule.get("price_ladders", []):
            if float(ladder["level"]) == level:
                if not enabled and ladder not in dis_l:
                    dis_l.append(ladder)
                if enabled and ladder in dis_l:
                    dis_l.remove(ladder)
                n += 1
        if n:
            self.save()
        return n

    def remove(self, symbol: str, level: float) -> int:
        rule = self.rules.get(symbol)
        if not rule:
            return 0
        n = 0
        if level in rule.get("price_targets", []):
            rule["price_targets"].remove(level)
            n += 1
        before = len(rule.get("price_ladders", []))
        rule["price_ladders"] = [l for l in rule.get("price_ladders", [])
                                 if float(l["level"]) != level]
        n += before - len(rule["price_ladders"])
        if n:
            self.save()
        return n

    def describe(self) -> str:
        if not self.rules:
            return "Sin reglas de vigilancia configuradas."
        lines = []
        for symbol, rule in sorted(self.rules.items()):
            lines.append(f"📌 {symbol}")
            dis_t = rule.get("disabled_targets", [])
            dis_l = rule.get("disabled_ladders", [])
            for t in rule.get("price_targets", []):
                marca = " 🔕" if t in dis_t else ""
                lines.append(f"  • nivel simple: {t:,.0f}{marca}")
            for l in rule.get("price_ladders", []):
                marca = " 🔕" if l in dis_l else ""
                lines.append(f"  • escalera: {l['level']:,.0f} "
                             f"{l['direction']} {l['step_pct']}%{marca}")
            if rule.get("stop_volume"):
                lines.append(f"  • stop_volume: {rule['stop_volume']:,}")
        if any(rule.get("disabled_targets") or rule.get("disabled_ladders")
               for rule in self.rules.values()):
            lines.append("(🔕 = desactivada; se reactiva con /setalert del mismo nivel)")
        return "\n".join(lines)


class TelegramCommandBot:
    def __init__(
        self,
        token: str,
        chat_id: str,
        store: WatchStore,
        monitor,
        alerts_state: dict,
        db=None,
        symbols: list[str] | None = None,
        ob_range_usd: float = 2000,
        ob_min_volume: float = 300,
        kiyotaka_key: str = "",
        ob_block_size_target: int | None = 25,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.store = store
        self.monitor = monitor
        self.alerts_state = alerts_state  # {"enabled": bool} compartido con app
        self.db = db
        self.symbols = symbols or []
        self.ob_range_usd = ob_range_usd
        self.ob_min_volume = ob_min_volume
        self.kiyotaka_key = kiyotaka_key
        self.ob_block_size_target = ob_block_size_target
        self._http = http
        self._stop = asyncio.Event()
        self._offset = 0

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def stop(self) -> None:
        self._stop.set()

    def _client(self) -> httpx.AsyncClient:
        return self._http or httpx.AsyncClient(base_url=f"{API}/bot{self.token}",
                                               timeout=35.0)

    async def run(self) -> None:
        if not self.enabled:
            log.info("Bot de comandos Telegram deshabilitado (sin token/chat id)")
            return
        owns = self._http is None
        client = self._client()
        try:
            await client.post("/setMyCommands", json={
                "commands": [{"command": c, "description": d}
                             for c, d in BOT_COMMANDS]})
            log.info("Bot de comandos Telegram activo (chat %s)", self.chat_id)
            while not self._stop.is_set():
                try:
                    r = await client.get("/getUpdates", params={
                        "offset": self._offset, "timeout": 25})
                    r.raise_for_status()
                    for upd in r.json().get("result", []):
                        self._offset = upd["update_id"] + 1
                        await self._handle(client, upd)
                except httpx.HTTPError as e:
                    log.warning("Bot Telegram: %s", e)
                    await asyncio.sleep(5)
        finally:
            if owns:
                await client.aclose()

    async def _handle(self, client: httpx.AsyncClient, upd: dict) -> None:
        # Botones inline (callback_query): quitar reglas desde la alerta
        cb = upd.get("callback_query")
        if cb:
            await self._handle_callback(client, cb)
            return
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat = str((msg.get("chat") or {}).get("id", ""))
        if not text or chat != self.chat_id:
            return
        cmd, _, args = text.partition(" ")
        cmd = cmd.lower().lstrip("/").split("@")[0]  # /cmd@botname → /cmd
        reply = await self._dispatch(cmd, args)
        await client.post("/sendMessage", json={"chat_id": chat, "text": reply})

    async def _handle_callback(self, client: httpx.AsyncClient, cb: dict) -> None:
        chat = str((cb.get("message") or {}).get("chat", {}).get("id", ""))
        data = cb.get("data", "")
        if chat != self.chat_id:
            return
        if data.startswith("delrule:"):
            _, symbol, level = data.split(":", 2)
            n = self.store.set_enabled(symbol, float(level), enabled=False)
            self._apply_to_monitor(symbol)
            text = (f"🔕 Alerta desactivada: {symbol} {float(level):,.0f}"
                    if n else "Esa regla ya no existía.")
            await client.post("/answerCallbackQuery",
                              json={"callback_query_id": cb["id"], "text": text})
            orig = (cb.get("message") or {}).get("text", "")
            await client.post("/editMessageText", json={
                "chat_id": chat, "message_id": cb["message"]["message_id"],
                "text": f"{orig}\n\n🔕 ALERTA DESACTIVADA"})

    async def send_alert(self, event_type: str, payload: dict) -> None:
        """Manda una alerta por el bot con botón 🗑 para quitar la regla.

        Aplica a price_target y price_ladder: el botón borra el nivel/escalera.
        """
        if not self.enabled:
            return
        if event_type == "price_target":
            level = payload["target"]
            text = (f"🎯 {payload['symbol']} cruzó {level:,.0f} "
                    f"({payload['crossed']}) — precio {payload['price']:,.2f}")
        elif event_type == "price_ladder":
            level = payload["level"]
            text = (f"🪜 {payload['symbol']} escalón {payload['step']}: "
                    f"{payload['threshold']:,.0f} {payload['direction']} "
                    f"(nivel {level:,.0f}) — precio {payload['price']:,.2f}")
        else:
            return
        client = self._client()
        owns = self._http is None
        try:
            await client.post("/sendMessage", json={
                "chat_id": self.chat_id,
                "text": text,
                "reply_markup": {"inline_keyboard": [[{
                    "text": "🔕 Desactivar alerta",
                    "callback_data": f"delrule:{payload['symbol']}:{level}",
                }]]},
            })
        finally:
            if owns:
                await client.aclose()

    async def _dispatch(self, cmd: str, args: str) -> str:
        if cmd == "setalert":
            spec = parse_alert_command(args)
            if not spec:
                return "Formato: /setalert BTC 60000  ó  /setalert BTC 60000 below 1%"
            self.store.add(spec)
            self._apply_to_monitor(spec["symbol"])
            if spec["type"] == "target":
                return f"✅ Nivel agregado: {spec['symbol']} {spec['level']:,.0f}"
            return (f"✅ Escalera agregada: {spec['symbol']} {spec['level']:,.0f} "
                    f"{spec['direction']} cada {spec['step_pct']}%")
        if cmd == "delalert":
            spec = parse_alert_command(args)
            if not spec:
                return "Formato: /delalert BTC 60000"
            n = self.store.remove(spec["symbol"], spec["level"])
            self._apply_to_monitor(spec["symbol"])
            return (f"🗑 Eliminadas {n} regla(s) en {spec['symbol']} {spec['level']:,.0f}"
                    if n else "No había reglas en ese nivel.")
        if cmd == "alerts":
            return self.store.describe()
        if cmd == "pause":
            self.alerts_state["enabled"] = False
            return "⏸ Notificaciones pausadas."
        if cmd == "resume":
            self.alerts_state["enabled"] = True
            return "▶️ Notificaciones reanudadas."
        if cmd == "patterns":
            arg = args.strip().lower()
            if arg == "on":
                self.alerts_state["patterns"] = True
                return "🕯 Notificaciones de patrones activadas."
            if arg == "off":
                self.alerts_state["patterns"] = False
                return "🔕 Notificaciones de patrones pausadas."
            pat = "activas" if self.alerts_state.get("patterns", True) else "pausadas"
            return f"Patrones: {pat}. Usá /patterns on u /patterns off."
        if cmd == "status":
            estado = "activas" if self.alerts_state.get("enabled") else "pausadas"
            pat = "activas" if self.alerts_state.get("patterns", True) else "pausadas"
            return f"values-watcher OK · notificaciones {estado} · patrones {pat}"
        if cmd == "orderblocks":
            return await self._orderblocks(args)
        if cmd == "flujo":
            return await self._flujo(args)
        if cmd == "oi":
            return await self._oi(args)
        if cmd == "fvgs":
            return await self._fvgs(args)
        if cmd == "liquidaciones":
            return await self._liquidaciones(args)
        if cmd == "precio":
            return await self._precio()
        if cmd in ("start", "help", "ayuda"):
            return ("Comandos:\n"
                    "/setalert BTC 60000 — nivel simple\n"
                    "/setalert BTC 60000 below 1% — escalera (1 alerta por escalón)\n"
                    "/delalert BTC 60000 — quitar reglas\n"
                    "/alerts — vigilancia actual\n"
                    "/orderblocks [BTC] — order blocks Kiyotaka\n"
                    "/flujo [BTC] [min] — spot vs futuros (comprando/vendiendo)\n"
                    "/oi [BTC] [horas] — open interest y funding\n"
                    "/fvgs [BTC] — últimos fair value gaps\n"
                    "/liquidaciones [BTC] — clusters 24h\n"
                    "/precio — precios actuales\n"
                    "/pause / /resume — notificaciones\n"
                    "/status — estado")
        return "Comando no reconocido. Probá /help."

    async def _flujo(self, args: str) -> str:
        parts = args.split()
        symbol = parse_symbol(parts[0]) if parts else "BTCUSDT"
        minutes = 60
        if len(parts) > 1:
            try:
                minutes = max(5, min(1440, int(parts[1])))
            except ValueError:
                return "Formato: /flujo [BTC] [minutos]"
        if not self.kiyotaka_key:
            return "Kiyotaka sin API key configurada."
        owns = self._http is None
        client = self._http or httpx.AsyncClient(
            base_url="https://api.kiyotaka.ai",
            headers={"X-Kiyotaka-Key": self.kiyotaka_key}, timeout=20.0)
        try:
            flow = await fetch_trade_flow(client, symbol, minutes,
                                          spacing=0 if self._http else 0.5)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                return ("⏳ Kiyotaka con la cuota ocupada (el heatmap consume la "
                        "cuota free). Reintentá en 1–2 minutos.")
            return f"Error Kiyotaka: {e}"
        except httpx.HTTPError as e:
            return f"Error Kiyotaka: {e}"
        finally:
            if owns:
                await client.aclose()
        return build_flow_summary(symbol, minutes, flow)

    async def _oi(self, args: str) -> str:
        parts = args.split()
        symbol = parse_symbol(parts[0]) if parts else "BTCUSDT"
        hours = 2
        if len(parts) > 1:
            try:
                hours = max(1, min(72, int(parts[1])))
            except ValueError:
                return "Formato: /oi [BTC] [horas]"
        if not self.kiyotaka_key:
            return "Kiyotaka sin API key configurada."
        owns = self._http is None
        client = self._http or httpx.AsyncClient(
            base_url="https://api.kiyotaka.ai",
            headers={"X-Kiyotaka-Key": self.kiyotaka_key}, timeout=20.0)
        try:
            rows = await fetch_open_interest(client, symbol, hours,
                                             spacing=0 if self._http else 0.5)
            flow_15m = await fetch_trade_flow(client, symbol, minutes=15,
                                              spacing=0 if self._http else 0.5)
            flow_5m = await fetch_trade_flow(client, symbol, minutes=5,
                                             spacing=0 if self._http else 0.5)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                return ("⏳ Kiyotaka con la cuota ocupada. "
                        "Reintentá en 1–2 minutos.")
            return f"Error Kiyotaka: {e}"
        except httpx.HTTPError as e:
            return f"Error Kiyotaka: {e}"
        finally:
            if owns:
                await client.aclose()
        return build_oi_summary_with_flow(symbol, hours, rows, flow_15m, flow_5m)

    async def _orderblocks(self, args: str) -> str:
        symbol = parse_symbol(args)
        price = await self._fetch_price(symbol)
        if price is None:
            return "No pude obtener el precio actual."
        bids = asks = None
        if self.kiyotaka_key:
            owns = self._http is None
            client = self._http or httpx.AsyncClient(
                base_url="https://api.kiyotaka.ai",
                headers={"X-Kiyotaka-Key": self.kiyotaka_key}, timeout=20.0)
            try:
                collector = KiyotakaCollector(
                    self.kiyotaka_key, [symbol], None,
                    block_size_target=self.ob_block_size_target, http=client)
                heatmap = await collector.fetch_heatmap(client, symbol)
                if heatmap:
                    bids, asks = iter_blocks(heatmap)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    return ("⏳ Kiyotaka con la cuota agotada. "
                            "Reintentá en unos minutos.")
                log.warning("orderblocks %s: %s", symbol, e)
            except httpx.HTTPError as e:
                log.warning("orderblocks %s: %s", symbol, e)
            finally:
                if owns:
                    await client.aclose()
        if bids is None:  # fallback: último snapshot guardado en DB
            if self.db is None:
                return "Sin acceso a datos."
            cur = await self.db.conn.execute(
                "SELECT data FROM heatmaps WHERE symbol=?"
                " ORDER BY timestamp DESC LIMIT 1", (symbol,))
            row = await cur.fetchone()
            if not row:
                return f"Sin heatmap de {symbol} (Kiyotaka sin datos o cuota agotada)."
            bids, asks = iter_blocks(json.loads(row[0]))
        return build_order_blocks_summary(symbol, price, bids, asks,
                                          self.ob_range_usd, self.ob_min_volume)["text"]

    async def _fvgs(self, args: str) -> str:
        symbol = parse_symbol(args)
        if self.db is None:
            return "Sin acceso a datos."
        cur = await self.db.conn.execute(
            "SELECT timeframe, direction, top, bottom, status FROM fvgs"
            " WHERE symbol=? ORDER BY formed_at DESC LIMIT 8", (symbol,))
        rows = await cur.fetchall()
        if not rows:
            return f"Sin FVGs detectados en {symbol} todavía."
        lines = [f"📐 Últimos FVGs {symbol}"]
        for tf, direction, top, bottom, status in rows:
            icon = "🟢" if direction == "bullish" else "🔴"
            estado = "abierto" if status == "open" else "mitigado"
            lines.append(f"{icon} {tf} {bottom:,.1f}–{top:,.1f} ({estado})")
        return "\n".join(lines)

    async def _liquidaciones(self, args: str) -> str:
        import time
        symbol = parse_symbol(args)
        if self.db is None:
            return "Sin acceso a datos."
        since = int(time.time() * 1000) - 24 * 3600 * 1000
        clusters = await self.db.liquidation_clusters(symbol, since, 50)
        if not clusters:
            return f"Sin liquidaciones en {symbol} en las últimas 24h."
        total = sum(c["usd"] for c in clusters)
        lines = [f"💥 Liquidaciones {symbol} 24h — total ${total/1e6:.1f}M"]
        for c in clusters[:8]:
            icon = "🔻" if c["side"] == "long" else "🔺"
            lines.append(f"{icon} ${c['bucket']:,.0f} → ${c['usd']/1e6:.2f}M ({c['count']})")
        return "\n".join(lines)

    async def _precio(self) -> str:
        lines = []
        for symbol in self.symbols or ["BTCUSDT"]:
            price = await self._fetch_price(symbol)
            lines.append(f"{symbol}: {price:,.2f}" if price else f"{symbol}: sin datos")
        return "\n".join(lines) or "Sin símbolos configurados."

    async def _fetch_price(self, symbol: str) -> float | None:
        try:
            client = self._client()
            r = await client.get("https://fapi.binance.com/fapi/v1/ticker/price",
                                 params={"symbol": symbol})
            r.raise_for_status()
            return float(r.json()["price"])
        except Exception as e:
            log.warning("Precio %s: %s", symbol, e)
            return None

    def _apply_to_monitor(self, symbol: str) -> None:
        """Sincroniza las reglas ACTIVAS persistidas con los trackers del monitor."""
        rule = self.store.rules.get(symbol, {})
        dis_t = rule.get("disabled_targets", [])
        dis_l = rule.get("disabled_ladders", [])
        self.monitor.price_tracker.targets[symbol] = sorted(
            {t for t in rule.get("price_targets", []) if t not in dis_t})
        self.monitor.ladder_tracker.ladders[symbol] = [
            l for l in rule.get("price_ladders", []) if l not in dis_l]
