from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from src.domain.enums import Side
from src.domain.events import EventBus, FillOccurred
from src.domain.models import Position
from src.utils.logging import get_logger

if TYPE_CHECKING:
    from src.services.market_state import MarketStateService
    from src.storage.sqlite_store import SQLiteStore

logger = get_logger("services.portfolio")

UNREALIZED_UPDATE_SEC = 5.0


class PortfolioEngine:
    """Tracks aggregate portfolio state: positions, P&L, exposure.

    Runs two concurrent tasks:
    1. Fill processor — updates positions on each fill
    2. Mark-to-market — recalculates unrealized P&L every 5s from live prices
    """

    def __init__(
        self,
        store: SQLiteStore,
        event_bus: EventBus,
        market_state: MarketStateService | None = None,
    ) -> None:
        self._store = store
        self._bus = event_bus
        self._market_state = market_state
        self._positions: dict[str, Position] = {}  # token_id -> Position

    async def run(self) -> None:
        logger.info("Portfolio engine started")
        await asyncio.gather(
            self._process_fills(),
            self._mark_to_market_loop(),
        )

    async def _process_fills(self) -> None:
        fill_q = self._bus.subscribe(FillOccurred)
        while True:
            event: FillOccurred = await fill_q.get()
            fill = event.fill

            cursor = await self._store.conn.execute(
                "SELECT token_id, market_condition_id, side, price FROM orders WHERE id = ?",
                (fill.order_id,),
            )
            row = await cursor.fetchone()
            if not row:
                logger.warning("Fill for unknown order: %s", fill.order_id)
                continue

            token_id, condition_id, side_str, _intended_price = row
            side = Side(side_str)

            await self._update_position(
                token_id=token_id,
                condition_id=condition_id,
                side=side,
                fill_price=fill.fill_price,
                fill_size=fill.fill_size,
            )

    async def _mark_to_market_loop(self) -> None:
        """Recalculate unrealized P&L for all open positions using live prices."""
        while True:
            await asyncio.sleep(UNREALIZED_UPDATE_SEC)
            if not self._market_state:
                continue

            try:
                positions = await self._store.get_open_positions()
                updated = 0
                for pos in positions:
                    book = self._market_state.get_book(pos.token_id)
                    if not book or not book.bids or not book.asks:
                        continue

                    # Mark-to-market: what would we get if we closed now?
                    if pos.side == Side.BUY:
                        exit_price = book.best_bid
                        unrealized = (exit_price - pos.avg_entry) * pos.size
                    else:
                        exit_price = book.best_ask
                        unrealized = (pos.avg_entry - exit_price) * pos.size

                    if abs(unrealized - pos.unrealized_pnl) > 0.001:
                        pos.unrealized_pnl = unrealized
                        await self._store.upsert_position(pos)
                        updated += 1

                if updated > 0:
                    logger.debug("Mark-to-market: updated %d positions", updated)

            except Exception:
                logger.exception("Mark-to-market error")

    async def _update_position(
        self,
        token_id: str,
        condition_id: str,
        side: Side,
        fill_price: float,
        fill_size: float,
    ) -> None:
        """Update or create position from a fill."""
        pos = self._positions.get(token_id)

        if pos is None:
            pos = Position(
                token_id=token_id,
                condition_id=condition_id,
                side=side,
                size=fill_size,
                avg_entry=fill_price,
            )
        else:
            if side == pos.side:
                # Adding to position
                total_cost = pos.avg_entry * pos.size + fill_price * fill_size
                pos.size += fill_size
                pos.avg_entry = total_cost / pos.size if pos.size > 0 else 0
            else:
                # Reducing/closing position — realize P&L
                reduce_size = min(fill_size, pos.size)
                if pos.side == Side.BUY:
                    pnl = (fill_price - pos.avg_entry) * reduce_size
                else:
                    pnl = (pos.avg_entry - fill_price) * reduce_size
                pos.realized_pnl += pnl
                pos.size -= reduce_size

                # Position flip
                remaining = fill_size - reduce_size
                if remaining > 0:
                    pos.side = side
                    pos.size = remaining
                    pos.avg_entry = fill_price

                logger.info("Realized P&L: %+.4f on %s (exit %.4f, entry %.4f, size %.2f)",
                            pnl, token_id[:12], fill_price, pos.avg_entry, reduce_size)

        self._positions[token_id] = pos
        await self._store.upsert_position(pos)

    async def get_portfolio_summary(self) -> dict:
        positions = await self._store.get_all_positions()
        total_exposure = sum(p.size * p.avg_entry for p in positions if p.size > 0)
        total_realized = sum(p.realized_pnl for p in positions)
        total_unrealized = sum(p.unrealized_pnl for p in positions)
        open_count = sum(1 for p in positions if p.size > 0)

        return {
            "total_exposure": total_exposure,
            "realized_pnl": total_realized,
            "unrealized_pnl": total_unrealized,
            "total_pnl": total_realized + total_unrealized,
            "open_positions": open_count,
        }
