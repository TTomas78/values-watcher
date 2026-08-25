"""Orquestador: collectors + monitor + alertas + API."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import uvicorn
import httpx

from values_watcher.alerts.client import AlertClient
from values_watcher.alerts.rules import AlertRules
from values_watcher.alerts.telegram_bot import TelegramCommandBot, WatchStore
from values_watcher.api.main import broadcaster, create_app
from values_watcher.collectors.binance_ws import BinanceCollector, KlinePoller
from values_watcher.collectors.kiyotaka import KiyotakaCollector
from values_watcher.config import load_config, load_settings
from values_watcher.monitor import LiveMonitor
from values_watcher.storage.db import Database

log = logging.getLogger("values_watcher")

# Eventos que se alertan por el bot con botón 🗑 para quitar la regla
BOT_ALERT_EVENTS = {"price_target", "price_ladder"}


async def run() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    settings = load_settings()

    pattern_tfs = cfg.patterns.timeframes if cfg.patterns.enabled else []
    all_timeframes = list(dict.fromkeys(cfg.timeframes + pattern_tfs))

    db_path = Path(cfg.storage.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(str(db_path))
    await db.connect()

    alert_client = AlertClient(
        url=settings.notify_api_url,
        api_key=settings.notify_api_key,
        auth_header=settings.notify_auth_header,
    )
    rules = AlertRules(alert_client, db,
                       cfg.alerts.enabled_events if cfg.alerts.enabled else [],
                       cfg.alerts.dedup_minutes)
    if not cfg.alerts.enabled:
        log.info("Alertas PAUSADAS (alerts.enabled=false en config.yaml)")
    alerts_state = {"enabled": cfg.alerts.enabled, "patterns": True}
    bot_holder: dict = {}

    async def on_event(event_type: str, payload: dict) -> None:
        await broadcaster.broadcast({"type": event_type, "payload": payload})
        if not alerts_state["enabled"]:
            return
        if event_type == "pattern" and not alerts_state.get("patterns", True):
            return
        bot = bot_holder.get("bot")
        if bot is not None and bot.enabled and event_type in BOT_ALERT_EVENTS:
            # alertas de precio: directo por el bot, con botón 🗑 para quitar
            if await rules.check_and_record(event_type, payload):
                await bot.send_alert(event_type, payload)
        else:
            await rules.handle(event_type, payload)

    monitor = LiveMonitor(
        cfg.symbols, cfg.timeframes, db,
        wall_multiplier=cfg.orderbook.wall_multiplier,
        imbalance_threshold=cfg.orderbook.imbalance_threshold,
        on_event=on_event,
        watch={s: w.model_dump() for s, w in cfg.watch.items()},
        liq_min_alert_usd=cfg.liquidations.min_alert_usd,
        liq_critical_multiplier=cfg.liquidations.critical_multiplier,
        pattern_timeframes=pattern_tfs,
        pattern_min_candles=cfg.patterns.min_candles,
        telegram_token=settings.telegram_bot_token,
        telegram_chat_id=settings.telegram_chat_id,
    )

    # Watchlist en caliente: overrides persistidos (comandos Telegram) pisan
    # o complementan la config estática. Las desactivadas (🔕) no se aplican.
    store = WatchStore()
    for symbol, rule in store.rules.items():
        dis_t = rule.get("disabled_targets", [])
        dis_l = rule.get("disabled_ladders", [])
        monitor.price_tracker.targets[symbol] = sorted(
            {t for t in rule.get("price_targets", []) if t not in dis_t}
            | set(monitor.price_tracker.targets.get(symbol, [])))
        monitor.ladder_tracker.ladders[symbol] = (
            [l for l in rule.get("price_ladders", []) if l not in dis_l]
            + monitor.ladder_tracker.ladders.get(symbol, []))
    bot = TelegramCommandBot(settings.telegram_bot_token, settings.telegram_chat_id,
                             store, monitor, alerts_state,
                             db=db, symbols=cfg.symbols,
                             ob_range_usd=cfg.kiyotaka.order_blocks_range_usd,
                             ob_min_volume=cfg.kiyotaka.order_blocks_min_volume,
                             kiyotaka_key=settings.kiyotaka_api_key,
                             ob_block_size_target=cfg.kiyotaka.block_size_target)
    bot_holder["bot"] = bot

    binance = BinanceCollector(cfg.symbols, all_timeframes,
                               monitor.on_candle, monitor.on_book,
                               on_liquidation=monitor.on_liquidation)
    if cfg.kiyotaka.enabled:
        kiyotaka = KiyotakaCollector(
            settings.kiyotaka_api_key, cfg.symbols, db,
            poll_seconds=cfg.kiyotaka.poll_seconds,
            interval=cfg.kiyotaka.interval,
            period=cfg.kiyotaka.period,
            max_depth=cfg.kiyotaka.max_depth,
            block_size_target=cfg.kiyotaka.block_size_target,
            large_order_thresholds=cfg.kiyotaka.large_order_thresholds,
            critical_multiplier=cfg.kiyotaka.critical_multiplier,
            order_blocks_enabled=cfg.kiyotaka.order_blocks_enabled,
            order_blocks_interval_minutes=cfg.kiyotaka.order_blocks_interval_minutes,
            order_blocks_range_usd=cfg.kiyotaka.order_blocks_range_usd,
            order_blocks_min_volume=cfg.kiyotaka.order_blocks_min_volume,
            on_event=on_event,
        )
        kiyotaka_coro = kiyotaka.run()
    else:
        log.info("Kiyotaka sin polling: solo consultas bajo demanda (comandos del bot)")
        kiyotaka_coro = asyncio.sleep(0)

    app = create_app(db)
    server = uvicorn.Server(uvicorn.Config(
        app, host=cfg.api.host, port=cfg.api.port, log_level="warning"))

    # Warmup: persistir velas cerradas recientes y precargar el estado FVG.
    # Los eventos de warmup NO disparan alertas (solo se detectan y guardan).
    muted = LiveMonitor(
        cfg.symbols, cfg.timeframes, db,
        wall_multiplier=cfg.orderbook.wall_multiplier,
        imbalance_threshold=cfg.orderbook.imbalance_threshold,
        on_event=lambda *_: asyncio.sleep(0),
        pattern_timeframes=pattern_tfs,
        pattern_min_candles=cfg.patterns.min_candles,
        telegram_token=settings.telegram_bot_token,
        telegram_chat_id=settings.telegram_chat_id,
    )
    warmup = KlinePoller(cfg.symbols, all_timeframes, muted.on_candle)
    async with httpx.AsyncClient(base_url="https://fapi.binance.com", timeout=15.0) as http:
        for symbol in cfg.symbols:
            for tf in all_timeframes:
                try:
                    # tfs de patrones necesitan historial suficiente para evaluar
                    limit = cfg.patterns.min_candles + 1 if tf in pattern_tfs else 5
                    for candle, volume in await warmup.fetch_closed(http, symbol, tf, limit=limit):
                        await muted.on_candle(symbol, tf, candle, volume)
                except httpx.HTTPError as e:
                    log.warning("Warmup %s %s falló: %s", symbol, tf, e)
    monitor.trackers = muted.trackers  # conservar el estado FVG precargado
    monitor._pattern_buffers = muted._pattern_buffers  # idem buffers de patrones

    await alert_client.start()
    log.info("values-watcher iniciado — dashboard: http://%s:%d",
             cfg.api.host, cfg.api.port)
    try:
        await asyncio.gather(
            binance.run(),
            kiyotaka_coro,
            bot.run(),
            server.serve(),
        )
    finally:
        bot.stop()
        await alert_client.stop()
        await db.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
