from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from src.adapters.polymarket_public import PolymarketPublicClient, parse_market
from src.domain.events import EventBus, MarketDiscovered
from src.utils.logging import get_logger

if TYPE_CHECKING:
    from src.domain.models import Market
    from src.settings import Settings

logger = get_logger("services.market_discovery")


class MarketDiscoveryService:
    """Periodically polls Gamma API for active markets matching the configured filters."""

    def __init__(
        self,
        client: PolymarketPublicClient,
        settings: Settings,
        event_bus: EventBus,
    ) -> None:
        self._client = client
        self._settings = settings
        self._bus = event_bus
        self._known: dict[str, Market] = {}

    @property
    def known_markets(self) -> dict[str, Market]:
        return dict(self._known)

    async def run(self) -> None:
        """Long-running polling loop."""
        logger.info("Market discovery started")
        while True:
            try:
                await self._discover()
            except Exception:
                logger.exception("Market discovery cycle failed")
            await asyncio.sleep(self._settings.app.market_data_poll_interval_sec)

    async def discover_once(self) -> list[Market]:
        """Run a single discovery cycle. Useful for CLI one-shot mode."""
        return await self._discover()

    async def _discover(self) -> list[Market]:
        """Fetch, filter, and emit new markets."""
        new_markets: list[Market] = []
        offset = 0
        page_size = 100
        cfg = self._settings.markets

        while True:
            raw_markets = await self._client.get_markets(limit=page_size, offset=offset)
            if not raw_markets:
                break

            for raw in raw_markets:
                market = parse_market(raw)
                if market is None:
                    continue
                if not self._passes_filters(market):
                    continue
                if market.condition_id in self._known:
                    continue
                if len(self._known) >= cfg.max_tracked_markets:
                    logger.info(
                        "Reached max tracked markets (%d), stopping discovery",
                        cfg.max_tracked_markets,
                    )
                    return new_markets

                self._known[market.condition_id] = market
                new_markets.append(market)
                await self._bus.emit(MarketDiscovered(market=market))
                logger.info(
                    "Discovered: %s (liq=%.0f, vol24h=%.0f, spread=n/a)",
                    market.question[:60],
                    market.liquidity,
                    market.volume_24h,
                )

            offset += page_size
            if len(raw_markets) < page_size:
                break

        if new_markets:
            logger.info("Discovery cycle found %d new markets (total tracked: %d)",
                        len(new_markets), len(self._known))
        return new_markets

    def _passes_filters(self, market: Market) -> bool:
        cfg = self._settings.markets

        if not market.active:
            return False

        # Binary only filter
        if cfg.binary_only and len(market.tokens) != 2:
            return False

        # Liquidity filter
        if market.liquidity < cfg.min_liquidity:
            return False

        # Volume filter
        if market.volume_24h < cfg.min_volume_24h:
            return False

        # Category whitelist
        if cfg.categories_whitelist and market.category not in cfg.categories_whitelist:
            return False

        # Slug whitelist (if set, only these)
        if cfg.slugs_whitelist and market.slug not in cfg.slugs_whitelist:
            return False

        # Slug blacklist
        if market.slug in cfg.slugs_blacklist:
            return False

        # Fee filter
        if cfg.exclude_fee_enabled and market.fees_enabled:
            return False

        # Sports filter (basic heuristic)
        return not (cfg.exclude_sports and _is_sports(market))


def _is_sports(market: Market) -> bool:
    """Heuristic: detect sports markets by category or keywords."""
    sports_keywords = {"nfl", "nba", "mlb", "nhl", "soccer", "football", "basketball",
                       "baseball", "hockey", "tennis", "cricket", "ncaa", "serie a"}
    text = f"{market.category} {market.question}".lower()
    return any(kw in text for kw in sports_keywords)
