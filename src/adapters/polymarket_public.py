from __future__ import annotations

from datetime import datetime, timezone

import httpx

from src.domain.models import Market, OrderBookSnapshot, PriceLevel, Token
from src.utils.logging import get_logger

logger = get_logger("adapters.polymarket")

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


class PolymarketPublicClient:
    """Async HTTP client for Polymarket public endpoints. No auth required."""

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._http = http_client or httpx.AsyncClient(timeout=15.0)

    async def close(self) -> None:
        await self._http.aclose()

    # --- Gamma API (market discovery) ---

    async def get_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
    ) -> list[dict]:
        """Fetch markets from Gamma API with pagination."""
        params: dict[str, str | int | bool] = {
            "limit": limit,
            "offset": offset,
            "active": active,
            "closed": False,
            "archived": False,
        }
        resp = await self._http.get(f"{GAMMA_BASE}/markets", params=params)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    async def get_market_by_slug(self, slug: str) -> dict | None:
        """Fetch a single market by slug."""
        resp = await self._http.get(f"{GAMMA_BASE}/markets", params={"slug": slug})
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0]
        return None

    async def get_events(self, limit: int = 100, offset: int = 0) -> list[dict]:
        """Fetch events from Gamma API."""
        resp = await self._http.get(
            f"{GAMMA_BASE}/events",
            params={"limit": limit, "offset": offset, "active": True},
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    # --- CLOB API (orderbook / pricing) ---

    async def get_order_book(self, token_id: str) -> OrderBookSnapshot:
        """GET /book?token_id={id} — full orderbook snapshot."""
        resp = await self._http.get(f"{CLOB_BASE}/book", params={"token_id": token_id})
        resp.raise_for_status()
        data = resp.json()

        bids = [
            PriceLevel(price=float(b["price"]), size=float(b["size"]))
            for b in data.get("bids", [])
        ]
        asks = [
            PriceLevel(price=float(a["price"]), size=float(a["size"]))
            for a in data.get("asks", [])
        ]

        return OrderBookSnapshot(
            token_id=token_id,
            timestamp=datetime.now(timezone.utc),
            bids=sorted(bids, key=lambda x: x.price, reverse=True),
            asks=sorted(asks, key=lambda x: x.price),
        )

    async def get_midpoint(self, token_id: str) -> float:
        """GET /midpoint?token_id={id}."""
        resp = await self._http.get(f"{CLOB_BASE}/midpoint", params={"token_id": token_id})
        resp.raise_for_status()
        data = resp.json()
        return float(data.get("mid", 0.0))

    async def get_price(self, token_id: str, side: str) -> float:
        """GET /price?token_id={id}&side={BUY|SELL}."""
        resp = await self._http.get(
            f"{CLOB_BASE}/price", params={"token_id": token_id, "side": side}
        )
        resp.raise_for_status()
        data = resp.json()
        return float(data.get("price", 0.0))

    async def get_spread(self, token_id: str) -> float:
        """GET /spread?token_id={id}."""
        resp = await self._http.get(f"{CLOB_BASE}/spread", params={"token_id": token_id})
        resp.raise_for_status()
        data = resp.json()
        return float(data.get("spread", 0.0))

    async def get_last_trade_price(self, token_id: str) -> float:
        """GET /last-trade-price?token_id={id}."""
        resp = await self._http.get(
            f"{CLOB_BASE}/last-trade-price", params={"token_id": token_id}
        )
        resp.raise_for_status()
        data = resp.json()
        return float(data.get("price", 0.0))


def parse_market(raw: dict) -> Market | None:
    """Parse a raw Gamma API market dict into a Market domain object.
    Returns None if the market data is incomplete or malformed."""
    try:
        condition_id = raw.get("conditionId") or raw.get("condition_id", "")
        if not condition_id:
            return None

        clob_token_ids = raw.get("clobTokenIds")
        outcomes = raw.get("outcomes")
        outcome_prices = raw.get("outcomePrices")

        if not clob_token_ids or not outcomes:
            return None

        # Parse token IDs — may be JSON string or list
        if isinstance(clob_token_ids, str):
            import json
            clob_token_ids = json.loads(clob_token_ids)
        if isinstance(outcomes, str):
            import json
            outcomes = json.loads(outcomes)
        if isinstance(outcome_prices, str):
            import json
            outcome_prices = json.loads(outcome_prices)

        tokens = []
        for i, token_id in enumerate(clob_token_ids):
            outcome_name = outcomes[i] if i < len(outcomes) else f"Outcome_{i}"
            price = float(outcome_prices[i]) if outcome_prices and i < len(outcome_prices) else 0.0
            tokens.append(Token(token_id=str(token_id), outcome=outcome_name, price=price))

        end_date = None
        if raw.get("endDate"):
            try:
                end_date = datetime.fromisoformat(raw["endDate"].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        return Market(
            condition_id=condition_id,
            question=raw.get("question", ""),
            slug=raw.get("slug", ""),
            tokens=tokens,
            category=raw.get("category", ""),
            active=bool(raw.get("active", True)),
            volume=float(raw.get("volume", 0) or 0),
            volume_24h=float(raw.get("volume24hr", 0) or 0),
            liquidity=float(raw.get("liquidity", 0) or 0),
            fees_enabled=bool(raw.get("feesEnabled", False)),
            neg_risk=bool(raw.get("negRisk", False)),
            end_date=end_date,
        )
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        logger.warning("Failed to parse market: %s", exc)
        return None
