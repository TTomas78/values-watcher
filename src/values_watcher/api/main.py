"""API REST + WebSocket para el dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from values_watcher.storage.db import Database

log = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend"


class Broadcaster:
    """Fan-out de eventos en vivo a los clientes WebSocket conectados."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def broadcast(self, message: dict) -> None:
        dead = []
        for ws in self._clients:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


broadcaster = Broadcaster()


def create_app(db: Database) -> FastAPI:
    app = FastAPI(title="values-watcher")

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/candles")
    async def candles(symbol: str, timeframe: str = "5m", limit: int = 200):
        cur = await db.conn.execute(
            "SELECT open_time, open, high, low, close, volume FROM candles"
            " WHERE symbol=? AND timeframe=? ORDER BY open_time DESC LIMIT ?",
            (symbol, timeframe, limit),
        )
        rows = await cur.fetchall()
        return [
            {"time": r[0] // 1000, "open": r[1], "high": r[2], "low": r[3],
             "close": r[4], "volume": r[5]}
            for r in reversed(rows)
        ]

    @app.get("/api/fvgs")
    async def fvgs(symbol: str, timeframe: str = "5m", limit: int = 100):
        cur = await db.conn.execute(
            "SELECT direction, top, bottom, formed_at, status, mitigated_at FROM fvgs"
            " WHERE symbol=? AND timeframe=? ORDER BY formed_at DESC LIMIT ?",
            (symbol, timeframe, limit),
        )
        rows = await cur.fetchall()
        return [
            {"direction": r[0], "top": r[1], "bottom": r[2], "formed_at": r[3],
             "status": r[4], "mitigated_at": r[5]}
            for r in rows
        ]

    @app.get("/api/orderbook/{symbol}")
    async def orderbook(symbol: str):
        cur = await db.conn.execute(
            "SELECT bids, asks, timestamp FROM book_snapshots"
            " WHERE symbol=? ORDER BY timestamp DESC LIMIT 1", (symbol,))
        row = await cur.fetchone()
        if not row:
            return {"symbol": symbol, "bids": [], "asks": []}
        return {"symbol": symbol, "bids": json.loads(row[0]),
                "asks": json.loads(row[1]), "timestamp": row[2]}

    @app.get("/api/heatmap/{symbol}")
    async def heatmap(symbol: str):
        cur = await db.conn.execute(
            "SELECT data, timestamp FROM heatmaps"
            " WHERE symbol=? ORDER BY timestamp DESC LIMIT 1", (symbol,))
        row = await cur.fetchone()
        if not row:
            return {"symbol": symbol, "data": None}
        return {"symbol": symbol, "data": json.loads(row[0]), "timestamp": row[1]}

    @app.get("/api/liquidations/{symbol}")
    async def liquidations(symbol: str, hours: int = 24, bucket_usd: float = 50):
        import time as _time
        since = int(_time.time() * 1000) - hours * 3600 * 1000
        clusters = await db.liquidation_clusters(symbol, since, bucket_usd)
        total = sum(c["usd"] for c in clusters)
        return {"symbol": symbol, "hours": hours, "bucket_usd": bucket_usd,
                "total_usd": round(total, 2), "clusters": clusters}

    @app.get("/api/alerts")
    async def alerts(limit: int = 50):
        cur = await db.conn.execute(
            "SELECT event_type, payload, sent_at, ok FROM alerts"
            " ORDER BY sent_at DESC LIMIT ?", (limit,))
        rows = await cur.fetchall()
        return [
            {"event": r[0], "payload": json.loads(r[1]), "sent_at": r[2], "ok": bool(r[3])}
            for r in rows
        ]

    @app.websocket("/ws")
    async def ws(ws: WebSocket):
        await broadcaster.connect(ws)
        try:
            while True:
                await ws.receive_text()  # mantiene viva la conexión
        except WebSocketDisconnect:
            broadcaster.disconnect(ws)

    @app.get("/")
    async def index():
        return FileResponse(FRONTEND_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
    return app
