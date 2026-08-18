"""Verificación Fase 2: LiveMonitor en vivo; reporta FVGs y eventos detectados.

Uso: .venv/bin/python scripts/verify_phase2.py [segundos]
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from values_watcher.collectors.binance_ws import BinanceCollector
from values_watcher.config import load_config
from values_watcher.monitor import LiveMonitor
from values_watcher.storage.db import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("verify2")

event_counts: dict[str, int] = {}


async def main(seconds: int) -> None:
    cfg = load_config()
    db_path = Path("data/phase2.db")
    db_path.parent.mkdir(exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    db = Database(str(db_path))
    await db.connect()

    async def on_event(event_type, payload):
        event_counts[event_type] = event_counts.get(event_type, 0) + 1
        if event_type.startswith("fvg"):
            log.info("EVENTO %s: %s", event_type, payload)

    mon = LiveMonitor(
        cfg.symbols, cfg.timeframes, db,
        wall_multiplier=cfg.orderbook.wall_multiplier,
        imbalance_threshold=cfg.orderbook.imbalance_threshold,
        on_event=on_event,
    )
    collector = BinanceCollector(cfg.symbols, cfg.timeframes, mon.on_candle, mon.on_book)
    task = asyncio.create_task(collector.run())
    await asyncio.sleep(seconds)
    collector.stop()
    await asyncio.sleep(1)
    task.cancel()

    cur = await db.conn.execute("SELECT COUNT(*) FROM candles")
    candles = (await cur.fetchone())[0]
    cur = await db.conn.execute("SELECT COUNT(*) FROM fvgs")
    fvgs = (await cur.fetchone())[0]
    await db.close()

    print("\n=== RESULTADO FASE 2 ===")
    print(f"Velas cerradas en DB: {candles}")
    print(f"FVGs en DB: {fvgs}")
    print(f"Eventos por tipo: {event_counts}")
    ok = candles > 0
    print("VERIFICACIÓN:", "OK" if ok else "FALLÓ (sin velas cerradas en la ventana)")


if __name__ == "__main__":
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    asyncio.run(main(secs))
