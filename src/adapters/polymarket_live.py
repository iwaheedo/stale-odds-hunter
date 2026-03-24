from __future__ import annotations

import asyncio
import os

from src.utils.logging import get_logger

logger = get_logger("adapters.polymarket_live")


class PolymarketLiveClient:
    """Authenticated client for Polymarket live trading.

    Wraps py-clob-client SDK. Only instantiated when LIVE_TRADING_ENABLED=true.
    The private key is loaded ONLY in this module, never elsewhere.
    """

    def __init__(self, chain_id: int = 137) -> None:
        private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        if not private_key:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY not set — cannot start live trading")

        try:
            from py_clob_client.client import ClobClient
        except ImportError as exc:
            raise RuntimeError(
                "py-clob-client not installed. Install with: pip install py-clob-client"
            ) from exc

        self._client = ClobClient(
            host="https://clob.polymarket.com",
            key=private_key,
            chain_id=chain_id,
        )
        # Derive API credentials on init
        creds = self._client.create_or_derive_api_creds()
        self._client.set_api_creds(creds)
        self._heartbeat_id = ""
        self._loop = asyncio.get_event_loop()
        logger.info("Live client initialized (chain_id=%d)", chain_id)

    async def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = "GTC",
    ) -> str:
        """Create, sign, and post an order. Returns order_id."""
        from py_clob_client.order import OrderArgs

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
        )

        signed = await self._run_sync(self._client.create_order, order_args)
        result = await self._run_sync(self._client.post_order, signed, order_type)

        order_id = result.get("orderID", "")
        logger.info("Live order placed: %s %s @ %.4f size=%.2f → %s",
                     side, token_id[:12], price, size, order_id)
        return order_id

    async def cancel_order(self, order_id: str) -> None:
        """Cancel a single order."""
        await self._run_sync(self._client.cancel, order_id)
        logger.info("Cancelled order: %s", order_id)

    async def cancel_all(self) -> None:
        """Cancel all open orders."""
        await self._run_sync(self._client.cancel_all)
        logger.warning("CANCEL ALL: All open orders cancelled")

    async def send_heartbeat(self) -> None:
        """Send heartbeat to keep orders alive. Must call every 5s."""
        try:
            # The heartbeat endpoint expects the previous heartbeat_id
            result = await self._run_sync(
                self._client._post,
                "v1/heartbeats",
                {"heartbeat_id": self._heartbeat_id},
            )
            if isinstance(result, dict):
                self._heartbeat_id = result.get("heartbeat_id", self._heartbeat_id)
        except Exception:
            logger.exception("Heartbeat failed")

    async def get_open_orders(self) -> list[dict]:
        """Get all open orders."""
        result = await self._run_sync(self._client.get_orders)
        return result if isinstance(result, list) else []

    async def _run_sync(self, fn, *args):  # noqa: ANN001, ANN202
        """Run a synchronous SDK call in an executor thread."""
        return await asyncio.get_event_loop().run_in_executor(None, fn, *args)
