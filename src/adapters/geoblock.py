from __future__ import annotations

import httpx

from src.utils.logging import get_logger

logger = get_logger("adapters.geoblock")


async def check_geoblock(http_client: httpx.AsyncClient | None = None) -> bool:
    """Check if we appear to be geoblocked by Polymarket.

    Returns True if geoblocked (403 or connection failure).
    Returns False if access appears normal.
    """
    client = http_client or httpx.AsyncClient(timeout=10.0)
    close_after = http_client is None

    try:
        resp = await client.get("https://gamma-api.polymarket.com/markets?limit=1")
        if resp.status_code == 403:
            logger.warning("Geoblock detected: Polymarket returned 403")
            return True
        if resp.status_code == 200:
            return False
        logger.warning("Unexpected status from geoblock check: %d", resp.status_code)
        return False
    except httpx.HTTPError as exc:
        logger.warning("Geoblock check failed with network error: %s", exc)
        return True
    finally:
        if close_after:
            await client.aclose()
