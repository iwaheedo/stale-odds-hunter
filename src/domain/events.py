from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.domain.models import Fill, Market, Order, OrderBookSnapshot, Signal

# --- Event types ---

@dataclass
class MarketDiscovered:
    market: Market


@dataclass
class OrderBookUpdated:
    snapshot: OrderBookSnapshot


@dataclass
class TradeReceived:
    token_id: str
    price: float
    size: float
    side: str
    timestamp: float


@dataclass
class SignalGenerated:
    signal: Signal


@dataclass
class OrderRequested:
    order: Order


@dataclass
class FillOccurred:
    fill: Fill


@dataclass
class RiskHalt:
    reason: str


# --- Event bus ---

class EventBus:
    """In-process async pub/sub. No external broker needed."""

    def __init__(self) -> None:
        self._subscribers: dict[type, list[asyncio.Queue[Any]]] = defaultdict(list)

    def subscribe(self, event_type: type) -> asyncio.Queue[Any]:
        q: asyncio.Queue[Any] = asyncio.Queue()
        self._subscribers[event_type].append(q)
        return q

    async def emit(self, event: Any) -> None:
        for q in self._subscribers[type(event)]:
            await q.put(event)

    def subscriber_count(self, event_type: type) -> int:
        return len(self._subscribers[event_type])
