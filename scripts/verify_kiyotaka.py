"""Verificación Fase 4: pide un heatmap real a Kiyotaka con la key del .env."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx

from values_watcher.config import load_config, load_settings


async def main() -> None:
    cfg = load_config()
    settings = load_settings()
    if not settings.kiyotaka_api_key:
        print("FALLÓ: no hay KIYOTAKA_API_KEY en .env")
        sys.exit(1)

    client = httpx.AsyncClient(
        base_url="https://api.kiyotaka.ai",
        headers={"X-Kiyotaka-Key": settings.kiyotaka_api_key},
        timeout=20.0,
    )
    symbol = cfg.symbols[0]
    r = await client.get("/v1/block-sizes",
                         params={"exchange": "BINANCE_FUTURES", "rawSymbol": symbol})
    print(f"block-sizes ({r.status_code}): {r.text[:300]}")
    r.raise_for_status()

    from values_watcher.collectors.kiyotaka import _extract_block_size
    raw = _extract_block_size(r.json())
    hd = int(raw) * 5
    r2 = await client.get("/v1/points", params={
        "type": "BLOCK_BOOK_SNAPSHOT_AGG",
        "exchange": "BINANCE_FUTURES",
        "rawSymbol": symbol,
        "interval": cfg.kiyotaka.interval,
        "period": cfg.kiyotaka.period,
        "blockSize": hd,
        "maxDepth": cfg.kiyotaka.max_depth,
        "sortDirection": "SORT_DIRECTION_DESC",
    })
    print(f"points ({r2.status_code}): {r2.text[:500]}")
    r2.raise_for_status()
    body = r2.json()
    n = len(json.dumps(body))
    print(f"\nVERIFICACIÓN: OK — heatmap {symbol} recibido ({n} bytes, blockSize HD={hd})")
    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
