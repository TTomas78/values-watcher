"""Verificación Fase 1: corre el collector de Binance N segundos y reporta la DB.

Uso: .venv/bin/python scripts/verify_phase1.py [segundos]
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from values_watcher.collectors.binance_ws import BinanceCollector
from values_watcher.config import load_config
from values_watcher.storage.db import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("verify")

counts = {"candles": 0, "snapshots": 0}


async def main(seconds: int) -> None:
    cfg = load_config()
    db_path = Path("data/values_watcher.db")
    db_path.parent.mkdir(exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    db = Database(str(db_path))
    await db.connect()

    async def on_candle(symbol, tf, candle, volume):
        counts["candles"] += 1
        log.info("VELA %s %s close=%.2f", symbol, tf, candle.close)
        await db.insert_candle(symbol, tf, candle, volume)

    async def on_book(book):
        counts["snapshots"] += 1
        if counts["snapshots"] % 50 == 0:  # guardar 1 de cada 50 para no llenar la DB
            await db.insert_snapshot(
                book.symbol, book.timestamp,
                json.dumps(book.bids), json.dumps(book.asks),
            )

    collector = BinanceCollector(cfg.symbols, cfg.timeframes, on_candle, on_book)
    task = asyncio.create_task(collector.run())
    await asyncio.sleep(seconds)
    collector.stop()
    await asyncio.sleep(1)
    task.cancel()

    cur = await db.conn.execute("SELECT COUNT(*) FROM candles")
    candles_db = (await cur.fetchone())[0]
    cur = await db.conn.execute("SELECT COUNT(*) FROM book_snapshots")
    snaps_db = (await cur.fetchone())[0]
    cur = await db.conn.execute(
        "SELECT symbol, COUNT(*) FROM book_snapshots GROUP BY symbol")
    per_symbol = await cur.fetchall()
    await db.close()

    print("\n=== RESULTADO FASE 1 ===")
    print(f"Velas cerradas recibidas: {counts['candles']} (en DB: {candles_db})")
    print(f"Snapshots recibidos: {counts['snapshots']} (guardados: {snaps_db})")
    print(f"Snapshots por símbolo: {per_symbol}")
    ok = counts["snapshots"] > 0 and len(per_symbol) >= 1
    print("VERIFICACIÓN:", "OK" if ok else "FALLÓ")


if __name__ == "__main__":
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    asyncio.run(main(secs))
