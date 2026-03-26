from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from src.domain.events import (
    EventBus,
    MarketDiscovered,
    OrderBookUpdated,
    TradeReceived,
)
from src.utils.logging import get_logger
from src.utils.time import seconds_since, utc_now

if TYPE_CHECKING:
    from src.adapters.polymarket_public import PolymarketPublicClient
    from src.domain.models import Market, OrderBookSnapshot
    from src.storage.sqlite_store import SQLiteStore

logger = get_logger("services.market_state")

SNAPSHOT_PERSIST_INTERVAL_SEC = 5.0
POLL_STALE_INTERVAL_SEC = 10.0
BOOK_STALE_THRESHOLD_SEC = 5.0


class MarketStateService:
    """In-memory current market state with periodic persistence to SQLite.

    Includes HTTP polling fallback: if a token's book hasn't been updated
    via WebSocket in 5s, polls the CLOB /book endpoint directly.
    """

    def __init__(
        self,
        store: SQLiteStore,
        event_bus: EventBus,
        http_client: PolymarketPublicClient | None = None,
    ) -> None:
        self._store = store
        self._bus = event_bus
        self._http = http_client
        self._markets: dict[str, Market] = {}  # condition_id -> Market
        self._books: dict[str, OrderBookSnapshot] = {}  # token_id -> latest book
        self._token_to_market: dict[str, str] = {}  # token_id -> condition_id
        self._last_persist: dict[str, datetime] = {}  # token_id -> last persist time
        self._last_book_update: dict[str, datetime] = {}  # token_id -> last WS/poll update

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
                self._last_book_update[snap.token_id] = utc_now()
                await self._maybe_persist_snapshot(snap)

        async def _process_trades() -> None:
            while True:
                event: TradeReceived = await trade_q.get()
                await self._store.insert_trade(
                    event.token_id, event.timestamp, event.side,
                    event.price, event.size,
                )

        tasks = [
            _process_markets(),
            _process_books(),
            _process_trades(),
        ]
        if self._http:
            tasks.append(self._poll_stale_books())

        await asyncio.gather(*tasks)

    async def _maybe_persist_snapshot(self, snap: OrderBookSnapshot) -> None:
        """Only persist to SQLite every N seconds per token to avoid flooding."""
        last = self._last_persist.get(snap.token_id)
        if last and seconds_since(last) < SNAPSHOT_PERSIST_INTERVAL_SEC:
            return
        self._last_persist[snap.token_id] = datetime.now(UTC)
        await self._store.insert_orderbook_snapshot(snap)

    async def _poll_stale_books(self) -> None:
        """HTTP polling fallback: fetch books for tokens not updated by WebSocket recently."""
        logger.info("HTTP poll fallback started (interval=%ds, stale=%ds)",
                     int(POLL_STALE_INTERVAL_SEC), int(BOOK_STALE_THRESHOLD_SEC))
        first_poll_done = False
        poll_errors = 0
        while True:
            await asyncio.sleep(POLL_STALE_INTERVAL_SEC)
            if not self._http:
                logger.info("HTTP poll: no HTTP client available")
                continue

            polled = 0
            errors = 0
            for market in list(self._markets.values()):
                for token in market.tokens:
                    last = self._last_book_update.get(token.token_id)
                    if last and seconds_since(last) < BOOK_STALE_THRESHOLD_SEC:
                        continue
                    try:
                        snap = await self._http.get_order_book(token.token_id)
                        if snap.bids or snap.asks:
                            self._books[snap.token_id] = snap
                            self._last_book_update[snap.token_id] = utc_now()
                            await self._bus.emit(OrderBookUpdated(snapshot=snap))
                            await self._maybe_persist_snapshot(snap)
                            polled += 1
                            if not first_poll_done:
                                logger.info("HTTP poll: first book received (%s, %d bids, %d asks)",
                                            token.token_id[:16], len(snap.bids), len(snap.asks))
                                first_poll_done = True
                    except Exception as exc:
                        errors += 1
                        if errors <= 3:  # Log first few errors per cycle
                            logger.warning("HTTP poll error for %s: %s", token.token_id[:16], exc)

            tracked = len(self._markets)
            poll_errors += errors
            logger.info("HTTP poll: %d fetched, %d errors (%d markets, %d tokens total, %d cumulative errors)",
                        polled, errors, tracked, len(self._last_book_update), poll_errors)
