"""Cliente de alertas: POST JSON a la API del usuario, autenticado por API key.

Reintentos con backoff y cola acotada: si la API está caída, el pipeline no se
bloquea; los eventos más viejos se descartan pasado el límite de la cola.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

log = logging.getLogger(__name__)

DEFAULT_AUTH_HEADER = "Authorization: Bearer {key}"


class AlertClient:
    def __init__(
        self,
        url: str,
        api_key: str,
        auth_header: str = "",
        max_queue: int = 100,
        max_retries: int = 3,
        timeout: float = 10.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.auth_header = auth_header or DEFAULT_AUTH_HEADER
        self.max_retries = max_retries
        self.timeout = timeout
        self._http = http
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=max_queue)
        self._worker: asyncio.Task | None = None
        self.sent = 0
        self.failed = 0

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.api_key)

    def _headers(self) -> dict[str, str]:
        name, _, value = self.auth_header.partition(":")
        return {name.strip(): value.strip().format(key=self.api_key)}

    async def start(self) -> None:
        if self.enabled:
            self._worker = asyncio.create_task(self._run())
        else:
            log.warning("Alertas deshabilitadas: falta NOTIFY_API_URL o NOTIFY_API_KEY")

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    def enqueue(self, payload: dict) -> bool:
        """Encola un evento sin bloquear. False si la cola está llena (se descarta)."""
        if not self.enabled:
            return False
        try:
            self._queue.put_nowait(payload)
            return True
        except asyncio.QueueFull:
            log.error("Cola de alertas llena; evento descartado: %s", payload.get("event"))
            return False

    async def _run(self) -> None:
        while True:
            payload = await self._queue.get()
            ok = await self._post_with_retry(payload)
            if ok:
                self.sent += 1
            else:
                self.failed += 1

    async def _post_with_retry(self, payload: dict) -> bool:
        client = self._http
        owns = client is None
        if owns:
            client = httpx.AsyncClient(timeout=self.timeout)
        try:
            backoff = 1
            for attempt in range(1, self.max_retries + 1):
                try:
                    r = await client.post(self.url, json=payload, headers=self._headers())
                    if r.status_code < 400:
                        return True
                    log.warning("Alerta rechazada (%s): %s", r.status_code, r.text[:200])
                except httpx.HTTPError as e:
                    log.warning("Error enviando alerta (intento %d): %s", attempt, e)
                if attempt < self.max_retries:
                    await asyncio.sleep(backoff)
                    backoff *= 2
            return False
        finally:
            if owns:
                await client.aclose()


SEVERITY_BY_EVENT = {
    "fvg_new": "info",
    "fvg_mitigated": "info",
    "wall": "warning",
    "imbalance": "warning",
    "large_order": "warning",
    "order_blocks": "info",
    "price_target": "warning",
    "stop_volume": "warning",
    "liquidation": "warning",
    "price_ladder": "warning",
}


def build_payload(event_type: str, data: dict, service: str = "values-watcher") -> dict:
    """Esquema de la API de notificaciones (Telegram): title/service/severity/detail.

    La severidad puede venir forzada en data["severity"] (p.ej. large_order
    escala a critical según el tamaño del bloque). Si data trae "text" (p.ej.
    order_blocks), se usa como detail en lugar del JSON.
    """
    title = f"[{event_type}] {data.get('symbol', '')}".strip()
    detail = data.get("text") or json.dumps(data, ensure_ascii=False)
    return {
        "title": title[:200],
        "service": service[:100],
        "severity": data.get("severity") or SEVERITY_BY_EVENT.get(event_type, "info"),
        "detail": detail[:3500],
    }
