"""Envío de fotos (gráficos) directo a Telegram, sin pasar por el relay."""

from __future__ import annotations

import httpx


async def send_photo(token: str, chat_id: int, png: bytes, caption: str) -> int | None:
    """Manda un PNG como foto a Telegram. Devuelve message_id si OK."""
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.post(
                url,
                data={"chat_id": chat_id, "caption": caption[:1024]},
                files={"photo": ("fvg.png", png, "image/png")},
            )
            if r.status_code == 200:
                data = r.json()
                return data.get("result", {}).get("message_id")
            return None
        except Exception:
            return None


async def send_photo_reply(token: str, chat_id: int, png: bytes, caption: str, reply_to: int) -> int | None:
    """Manda foto como reply a otro mensaje."""
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.post(
                url,
                data={
                    "chat_id": chat_id,
                    "caption": caption[:1024],
                    "reply_to_message_id": reply_to,
                },
                files={"photo": ("fvg.png", png, "image/png")},
            )
            if r.status_code == 200:
                data = r.json()
                return data.get("result", {}).get("message_id")
            return None
        except Exception:
            return None
