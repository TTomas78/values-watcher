"""Persistencia en SQLite (async vía aiosqlite)."""

from __future__ import annotations

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    open_time INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, timeframe, open_time)
);
CREATE TABLE IF NOT EXISTS fvgs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    direction TEXT NOT NULL,
    top REAL NOT NULL,
    bottom REAL NOT NULL,
    formed_at INTEGER NOT NULL,
    status TEXT NOT NULL,
    mitigated_at INTEGER
);
CREATE TABLE IF NOT EXISTS book_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    bids TEXT NOT NULL,   -- JSON [[price, qty], ...]
    asks TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    sent_at INTEGER NOT NULL,
    ok INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS heatmaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    data TEXT NOT NULL    -- JSON tal cual viene de Kiyotaka
);
CREATE TABLE IF NOT EXISTS liquidations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,     -- "long" | "short" (posición liquidada)
    price REAL NOT NULL,
    quantity REAL NOT NULL,
    usd REAL NOT NULL,
    timestamp INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_liq_symbol ON liquidations(symbol, timestamp);
CREATE INDEX IF NOT EXISTS idx_fvgs_symbol ON fvgs(symbol, timeframe, status);
CREATE INDEX IF NOT EXISTS idx_snapshots_symbol ON book_snapshots(symbol, timestamp);
CREATE INDEX IF NOT EXISTS idx_alerts_dedup ON alerts(dedup_key, sent_at);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        await self._conn.executescript(SCHEMA)
        # Deduplicar FVGs históricos (el warmup reinsertaba en cada reinicio)
        await self._conn.execute(
            "DELETE FROM fvgs WHERE id NOT IN ("
            "  SELECT id FROM ("
            "    SELECT id, ROW_NUMBER() OVER ("
            "      PARTITION BY symbol, timeframe, formed_at"
            "      ORDER BY (status='mitigated') DESC, id ASC) rn"
            "    FROM fvgs"
            "  ) WHERE rn = 1"
            ")"
        )
        await self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_fvgs_unique"
            " ON fvgs(symbol, timeframe, formed_at)"
        )
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database no conectada"
        return self._conn

    async def insert_candle(self, symbol: str, timeframe: str, c, volume: float) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)",
            (symbol, timeframe, c.open_time, c.open, c.high, c.low, c.close, volume),
        )
        await self.conn.commit()

    async def insert_fvg(self, fvg) -> int:
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO fvgs (symbol,timeframe,direction,top,bottom,formed_at,status,mitigated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (fvg.symbol, fvg.timeframe, fvg.direction.value, fvg.top, fvg.bottom,
             fvg.formed_at, fvg.status.value, fvg.mitigated_at),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def mark_fvg_mitigated(self, fvg) -> None:
        await self.conn.execute(
            "UPDATE fvgs SET status='mitigated', mitigated_at=?"
            " WHERE symbol=? AND timeframe=? AND formed_at=? AND top=? AND bottom=?",
            (fvg.mitigated_at, fvg.symbol, fvg.timeframe, fvg.formed_at, fvg.top, fvg.bottom),
        )
        await self.conn.commit()

    async def insert_snapshot(self, symbol: str, timestamp: int, bids: str, asks: str) -> None:
        await self.conn.execute(
            "INSERT INTO book_snapshots (symbol,timestamp,bids,asks) VALUES (?,?,?,?)",
            (symbol, timestamp, bids, asks),
        )
        await self.conn.commit()

    async def insert_heatmap(self, symbol: str, timestamp: int, data: str) -> None:
        await self.conn.execute(
            "INSERT INTO heatmaps (symbol,timestamp,data) VALUES (?,?,?)",
            (symbol, timestamp, data),
        )
        await self.conn.commit()

    async def insert_liquidation(self, liq) -> None:
        await self.conn.execute(
            "INSERT INTO liquidations (symbol,side,price,quantity,usd,timestamp)"
            " VALUES (?,?,?,?,?,?)",
            (liq.symbol, liq.side, liq.price, liq.quantity, liq.usd, liq.timestamp),
        )
        await self.conn.commit()

    async def liquidation_clusters(self, symbol: str, since_ms: int,
                                   bucket_usd: float) -> list[dict]:
        """USD liquidado agrupado por bucket de precio, más grande primero."""
        cur = await self.conn.execute(
            "SELECT CAST(price / ? AS INTEGER) * ? AS bucket, side,"
            " SUM(usd) AS total_usd, COUNT(*) AS n, MAX(timestamp) AS last_ts"
            " FROM liquidations WHERE symbol=? AND timestamp>=?"
            " GROUP BY bucket, side ORDER BY total_usd DESC",
            (bucket_usd, bucket_usd, symbol, since_ms),
        )
        rows = await cur.fetchall()
        return [
            {"bucket": r[0], "side": r[1], "usd": round(r[2], 2),
             "count": r[3], "last_ts": r[4]}
            for r in rows
        ]

    async def insert_alert(self, event_type: str, dedup_key: str, payload: str,
                           sent_at: int, ok: bool) -> None:
        await self.conn.execute(
            "INSERT INTO alerts (event_type,dedup_key,payload,sent_at,ok) VALUES (?,?,?,?,?)",
            (event_type, dedup_key, payload, sent_at, int(ok)),
        )
        await self.conn.commit()

    async def last_alert_at(self, dedup_key: str) -> int | None:
        cur = await self.conn.execute(
            "SELECT MAX(sent_at) FROM alerts WHERE dedup_key=?", (dedup_key,)
        )
        row = await cur.fetchone()
        return row[0] if row and row[0] is not None else None
