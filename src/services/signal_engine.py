from __future__ import annotations

from typing import TYPE_CHECKING

from src.domain.events import EventBus, OrderBookUpdated, SignalGenerated
from src.utils.logging import get_logger
from src.utils.time import seconds_since, utc_now

if TYPE_CHECKING:
    from src.services.market_state import MarketStateService
    from src.storage.sqlite_store import SQLiteStore
    from src.strategies.base import BaseStrategy

logger = get_logger("services.signal_engine")

DEBUG_LOG_INTERVAL_SEC = 30.0


class SignalEngine:
    """Runs all strategies on every orderbook update and emits signals."""

    def __init__(
        self,
        strategies: list[BaseStrategy],
        market_state: MarketStateService,
        store: SQLiteStore,
        event_bus: EventBus,
    ) -> None:
        self._strategies = strategies
        self._state = market_state
        self._store = store
        self._bus = event_bus
        self._paused_strategies: set[str] = set()
        self._total_signals = 0
        self._evals_since_log = 0
        self._both_books_count = 0
        self._best_deviation = 0.0
        self._best_deviation_market = ""
        self._last_debug_log = utc_now()

    @property
    def total_signals_generated(self) -> int:
        return self._total_signals

    def pause_strategy(self, name: str) -> None:
        self._paused_strategies.add(name)
        logger.info("Strategy paused: %s", name)

    def resume_strategy(self, name: str) -> None:
        self._paused_strategies.discard(name)
        logger.info("Strategy resumed: %s", name)

    async def run(self) -> None:
        book_q = self._bus.subscribe(OrderBookUpdated)
        logger.info("Signal engine started with %d strategies: %s",
                     len(self._strategies),
                     [s.name for s in self._strategies])

        while True:
            event: OrderBookUpdated = await book_q.get()
            token_id = event.snapshot.token_id

            market = self._state.get_market_for_token(token_id)
            if not market:
                continue

            # Gather all books for this market's tokens
            books = {}
            for token in market.tokens:
                book = self._state.get_book(token.token_id)
                if book:
                    books[token.token_id] = book

            if not books:
                continue

            # Track if both books are available (for debug)
            has_both = len(books) >= 2 and len(market.tokens) >= 2
            if has_both:
                self._both_books_count += 1
                # Track complement deviation for debug
                mids = [b.midpoint for b in books.values()]
                if len(mids) == 2:
                    dev = abs(sum(mids) - 1.0)
                    if dev > self._best_deviation:
                        self._best_deviation = dev
                        self._best_deviation_market = market.question[:50]

            self._evals_since_log += 1
            self._maybe_debug_log()

            positions = await self._store.get_open_positions()

            for strategy in self._strategies:
                if strategy.name in self._paused_strategies:
                    continue

                try:
                    signals = await strategy.evaluate(market, books, positions)
                    for signal in signals:
                        await self._store.insert_signal(signal)
                        await self._bus.emit(SignalGenerated(signal=signal))
                        self._total_signals += 1
                        logger.info(
                            "Signal [%s] %s %s edge=%.4f conf=%.2f | %s",
                            signal.strategy,
                            signal.side.value,
                            signal.token_id[:12],
                            signal.edge,
                            signal.confidence,
                            market.question[:50],
                        )
                except Exception:
                    logger.exception("Strategy %s failed on market %s",
                                     strategy.name, market.condition_id)

    def _maybe_debug_log(self) -> None:
        if seconds_since(self._last_debug_log) < DEBUG_LOG_INTERVAL_SEC:
            return

        all_markets = self._state.get_all_markets()
        all_books = self._state.get_all_books()
        markets_with_both = 0
        for m in all_markets.values():
            if len(m.tokens) >= 2:
                has = all(all_books.get(t.token_id) for t in m.tokens)
                if has:
                    markets_with_both += 1

        logger.info(
            "Signal debug: %d evals, %d markets with both books, "
            "best deviation=%.4f (%s), total signals=%d",
            self._evals_since_log,
            markets_with_both,
            self._best_deviation,
            self._best_deviation_market[:40] if self._best_deviation_market else "none",
            self._total_signals,
        )
        self._evals_since_log = 0
        self._both_books_count = 0
        self._best_deviation = 0.0
        self._best_deviation_market = ""
        self._last_debug_log = utc_now()
