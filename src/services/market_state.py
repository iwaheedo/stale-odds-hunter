from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from src.domain.events import (
    EventBus,
    MarketDiscovered,
    OrderBookUpdated,
    TradeReceived,
)
from src.domain.models import Market, OrderBookSnapshot
from src.storage.sqlite_store import SQLiteStore
from src.utils.logging import get_logger
from src.utils.time import seconds_since

logger = get_logger("services.market_state")

SNAPSHOT_PERSIST_INTERVAL_SEC = 5.0


class MarketStateService:
    """In-memory current market state with periodic persistence to SQLite."""

    def __init__(self, store: SQLiteStore, event_bus: EventBus) -> None:
        self._store = store
        self._bus = event_bus
        self._markets: dict[str, Market] = {}  # condition_id -> Market
        self._books: dict[str, OrderBookSnapshot] = {}  # token_id -> latest book
        self._token_to_market: dict[str, str] = {}  # token_id -> condition_id
        self._last_persist: dict[str, datetime] = {}  # token_id -> last persist time

    def get_book(self, token_id: str) -> OrderBookSnapshot | None:
        return self._books.get(token_id)

    def get_market(self, condition_id: str) -> Market | None:
        return self._markets.get(condition_id)

    def get_market_for_token(self, token_id: str) -> Market | None:
        cid = self._token_to_market.get(token_id)
        return self._markets.get(cid) if cid else None

    def get_all_books(self) -> dict[str, OrderBookSnapshot]:
        return dict(self._books)

    def get_all_markets(self) -> dict[str, Market]:
        return dict(self._markets)

    async def run(self) -> None:
        """Listen for events and maintain state."""
        market_q = self._bus.subscribe(MarketDiscovered)
        book_q = self._bus.subscribe(OrderBookUpdated)
        trade_q = self._bus.subscribe(TradeReceived)

        logger.info("Market state service started")

        async def _process_markets() -> None:
            while True:
                event: MarketDiscovered = await market_q.get()
                market = event.market
                self._markets[market.condition_id] = market
                for token in market.tokens:
                    self._token_to_market[token.token_id] = market.condition_id
                await self._store.upsert_market(market)

        async def _process_books() -> None:
            while True:
                event: OrderBookUpdated = await book_q.get()
                snap = event.snapshot
                self._books[snap.token_id] = snap
                await self._maybe_persist_snapshot(snap)

        async def _process_trades() -> None:
            while True:
                event: TradeReceived = await trade_q.get()
                await self._store.insert_trade(
                    event.token_id, event.timestamp, event.side,
                    event.price, event.size,
                )

        await asyncio.gather(
            _process_markets(),
            _process_books(),
            _process_trades(),
        )

    async def _maybe_persist_snapshot(self, snap: OrderBookSnapshot) -> None:
        """Only persist to SQLite every N seconds per token to avoid flooding."""
        last = self._last_persist.get(snap.token_id)
        if last and seconds_since(last) < SNAPSHOT_PERSIST_INTERVAL_SEC:
            return
        self._last_persist[snap.token_id] = datetime.now(timezone.utc)
        await self._store.insert_orderbook_snapshot(snap)
