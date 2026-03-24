from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from src.domain.enums import OrderStatus, Side


@dataclass
class Token:
    token_id: str
    outcome: str  # "Yes" or "No"
    price: float = 0.0


@dataclass
class Market:
    condition_id: str
    question: str
    slug: str
    tokens: list[Token]
    category: str = ""
    active: bool = True
    volume: float = 0.0
    volume_24h: float = 0.0
    liquidity: float = 0.0
    fees_enabled: bool = False
    neg_risk: bool = False
    end_date: datetime | None = None
    created_at: datetime | None = None


@dataclass
class PriceLevel:
    price: float
    size: float


@dataclass
class OrderBookSnapshot:
    token_id: str
    timestamp: datetime
    bids: list[PriceLevel]
    asks: list[PriceLevel]

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 1.0

    @property
    def midpoint(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    @property
    def bid_depth(self) -> float:
        return sum(level.size for level in self.bids)

    @property
    def ask_depth(self) -> float:
        return sum(level.size for level in self.asks)


@dataclass
class Signal:
    strategy: str
    market_condition_id: str
    token_id: str
    side: Side
    fair_value: float
    market_price: float
    edge: float
    confidence: float
    timestamp: datetime
    rationale: str = ""
    id: str = field(default_factory=lambda: str(uuid4()))


@dataclass
class Order:
    token_id: str
    market_condition_id: str
    signal_id: str
    side: Side
    price: float
    size: float
    order_type: str = "GTC"
    status: OrderStatus = OrderStatus.PENDING
    is_paper: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    filled_at: datetime | None = None
    fill_price: float | None = None
    fill_size: float | None = None
    reject_reason: str = ""
    id: str = field(default_factory=lambda: str(uuid4()))


@dataclass
class Fill:
    order_id: str
    fill_price: float
    fill_size: float
    fee_estimate: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    id: str = field(default_factory=lambda: str(uuid4()))


@dataclass
class Position:
    token_id: str
    condition_id: str
    side: Side
    size: float = 0.0
    avg_entry: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0

    @property
    def market_value(self) -> float:
        return self.size * self.avg_entry


@dataclass
class RiskDecision:
    approved: bool
    reason: str = ""
