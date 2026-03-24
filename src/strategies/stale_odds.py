from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from src.domain.enums import Side
from src.domain.models import Market, OrderBookSnapshot, Position, Signal
from src.strategies.base import BaseStrategy
from src.utils.logging import get_logger
from src.utils.maths import calculate_edge, complement_fair_value
from src.utils.time import seconds_since, utc_now

if TYPE_CHECKING:
    from datetime import datetime

    from src.settings import StrategiesConfig

logger = get_logger("strategies.stale_odds")


class StaleOddsStrategy(BaseStrategy):
    """Detects markets where one side is stale relative to the other.

    Core logic:
    For a binary YES/NO market, price_yes + price_no should ≈ 1.0.
    When the complement deviates beyond a threshold, the side with
    the wider spread is likely stale. Fair value of the stale side
    is estimated as 1.0 - fresh_side_midpoint.
    """

    @property
    def name(self) -> str:
        return "stale_odds"

    def __init__(self, config: StrategiesConfig) -> None:
        stale_cfg = config.stale_odds
        self._complement_threshold = float(stale_cfg.get("complement_deviation_threshold", 0.03))
        self._min_edge = config.entry_edge_threshold
        self._slippage_buffer = config.slippage_buffer
        self._uncertainty_buffer = config.uncertainty_buffer
        self._max_spread = float(stale_cfg.get("max_spread_for_entry", 0.10))
        self._min_confidence = float(stale_cfg.get("min_confidence", 0.3))
        # Cooldown: don't signal the same market more than once per 60s
        self._last_signal_time: dict[str, datetime] = {}
        self._signal_cooldown_sec = 60.0

    async def evaluate(
        self,
        market: Market,
        books: dict[str, OrderBookSnapshot],
        positions: list[Position],
    ) -> list[Signal]:
        if len(market.tokens) != 2:
            return []

        # Cooldown: skip if we signaled this market recently
        last = self._last_signal_time.get(market.condition_id)
        if last and seconds_since(last) < self._signal_cooldown_sec:
            return []

        yes_token = market.tokens[0]
        no_token = market.tokens[1]
        yes_book = books.get(yes_token.token_id)
        no_book = books.get(no_token.token_id)

        if not yes_book or not no_book:
            return []

        # Need at least some bids and asks
        if not yes_book.bids or not yes_book.asks or not no_book.bids or not no_book.asks:
            return []

        signals: list[Signal] = []

        # Check 1: Complement deviation (YES + NO != 1.0)
        complement_sum = yes_book.midpoint + no_book.midpoint
        deviation = abs(complement_sum - 1.0)

        if deviation >= self._complement_threshold:
            signal = self._evaluate_complement_deviation(
                market, yes_token.token_id, no_token.token_id, yes_book, no_book, market.fees_enabled,
            )
            if signal:
                signals.append(signal)

        # Check 2: Spread anomaly (one side much wider than the other)
        for token in market.tokens:
            book = books.get(token.token_id)
            if not book or not book.bids or not book.asks:
                continue
            other_token = no_token if token.token_id == yes_token.token_id else yes_token
            other_book = books.get(other_token.token_id)
            if not other_book or not other_book.bids or not other_book.asks:
                continue

            signal = self._evaluate_spread_anomaly(
                market, token.token_id, book, other_book, market.fees_enabled,
            )
            if signal and not any(s.token_id == signal.token_id for s in signals):
                signals.append(signal)

        # Check 3: Book imbalance (bid depth >> ask depth or vice versa)
        for token in market.tokens:
            book = books.get(token.token_id)
            if not book or not book.bids or not book.asks:
                continue
            other_token = no_token if token.token_id == yes_token.token_id else yes_token
            other_book = books.get(other_token.token_id)
            if not other_book:
                continue

            signal = self._evaluate_book_imbalance(
                market, token.token_id, book, other_book, market.fees_enabled,
            )
            if signal and not any(s.token_id == signal.token_id for s in signals):
                signals.append(signal)

        if signals:
            self._last_signal_time[market.condition_id] = utc_now()
        return signals

    def _evaluate_complement_deviation(
        self,
        market: Market,
        yes_token_id: str,
        no_token_id: str,
        yes_book: OrderBookSnapshot,
        no_book: OrderBookSnapshot,
        fees_enabled: bool,
    ) -> Signal | None:
        """When yes_mid + no_mid != 1.0, the wider-spread side is likely stale."""
        # Determine which side is stale (wider spread = more likely stale)
        if yes_book.spread > no_book.spread:
            stale_id, stale_book = yes_token_id, yes_book
            fresh_book = no_book
        else:
            stale_id, stale_book = no_token_id, no_book
            fresh_book = yes_book

        fair_value = complement_fair_value(fresh_book.midpoint)
        fees = 0.02 if fees_enabled else 0.0  # 2% taker fee estimate

        # Check buy edge (fair > ask → buy the stale side)
        buy_edge = calculate_edge(
            fair_value, stale_book.best_ask, fees,
            self._slippage_buffer, self._uncertainty_buffer,
        )
        # Check sell edge (bid > fair → sell the stale side)
        sell_edge = calculate_edge(
            stale_book.best_bid, fair_value, fees,
            self._slippage_buffer, self._uncertainty_buffer,
        )

        side: Side | None = None
        edge = 0.0
        market_price = 0.0

        if buy_edge >= self._min_edge and stale_book.spread <= self._max_spread:
            side = Side.BUY
            edge = buy_edge
            market_price = stale_book.best_ask
        elif sell_edge >= self._min_edge and stale_book.spread <= self._max_spread:
            side = Side.SELL
            edge = sell_edge
            market_price = stale_book.best_bid

        if side is None:
            return None

        confidence = min(abs(edge) / 0.10, 1.0)
        if confidence < self._min_confidence:
            return None

        return Signal(
            id=str(uuid4()),
            strategy=self.name,
            market_condition_id=market.condition_id,
            token_id=stale_id,
            side=side,
            fair_value=fair_value,
            market_price=market_price,
            edge=edge,
            confidence=confidence,
            timestamp=utc_now(),
            rationale=f"complement_deviation: sum={yes_book.midpoint + no_book.midpoint:.4f}, "
                      f"stale_spread={stale_book.spread:.4f}",
        )

    def _evaluate_spread_anomaly(
        self,
        market: Market,
        token_id: str,
        book: OrderBookSnapshot,
        other_book: OrderBookSnapshot,
        fees_enabled: bool,
    ) -> Signal | None:
        """Wide spread on one side while other side is tight suggests staleness."""
        if book.spread <= self._max_spread:
            return None
        if other_book.spread >= book.spread:
            return None  # Both wide — no clear stale side

        fair_value = complement_fair_value(other_book.midpoint)
        fees = 0.02 if fees_enabled else 0.0

        buy_edge = calculate_edge(
            fair_value, book.best_ask, fees,
            self._slippage_buffer, self._uncertainty_buffer,
        )

        if buy_edge >= self._min_edge:
            confidence = min(abs(buy_edge) / 0.10, 1.0)
            if confidence < self._min_confidence:
                return None
            return Signal(
                id=str(uuid4()),
                strategy=self.name,
                market_condition_id=market.condition_id,
                token_id=token_id,
                side=Side.BUY,
                fair_value=fair_value,
                market_price=book.best_ask,
                edge=buy_edge,
                confidence=confidence,
                timestamp=utc_now(),
                rationale=f"spread_anomaly: spread={book.spread:.4f}, "
                          f"other_spread={other_book.spread:.4f}",
            )
        return None

    def _evaluate_book_imbalance(
        self,
        market: Market,
        token_id: str,
        book: OrderBookSnapshot,
        other_book: OrderBookSnapshot,
        fees_enabled: bool,
    ) -> Signal | None:
        """Detect when bid depth significantly exceeds ask depth (or vice versa).

        Heavy bid depth with thin asks → price should be higher → BUY signal.
        Heavy ask depth with thin bids → price should be lower → SELL signal.

        Fair value is adjusted by the imbalance: midpoint is shifted toward
        the heavier side. The shift magnitude is proportional to the log
        of the imbalance ratio, scaled to a few cents.
        """
        bid_depth = book.bid_depth
        ask_depth = book.ask_depth

        if bid_depth <= 0 or ask_depth <= 0:
            return None

        imbalance_ratio = bid_depth / ask_depth

        # Need at least 5:1 imbalance to avoid noise
        if 0.2 < imbalance_ratio < 5.0:
            return None

        fees = 0.02 if fees_enabled else 0.0
        midpoint = book.midpoint

        # Imbalance-adjusted fair value: shift midpoint toward the heavier side
        # A 3:1 bid/ask ratio shifts fair value up by ~1.5 cents
        # A 5:1 ratio shifts by ~2.5 cents, capped at 5 cents
        import math
        shift = min(math.log2(max(imbalance_ratio, 1.0 / imbalance_ratio)) * 0.01, 0.05)

        if imbalance_ratio >= 5.0:
            # Heavy bids → fair value is above midpoint
            fair_value = midpoint + shift
            edge = fair_value - book.best_ask - fees - self._slippage_buffer
            if edge >= self._min_edge:
                confidence = min(imbalance_ratio / 10.0, 1.0)
                if confidence < self._min_confidence:
                    return None
                return Signal(
                    id=str(uuid4()),
                    strategy=self.name,
                    market_condition_id=market.condition_id,
                    token_id=token_id,
                    side=Side.BUY,
                    fair_value=fair_value,
                    market_price=book.best_ask,
                    edge=edge,
                    confidence=confidence,
                    timestamp=utc_now(),
                    rationale=f"book_imbalance: bid={bid_depth:.0f}, ask={ask_depth:.0f}, "
                              f"ratio={imbalance_ratio:.1f}x, shift={shift:.4f}",
                )

        elif imbalance_ratio <= 0.2:
            # Heavy asks → fair value is below midpoint
            fair_value = midpoint - shift
            edge = book.best_bid - fair_value - fees - self._slippage_buffer
            if edge >= self._min_edge:
                inv_ratio = 1.0 / imbalance_ratio
                confidence = min(inv_ratio / 10.0, 1.0)
                if confidence < self._min_confidence:
                    return None
                return Signal(
                    id=str(uuid4()),
                    strategy=self.name,
                    market_condition_id=market.condition_id,
                    token_id=token_id,
                    side=Side.SELL,
                    fair_value=fair_value,
                    market_price=book.best_bid,
                    edge=edge,
                    confidence=confidence,
                    timestamp=utc_now(),
                    rationale=f"book_imbalance: bid={bid_depth:.0f}, ask={ask_depth:.0f}, "
                              f"ratio={imbalance_ratio:.1f}x, shift={shift:.4f}",
                )

        return None
