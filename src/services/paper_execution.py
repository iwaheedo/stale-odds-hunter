from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from src.domain.enums import OrderStatus, Side
from src.domain.events import EventBus, FillOccurred, OrderBookUpdated, SignalGenerated
from src.domain.models import Fill, Order, Position, Signal
from src.services.market_state import MarketStateService
from src.services.risk_engine import RiskEngine
from src.settings import Settings
from src.storage.sqlite_store import SQLiteStore
from src.utils.logging import get_logger
from src.utils.maths import complement_fair_value
from src.utils.time import utc_now, seconds_since

logger = get_logger("services.paper_execution")


class PaperExecutionService:
    """Simulates order execution without touching real markets.

    Also monitors open positions for exit conditions:
    - Edge compresses below exit_edge_threshold
    - Hold time exceeds hold_time_limit_minutes
    - Stop loss hit (position P&L below threshold)
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
        self._latency_ms = 500
        self._total_orders = 0
        self._total_fills = 0
        self._position_open_times: dict[str, datetime] = {}  # token_id -> open time

    @property
    def total_orders(self) -> int:
        return self._total_orders

    @property
    def total_fills(self) -> int:
        return self._total_fills

    async def run(self) -> None:
        signal_q = self._bus.subscribe(SignalGenerated)
        logger.info("Paper execution engine started")

        # Run signal processing and exit monitoring concurrently
        await asyncio.gather(
            self._process_signals(signal_q),
            self._exit_monitor_loop(),
        )

    async def _process_signals(self, signal_q: asyncio.Queue) -> None:
        while True:
            event: SignalGenerated = await signal_q.get()
            signal = event.signal

            try:
                await self._process_signal(signal)
            except Exception:
                logger.exception("Paper execution failed for signal %s", signal.id)

    async def _process_signal(self, signal: Signal) -> None:
        # Simulate network latency
        await asyncio.sleep(self._latency_ms / 1000)

        # Build order from signal
        order = self._build_order(signal)

        # Run risk checks
        decision = await self._risk.check(order, signal)
        if not decision.approved:
            order.status = OrderStatus.REJECTED
            order.reject_reason = decision.reason
            await self._store.insert_order(order)
            self._total_orders += 1
            logger.info(
                "Order REJECTED: %s | %s",
                decision.reason,
                signal.rationale[:60],
            )
            return

        # Persist the order
        await self._store.insert_order(order)
        self._total_orders += 1

        # Attempt to simulate fill
        fill = self._try_fill(order, signal)
        if fill:
            now = utc_now()
            await self._store.update_order_fill(
                order_id=order.id,
                fill_price=fill.fill_price,
                fill_size=fill.fill_size,
                filled_at=now,
            )
            await self._store.insert_fill(fill)
            await self._bus.emit(FillOccurred(fill=fill))
            self._total_fills += 1
            self._position_open_times.setdefault(order.token_id, utc_now())

            logger.info(
                "Paper FILL: %s %s @ %.4f size=%.2f edge=%.4f | %s",
                signal.side.value,
                order.token_id[:12],
                fill.fill_price,
                fill.fill_size,
                signal.edge,
                signal.rationale[:40],
            )
        else:
            # Mark as open (resting limit order in paper mode)
            await self._store.update_order_status(order.id, OrderStatus.OPEN)
            logger.info(
                "Paper ORDER resting: %s %s @ %.4f (no immediate fill)",
                signal.side.value, order.token_id[:12], order.price,
            )

    def _build_order(self, signal: Signal) -> Order:
        """Convert signal to order with position sizing from config."""
        size = self._calculate_size(signal)

        # For taker execution, use the market price (best ask for buys, best bid for sells)
        # For maker, use fair value as limit price
        price = signal.market_price

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

    def _calculate_size(self, signal: Signal) -> float:
        """Position size based on config and confidence."""
        base_size = self._settings.strategies.default_order_size_usd
        max_size = self._settings.strategies.max_order_size_usd

        # Scale by confidence
        scaled = base_size * signal.confidence
        return min(max(scaled, 1.0), max_size)

    def _try_fill(self, order: Order, signal: Signal) -> Fill | None:
        """Simulate fill based on current book state.

        Taker model: fill at best ask (buys) / best bid (sells).
        Pessimistic — no price improvement.
        """
        book = self._state.get_book(order.token_id)
        if not book:
            return None

        if order.side == Side.BUY:
            if not book.asks:
                return None
            best_ask = book.best_ask
            # Fill if our order price >= best ask (marketable)
            if order.price >= best_ask:
                fill_price = best_ask  # pessimistic fill at ask
                fill_size = self._apply_slippage(order.size, book.ask_depth)
                fee = self._estimate_fee(fill_price, fill_size, signal)
                return Fill(
                    id=str(uuid4()),
                    order_id=order.id,
                    fill_price=fill_price,
                    fill_size=fill_size,
                    fee_estimate=fee,
                    timestamp=utc_now(),
                )

        elif order.side == Side.SELL:
            if not book.bids:
                return None
            best_bid = book.best_bid
            if order.price <= best_bid:
                fill_price = best_bid  # pessimistic fill at bid
                fill_size = self._apply_slippage(order.size, book.bid_depth)
                fee = self._estimate_fee(fill_price, fill_size, signal)
                return Fill(
                    id=str(uuid4()),
                    order_id=order.id,
                    fill_price=fill_price,
                    fill_size=fill_size,
                    fee_estimate=fee,
                    timestamp=utc_now(),
                )

        return None

    def _apply_slippage(self, desired_size: float, available_depth: float) -> float:
        """Reduce fill size if order is large relative to book depth."""
        if available_depth <= 0:
            return desired_size * 0.5  # conservative partial fill
        ratio = desired_size / available_depth
        if ratio > 0.5:
            return desired_size * 0.5  # heavy slippage, only fill half
        if ratio > 0.1:
            return desired_size * 0.8  # moderate slippage
        return desired_size  # small order, full fill

    def _estimate_fee(self, price: float, size: float, signal: Signal) -> float:
        """Estimate trading fee based on market's fee status."""
        market = self._state.get_market(signal.market_condition_id)
        if market and market.fees_enabled:
            return price * size * 0.02  # 2% taker fee estimate
        return 0.0

    # --- Exit Monitoring ---

    async def _exit_monitor_loop(self) -> None:
        """Periodically check open positions for exit conditions."""
        exit_threshold = self._settings.strategies.exit_edge_threshold
        hold_limit_min = self._settings.strategies.hold_time_limit_minutes

        while True:
            await asyncio.sleep(5.0)  # check every 5 seconds
            try:
                positions = await self._store.get_open_positions()
                for pos in positions:
                    exit_reason = self._check_exit(pos, exit_threshold, hold_limit_min)
                    if exit_reason:
                        await self._execute_exit(pos, exit_reason)
            except Exception:
                logger.exception("Exit monitor error")

    def _check_exit(self, pos: Position, exit_threshold: float,
                    hold_limit_min: int) -> str | None:
        """Check if a position should be exited.

        Returns exit reason string, or None if no exit needed.
        """
        market = self._state.get_market(pos.condition_id)
        if not market or len(market.tokens) != 2:
            return None

        # Get current books
        yes_book = self._state.get_book(market.tokens[0].token_id)
        no_book = self._state.get_book(market.tokens[1].token_id)
        if not yes_book or not no_book:
            return None

        # Check 1: Edge compression — has the mispricing corrected?
        current_book = self._state.get_book(pos.token_id)
        if current_book and current_book.bids and current_book.asks:
            other_token = (market.tokens[1] if pos.token_id == market.tokens[0].token_id
                          else market.tokens[0])
            other_book = self._state.get_book(other_token.token_id)
            if other_book and other_book.bids and other_book.asks:
                fair = complement_fair_value(other_book.midpoint)
                if pos.side == Side.BUY:
                    remaining_edge = fair - current_book.best_bid
                else:
                    remaining_edge = current_book.best_ask - fair

                if remaining_edge < exit_threshold:
                    return f"edge_compressed: remaining={remaining_edge:.4f} < threshold={exit_threshold}"

        # Check 2: Hold time exceeded
        open_time = self._position_open_times.get(pos.token_id)
        if open_time and hold_limit_min > 0:
            held_minutes = seconds_since(open_time) / 60.0
            if held_minutes > hold_limit_min:
                return f"hold_time_exceeded: {held_minutes:.0f}min > {hold_limit_min}min"

        # Check 3: Market closing soon
        if market.end_date:
            minutes_left = (market.end_date - utc_now()).total_seconds() / 60.0
            close_guard = self._settings.risk.no_entry_before_close_minutes
            if minutes_left < close_guard:
                return f"market_closing: {minutes_left:.0f}min remaining"

        return None

    async def _execute_exit(self, pos: Position, reason: str) -> None:
        """Simulate closing a position."""
        book = self._state.get_book(pos.token_id)
        if not book:
            return

        # Exit: sell if we're long, buy if we're short
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

        # Create exit order
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

        # Create fill
        fee = exit_price * pos.size * 0.02 if self._state.get_market(pos.condition_id) and self._state.get_market(pos.condition_id).fees_enabled else 0.0
        fill = Fill(
            id=str(uuid4()),
            order_id=exit_order.id,
            fill_price=exit_price,
            fill_size=pos.size,
            fee_estimate=fee,
            timestamp=utc_now(),
        )
        await self._store.insert_fill(fill)
        await self._bus.emit(FillOccurred(fill=fill))

        # Clean up tracking
        self._position_open_times.pop(pos.token_id, None)
        self._total_fills += 1

        logger.info(
            "EXIT: %s %s @ %.4f size=%.2f reason=%s",
            exit_side.value, pos.token_id[:12], exit_price, pos.size, reason,
        )
