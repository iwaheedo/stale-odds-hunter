from __future__ import annotations

import asyncio
import json

import websockets
from websockets.exceptions import ConnectionClosed

from src.domain.events import EventBus, OrderBookUpdated, TradeReceived
from src.domain.models import OrderBookSnapshot, PriceLevel
from src.settings import Settings
from src.utils.logging import get_logger
from src.utils.time import utc_now

logger = get_logger("adapters.websocket")

WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class PolymarketWebSocket:
    """Manages WebSocket connection to Polymarket CLOB market channel."""

    def __init__(self, event_bus: EventBus, settings: Settings) -> None:
        self._bus = event_bus
        self._settings = settings
        self._subscribed_assets: set[str] = set()
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._connected = False
        self._last_message_at = utc_now()

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def last_message_at(self):  # noqa: ANN201
        return self._last_message_at

    @property
    def subscribed_count(self) -> int:
        return len(self._subscribed_assets)

    async def run(self) -> None:
        """Connect, subscribe, read messages forever with auto-reconnect."""
        logger.info("WebSocket client starting")
        while True:
            try:
                async with websockets.connect(
                    WS_MARKET_URL,
                    ping_interval=None,  # we handle pings manually
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    logger.info("WebSocket connected")

                    await self._resubscribe_all()

                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    read_task = asyncio.create_task(self._read_loop(ws))

                    done, pending = await asyncio.wait(
                        [ping_task, read_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
                    # Re-raise if read/ping failed with an unexpected error
                    for t in done:
                        if t.exception() and not isinstance(t.exception(), ConnectionClosed):
                            raise t.exception()  # type: ignore[misc]

            except (ConnectionClosed, OSError, TimeoutError) as exc:
                logger.warning("WebSocket disconnected: %s — reconnecting in %ds",
                               type(exc).__name__, self._settings.app.ws_reconnect_sec)
            except Exception:
                logger.exception("Unexpected WebSocket error — reconnecting")
            finally:
                self._connected = False
                self._ws = None

            await asyncio.sleep(self._settings.app.ws_reconnect_sec)

    async def subscribe(self, token_ids: list[str]) -> None:
        """Subscribe to live data for the given token IDs."""
        new_ids = [tid for tid in token_ids if tid not in self._subscribed_assets]
        if not new_ids:
            return
        self._subscribed_assets.update(new_ids)
        if self._ws and self._connected:
            await self._send_subscribe(new_ids)
        logger.info("Subscribed to %d tokens (total: %d)", len(new_ids), len(self._subscribed_assets))

    async def _send_subscribe(self, token_ids: list[str]) -> None:
        if not self._ws:
            return
        msg = json.dumps({
            "assets_ids": token_ids,
            "type": "market",
        })
        try:
            await self._ws.send(msg)
        except ConnectionClosed:
            logger.warning("Cannot subscribe — connection closed")

    async def _resubscribe_all(self) -> None:
        if self._subscribed_assets:
            await self._send_subscribe(list(self._subscribed_assets))
            logger.info("Re-subscribed to %d tokens after reconnect", len(self._subscribed_assets))

    async def _ping_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        interval = self._settings.app.ws_ping_interval_sec
        while True:
            await asyncio.sleep(interval)
            try:
                await ws.send("PING")
            except ConnectionClosed:
                return

    async def _read_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        async for raw in ws:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            if raw == "PONG":
                continue

            self._last_message_at = utc_now()

            try:
                msgs = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Malformed WebSocket message, dropping")
                continue

            # The market channel can send a list of events
            if isinstance(msgs, dict):
                msgs = [msgs]

            for msg in msgs:
                await self._handle_message(msg)

    async def _handle_message(self, msg: dict) -> None:
        event_type = msg.get("event_type", "")
        asset_id = msg.get("asset_id", "")

        if event_type == "book":
            snapshot = self._parse_book(msg, asset_id)
            if snapshot:
                await self._bus.emit(OrderBookUpdated(snapshot=snapshot))

        elif event_type == "price_change":
            # price_change contains updated best bid/ask
            snapshot = self._parse_price_change(msg, asset_id)
            if snapshot:
                await self._bus.emit(OrderBookUpdated(snapshot=snapshot))

        elif event_type == "last_trade_price":
            price = float(msg.get("price", 0))
            size = float(msg.get("size", 0))
            side = msg.get("side", "")
            ts = float(msg.get("timestamp", 0))
            if asset_id and price > 0:
                await self._bus.emit(TradeReceived(
                    token_id=asset_id,
                    price=price,
                    size=size,
                    side=side,
                    timestamp=ts,
                ))

    def _parse_book(self, msg: dict, token_id: str) -> OrderBookSnapshot | None:
        try:
            bids = [
                PriceLevel(price=float(b["price"]), size=float(b["size"]))
                for b in msg.get("bids", [])
            ]
            asks = [
                PriceLevel(price=float(a["price"]), size=float(a["size"]))
                for a in msg.get("asks", [])
            ]
            return OrderBookSnapshot(
                token_id=token_id,
                timestamp=utc_now(),
                bids=sorted(bids, key=lambda x: x.price, reverse=True),
                asks=sorted(asks, key=lambda x: x.price),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Failed to parse book message: %s", exc)
            return None

    def _parse_price_change(self, msg: dict, token_id: str) -> OrderBookSnapshot | None:
        """Parse price_change into a minimal snapshot with best bid/ask."""
        try:
            changes = msg.get("changes", [])
            bids = []
            asks = []
            for change in changes:
                side = change.get("side", "")
                price = float(change.get("price", 0))
                size = float(change.get("size", 0))
                if side == "BUY":
                    bids.append(PriceLevel(price=price, size=size))
                elif side == "SELL":
                    asks.append(PriceLevel(price=price, size=size))

            if not bids and not asks:
                return None

            return OrderBookSnapshot(
                token_id=token_id,
                timestamp=utc_now(),
                bids=sorted(bids, key=lambda x: x.price, reverse=True),
                asks=sorted(asks, key=lambda x: x.price),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Failed to parse price_change: %s", exc)
            return None
