from __future__ import annotations

from src.domain.events import EventBus, OrderBookUpdated, SignalGenerated
from src.services.market_state import MarketStateService
from src.storage.sqlite_store import SQLiteStore
from src.strategies.base import BaseStrategy
from src.utils.logging import get_logger

logger = get_logger("services.signal_engine")


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
