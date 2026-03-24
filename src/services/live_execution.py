from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING
from uuid import uuid4

from src.adapters.geoblock import check_geoblock
from src.adapters.polymarket_live import PolymarketLiveClient
from src.domain.enums import OrderStatus
from src.domain.events import EventBus, FillOccurred, SignalGenerated
from src.domain.models import Fill, Order, Signal
from src.utils.logging import get_logger
from src.utils.time import utc_now

if TYPE_CHECKING:
    from src.services.market_state import MarketStateService
    from src.services.risk_engine import RiskEngine
    from src.settings import Settings
    from src.storage.sqlite_store import SQLiteStore

logger = get_logger("services.live_execution")

HEARTBEAT_INTERVAL = 5  # seconds


class LiveExecutionService:
    """Live order execution via Polymarket CLOB API.

    Feature-flagged: only runs when LIVE_TRADING_ENABLED=true.
    Preconditions checked at startup:
    1. Geoblock check passes
    2. Private key is set
    3. API credentials can be derived
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
        self._client: PolymarketLiveClient | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._total_orders = 0
        self._total_fills = 0

    async def run(self) -> None:
        """Start live execution with safety checks."""
        logger.warning("=" * 50)
        logger.warning("LIVE TRADING MODE ENABLED")
        logger.warning("=" * 50)

        # Precondition 1: Geoblock
        blocked = await check_geoblock()
        if blocked:
            logger.error("GEOBLOCK: Cannot start live trading — region blocked")
            raise RuntimeError("Geoblocked — live trading not available in this region")

        # Precondition 2: Private key
        if not os.environ.get("POLYMARKET_PRIVATE_KEY"):
            raise RuntimeError("POLYMARKET_PRIVATE_KEY not set")

        # Precondition 3: Initialize client (derives API creds)
        try:
            self._client = PolymarketLiveClient()
        except Exception as exc:
            logger.error("Failed to initialize live client: %s", exc)
            raise

        # Start heartbeat
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        # Process signals
        signal_q = self._bus.subscribe(SignalGenerated)
        logger.info("Live execution engine started")

        try:
            while True:
                event: SignalGenerated = await signal_q.get()
                try:
                    await self._process_signal(event.signal)
                except Exception:
                    logger.exception("Live execution failed for signal %s", event.signal.id)
        finally:
            await self._shutdown()

    async def _process_signal(self, signal: Signal) -> None:
        if not self._client:
            return

        order = self._build_order(signal)

        # Risk check
        decision = await self._risk.check(order, signal)
        if not decision.approved:
            order.status = OrderStatus.REJECTED
            order.reject_reason = decision.reason
            await self._store.insert_order(order)
            self._total_orders += 1
            logger.info("LIVE order REJECTED: %s", decision.reason)
            return

        await self._store.insert_order(order)
        self._total_orders += 1

        # Place the real order
        try:
            live_order_id = await self._client.place_order(
                token_id=order.token_id,
                side=order.side.value,
                price=order.price,
                size=order.size,
                order_type=order.order_type,
            )

            # For simplicity in V1, treat GTC orders as immediately filled at intended price
            # In V2, we'd poll for fill status
            now = utc_now()
            await self._store.update_order_fill(order.id, order.price, order.size, now)
            fill = Fill(
                id=str(uuid4()), order_id=order.id,
                fill_price=order.price, fill_size=order.size,
                timestamp=now,
            )
            await self._store.insert_fill(fill)
            await self._bus.emit(FillOccurred(fill=fill))
            self._total_fills += 1

            logger.info("LIVE FILL: %s %s @ %.4f size=%.2f order=%s",
                         signal.side.value, order.token_id[:12],
                         order.price, order.size, live_order_id)

        except Exception as exc:
            logger.error("LIVE order placement failed: %s", exc)
            await self._store.update_order_status(order.id, OrderStatus.REJECTED, str(exc))
            await self._store.insert_risk_event(
                severity="ERROR", event_type="LIVE_ORDER_FAILED",
                details={"order_id": order.id, "error": str(exc)},
            )

    def _build_order(self, signal: Signal) -> Order:
        size = min(
            self._settings.strategies.default_order_size_usd * signal.confidence,
            self._settings.strategies.max_order_size_usd,
        )
        size = max(size, 1.0)

        return Order(
            id=str(uuid4()),
            token_id=signal.token_id,
            market_condition_id=signal.market_condition_id,
            signal_id=signal.id,
            side=signal.side,
            price=signal.market_price,
            size=size,
            order_type="GTC",
            status=OrderStatus.PENDING,
            is_paper=False,
            created_at=utc_now(),
        )

    async def _heartbeat_loop(self) -> None:
        """Send heartbeat every 5s to prevent auto-cancellation."""
        if not self._client:
            return
        logger.info("Heartbeat loop started (interval=%ds)", HEARTBEAT_INTERVAL)
        while True:
            try:
                await self._client.send_heartbeat()
            except Exception:
                logger.exception("Heartbeat send failed — orders may be auto-cancelled")
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def _shutdown(self) -> None:
        """Cancel all orders on shutdown."""
        logger.warning("Live execution shutting down — cancelling all orders")
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._client:
            try:
                await self._client.cancel_all()
            except Exception:
                logger.exception("Cancel-all on shutdown failed")
