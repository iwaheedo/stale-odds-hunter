from __future__ import annotations

from src.domain.enums import Side
from src.domain.events import EventBus, FillOccurred
from src.domain.models import Position
from src.storage.sqlite_store import SQLiteStore
from src.utils.logging import get_logger

logger = get_logger("services.portfolio")


class PortfolioEngine:
    """Tracks aggregate portfolio state: positions, P&L, exposure."""

    def __init__(self, store: SQLiteStore, event_bus: EventBus) -> None:
        self._store = store
        self._bus = event_bus
        self._positions: dict[str, Position] = {}  # token_id -> Position

    async def run(self) -> None:
        fill_q = self._bus.subscribe(FillOccurred)
        logger.info("Portfolio engine started")

        while True:
            event: FillOccurred = await fill_q.get()
            fill = event.fill

            # Look up the order to get market info
            cursor = await self._store.conn.execute(
                "SELECT token_id, market_condition_id, side, price FROM orders WHERE id = ?",
                (fill.order_id,),
            )
            row = await cursor.fetchone()
            if not row:
                logger.warning("Fill for unknown order: %s", fill.order_id)
                continue

            token_id, condition_id, side_str, intended_price = row
            side = Side(side_str)

            await self._update_position(
                token_id=token_id,
                condition_id=condition_id,
                side=side,
                fill_price=fill.fill_price,
                fill_size=fill.fill_size,
            )

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
                # Adding to position — recalculate average entry
                total_cost = pos.avg_entry * pos.size + fill_price * fill_size
                pos.size += fill_size
                pos.avg_entry = total_cost / pos.size if pos.size > 0 else 0
            else:
                # Reducing position — realize P&L
                reduce_size = min(fill_size, pos.size)
                if pos.side == Side.BUY:
                    pnl = (fill_price - pos.avg_entry) * reduce_size
                else:
                    pnl = (pos.avg_entry - fill_price) * reduce_size
                pos.realized_pnl += pnl
                pos.size -= reduce_size

                # If fill_size > pos.size, we've flipped the position
                remaining = fill_size - reduce_size
                if remaining > 0:
                    pos.side = side
                    pos.size = remaining
                    pos.avg_entry = fill_price

        self._positions[token_id] = pos
        await self._store.upsert_position(pos)

        logger.info(
            "Position updated: %s %s size=%.2f avg=%.4f rpnl=%.4f",
            token_id[:12], pos.side.value, pos.size, pos.avg_entry, pos.realized_pnl,
        )

    async def get_portfolio_summary(self) -> dict:
        """Return aggregate portfolio metrics."""
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
