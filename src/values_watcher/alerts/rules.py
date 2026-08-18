"""Reglas de alerta: filtra eventos habilitados y aplica dedup por ventana de tiempo."""

from __future__ import annotations

import json
import logging
import time

from values_watcher.alerts.client import AlertClient, build_payload
from values_watcher.storage.db import Database

log = logging.getLogger(__name__)


def dedup_key(event_type: str, payload: dict) -> str:
    """Clave de dedup estable por evento.

    - fvg_new/fvg_mitigated: mismo gap (symbol+tf+formed_at)
    - wall: mismo lado y nivel de precio
    - imbalance: por símbolo y dirección del sesgo
    """
    if event_type.startswith("fvg"):
        return f"{event_type}:{payload['symbol']}:{payload['timeframe']}:{payload['formed_at']}"
    if event_type == "wall":
        return f"wall:{payload['symbol']}:{payload['side']}:{payload['price']}"
    if event_type == "imbalance":
        side = "bid" if payload["ratio"] >= 0.5 else "ask"
        return f"imbalance:{payload['symbol']}:{side}"
    if event_type == "large_order":
        return f"large_order:{payload['symbol']}:{payload['side']}:{payload['price']}"
    if event_type == "order_blocks":
        return f"order_blocks:{payload['symbol']}"
    if event_type == "price_target":
        return f"price_target:{payload['symbol']}:{payload['target']}:{payload['crossed']}"
    if event_type == "stop_volume":
        return f"stop_volume:{payload['symbol']}:{payload['side']}:{payload['price']}"
    if event_type == "liquidation":
        bucket = int(payload["price"] // 50 * 50)
        return f"liquidation:{payload['symbol']}:{payload['side']}:{bucket}"
    if event_type == "price_ladder":
        # un escalón = una notificación; tras el rearmado puede volver a avisar
        return f"price_ladder:{payload['symbol']}:{payload['level']}:{payload['step']}"
    return f"{event_type}:{json.dumps(payload, sort_keys=True)}"


class AlertRules:
    def __init__(
        self,
        client: AlertClient,
        db: Database,
        enabled_events: list[str],
        dedup_minutes: int = 30,
    ) -> None:
        self.client = client
        self.db = db
        self.enabled = set(enabled_events)
        self.dedup_ms = dedup_minutes * 60 * 1000

    async def check_and_record(self, event_type: str, payload: dict) -> bool:
        """Dedup + registro. True si la alerta debe enviarse (por cualquier canal)."""
        if event_type not in self.enabled:
            return False
        key = dedup_key(event_type, payload)
        now = int(time.time() * 1000)
        last = await self.db.last_alert_at(key)
        if last is not None and now - last < self.dedup_ms:
            log.debug("Dedup: %s ignorado", key)
            return False
        await self.db.insert_alert(event_type, key, json.dumps(payload), now, True)
        return True

    async def handle(self, event_type: str, payload: dict) -> bool:
        """Evalúa el evento y envía la alerta si corresponde. True si se encoló."""
        if not await self.check_and_record(event_type, payload):
            return False
        return self.client.enqueue(build_payload(event_type, payload))
