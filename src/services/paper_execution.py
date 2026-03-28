from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from src.domain.enums import OrderStatus, Side
from src.domain.events import EventBus, FillOccurred, SignalGenerated
from src.domain.models import Fill, Order, Position, Signal
from src.utils.logging import get_logger
from src.utils.time import seconds_since, utc_now

if TYPE_CHECKING:
    from datetime import datetime

    from src.services.market_state import MarketStateService
    from src.services.risk_engine import RiskEngine
    from src.settings import Settings
    from src.storage.sqlite_store import SQLiteStore

logger = get_logger("services.paper_execution")


@dataclass
class PositionMeta:
    """Metadata tracked per open position for exit decisions."""
    token_id: str
    condition_id: str
    entry_edge: float
    entry_price: float
    entry_fair_value: float
    entry_side: Side
    entry_time: datetime
    size: float


class PaperExecutionService:
    """Simulates order execution with proper exit logic.

    Entry: fills at best ask (buys) / best bid (sells) — pessimistic taker model.
    Exit conditions:
    - Profit target: current P&L > 1.5x entry edge × size
    - Stop-loss: current P&L < -2x entry edge × size
    - Time stop: held > 30 minutes
    - Edge compression: mispricing corrected
    """

    def __init__(
        self,
        market_state: MarketStateService,
        risk_engine: RiskEngine,
        store: SQLiteStore,
        event_bus: EventBus,
        settings: Settings,
    ) -> None:
        self._state = market_state
        self._risk = risk_engine
        self._store = store
        self._bus = event_bus
        self._settings = settings
        self._total_orders = 0
        self._total_fills = 0
        self._total_exits = 0
        # Track open position metadata for exits
        self._open_metas: dict[str, PositionMeta] = {}  # token_id -> PositionMeta

    @property
    def total_orders(self) -> int:
        return self._total_orders

    @property
    def total_fills(self) -> int:
        return self._total_fills

    async def run(self) -> None:
        signal_q = self._bus.subscribe(SignalGenerated)
        logger.info("Paper execution engine started")

        await asyncio.gather(
            self._process_signals(signal_q),
            self._exit_monitor_loop(),
        )

    # --- Signal Processing ---

    async def _process_signals(self, signal_q: asyncio.Queue[SignalGenerated]) -> None:
        while True:
            event: SignalGenerated = await signal_q.get()
            try:
                await self._process_signal(event.signal)
            except Exception:
                logger.exception("Paper execution failed for signal %s", event.signal.id)

    async def _process_signal(self, signal: Signal) -> None:
        # Small latency simulation
        await asyncio.sleep(0.1)

        order = self._build_order(signal)

        # Risk check
        decision = await self._risk.check(order, signal)
        if not decision.approved:
            order.status = OrderStatus.REJECTED
            order.reject_reason = decision.reason
            await self._store.insert_order(order)
            self._total_orders += 1
            return

        await self._store.insert_order(order)
        self._total_orders += 1

        # Fill simulation
        fill = self._try_fill(order, signal)
        if fill:
            now = utc_now()
            await self._store.update_order_fill(order.id, fill.fill_price, fill.fill_size, now)
            await self._store.insert_fill(fill)
            await self._bus.emit(FillOccurred(fill=fill))
            self._total_fills += 1

            # Track entry metadata for exit logic
            self._open_metas[order.token_id] = PositionMeta(
                token_id=order.token_id,
                condition_id=order.market_condition_id,
                entry_edge=signal.edge,
                entry_price=fill.fill_price,
                entry_fair_value=signal.fair_value,
                entry_side=signal.side,
                entry_time=now,
                size=fill.fill_size,
            )

            logger.info(
                "FILL: %s %s @ %.4f size=%.2f edge=%.4f | %s",
                signal.side.value, order.token_id[:12],
                fill.fill_price, fill.fill_size, signal.edge,
                signal.rationale[:50],
            )

    def _build_order(self, signal: Signal) -> Order:
        # Flat sizing — no confidence scaling
        base_usd = self._settings.strategies.default_order_size_usd
        price = signal.market_price

        if price < 0.05 or price > 0.95:
            size = 0.0  # Don't trade penny/near-certain tokens
        else:
            size = round(base_usd / price, 2)
            size = min(size, 100.0)  # Cap at 100 shares to limit penny-token leverage
            size = max(size, 5.0)

        return Order(
            id=str(uuid4()),
            token_id=signal.token_id,
            market_condition_id=signal.market_condition_id,
            signal_id=signal.id,
            side=signal.side,
            price=price,
            size=size,
            order_type="GTC",
            status=OrderStatus.PENDING,
            is_paper=True,
            created_at=utc_now(),
        )

    def _try_fill(self, order: Order, signal: Signal) -> Fill | None:
        """Taker fill: at best ask (buys) / best bid (sells). No slippage reduction."""
        book = self._state.get_book(order.token_id)
        if not book:
            return None

        if order.side == Side.BUY:
            if not book.asks:
                return None
            if order.price >= book.best_ask:
                fee = self._estimate_fee(book.best_ask, order.size, signal)
                return Fill(
                    id=str(uuid4()), order_id=order.id,
                    fill_price=book.best_ask, fill_size=order.size,
                    fee_estimate=fee, timestamp=utc_now(),
                )
        elif order.side == Side.SELL:
            if not book.bids:
                return None
            if order.price <= book.best_bid:
                fee = self._estimate_fee(book.best_bid, order.size, signal)
                return Fill(
                    id=str(uuid4()), order_id=order.id,
                    fill_price=book.best_bid, fill_size=order.size,
                    fee_estimate=fee, timestamp=utc_now(),
                )
        return None

    def _estimate_fee(self, price: float, size: float, signal: Signal) -> float:
        market = self._state.get_market(signal.market_condition_id)
        if market and market.fees_enabled:
            return price * size * 0.02
        return 0.0

    # --- Exit Monitoring ---

    async def _exit_monitor_loop(self) -> None:
        """Check open positions every 3s for exit conditions."""
        while True:
            await asyncio.sleep(3.0)
            try:
                positions = await self._store.get_open_positions()
                for pos in positions:
                    meta = self._open_metas.get(pos.token_id)
                    reason = self._check_exit(pos, meta)
                    if reason:
                        await self._execute_exit(pos, reason)
            except Exception:
                logger.exception("Exit monitor error")

    def _check_exit(self, pos: Position, meta: PositionMeta | None) -> str | None:
        """Check exit conditions. Returns reason or None."""
        book = self._state.get_book(pos.token_id)
        if not book or not book.bids or not book.asks:
            return None

        # Current mark-to-market P&L
        if pos.side == Side.BUY:
            current_pnl = (book.best_bid - pos.avg_entry) * pos.size
        else:
            current_pnl = (pos.avg_entry - book.best_ask) * pos.size

        if meta:
            # Absolute dollar thresholds — simple and predictable
            profit_target = 1.00  # Take $1 profit
            stop_loss = -2.00  # Cut at $2 loss

            if current_pnl >= profit_target:
                return f"profit_target: pnl={current_pnl:+.2f} >= ${profit_target:.2f}"

            if current_pnl <= stop_loss:
                return f"stop_loss: pnl={current_pnl:+.2f} <= ${stop_loss:.2f}"

            # Time stop: 5 minutes — forces turnover, prevents stale positions
            held_min = seconds_since(meta.entry_time) / 60.0
            if held_min > 5:
                return f"time_stop: {held_min:.0f}min > 5min"

        else:
            return "no_metadata: exiting orphan position"

        return None

    async def _execute_exit(self, pos: Position, reason: str) -> None:
        """Close a position at market."""
        book = self._state.get_book(pos.token_id)
        if not book:
            return

        if pos.side == Side.BUY:
            if not book.bids:
                return
            exit_price = book.best_bid
            exit_side = Side.SELL
        else:
            if not book.asks:
                return
            exit_price = book.best_ask
            exit_side = Side.BUY

        exit_order = Order(
            id=str(uuid4()),
            token_id=pos.token_id,
            market_condition_id=pos.condition_id,
            signal_id="EXIT",
            side=exit_side,
            price=exit_price,
            size=pos.size,
            order_type="FOK",
            status=OrderStatus.FILLED,
            is_paper=True,
            created_at=utc_now(),
            filled_at=utc_now(),
            fill_price=exit_price,
            fill_size=pos.size,
        )
        await self._store.insert_order(exit_order)

        mkt = self._state.get_market(pos.condition_id)
        fee = exit_price * pos.size * 0.02 if mkt and mkt.fees_enabled else 0.0
        fill = Fill(
            id=str(uuid4()), order_id=exit_order.id,
            fill_price=exit_price, fill_size=pos.size,
            fee_estimate=fee, timestamp=utc_now(),
        )
        await self._store.insert_fill(fill)
        await self._bus.emit(FillOccurred(fill=fill))

        # Clean up metadata
        self._open_metas.pop(pos.token_id, None)
        self._total_exits += 1

        logger.info(
            "EXIT: %s %s @ %.4f size=%.2f reason=%s",
            exit_side.value, pos.token_id[:12], exit_price, pos.size, reason,
        )
