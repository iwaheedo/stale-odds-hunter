from __future__ import annotations

import math
from collections import deque
from typing import TYPE_CHECKING
from uuid import uuid4

from src.domain.enums import Side
from src.domain.models import Market, OrderBookSnapshot, Position, Signal
from src.strategies.base import BaseStrategy
from src.utils.logging import get_logger
from src.utils.time import seconds_since, utc_now

if TYPE_CHECKING:
    from datetime import datetime

    from src.settings import StrategiesConfig

logger = get_logger("strategies.stale_odds")

# How many price snapshots to keep per token (rolling window)
PRICE_HISTORY_SIZE = 60  # ~5 min at 5s intervals


class PricePoint:
    __slots__ = ("midpoint", "spread", "bid_depth", "ask_depth", "timestamp")

    def __init__(self, midpoint: float, spread: float,
                 bid_depth: float, ask_depth: float, timestamp: float) -> None:
        self.midpoint = midpoint
        self.spread = spread
        self.bid_depth = bid_depth
        self.ask_depth = ask_depth
        self.timestamp = timestamp


class StaleOddsStrategy(BaseStrategy):
    """Detects stale odds using multiple signals:

    1. **Momentum detection**: When a market moves significantly in a short window,
       the fair value has shifted. If the current price hasn't fully adjusted,
       there's edge in trading the direction of momentum.

    2. **Spread widening**: When the spread suddenly widens (market makers pulling
       quotes), the last stable midpoint is likely closer to fair value.

    3. **Complement deviation**: When YES + NO drifts from 1.0 (original approach).

    4. **Volume-weighted drift**: When recent trades are consistently on one side
       of the midpoint, fair value is drifting in that direction.
    """

    @property
    def name(self) -> str:
        return "stale_odds"

    def __init__(self, config: StrategiesConfig) -> None:
        stale_cfg = config.stale_odds
        self._min_edge = config.entry_edge_threshold
        self._slippage_buffer = config.slippage_buffer
        self._uncertainty_buffer = config.uncertainty_buffer
        self._max_spread = float(stale_cfg.get("max_spread_for_entry", 0.20))
        self._min_confidence = float(stale_cfg.get("min_confidence", 0.15))
        self._complement_threshold = float(stale_cfg.get("complement_deviation_threshold", 0.01))

        # Momentum params
        self._momentum_window_sec = 60.0  # look back 60s for price moves
        self._momentum_threshold = 0.01  # 1 cent minimum move (was 0.5 cent — too noisy)
        self._momentum_continuation_factor = 0.4  # expect 40% continuation

        # Price history per token
        self._price_history: dict[str, deque[PricePoint]] = {}

        # Cooldown per market
        self._last_signal_time: dict[str, datetime] = {}
        self._signal_cooldown_sec = 30.0

    def _record_price(self, token_id: str, book: OrderBookSnapshot) -> None:
        """Add current price to rolling history."""
        if token_id not in self._price_history:
            self._price_history[token_id] = deque(maxlen=PRICE_HISTORY_SIZE)
        self._price_history[token_id].append(PricePoint(
            midpoint=book.midpoint,
            spread=book.spread,
            bid_depth=book.bid_depth,
            ask_depth=book.ask_depth,
            timestamp=utc_now().timestamp(),
        ))

    async def evaluate(
        self,
        market: Market,
        books: dict[str, OrderBookSnapshot],
        positions: list[Position],
    ) -> list[Signal]:
        if len(market.tokens) != 2:
            return []

        # Cooldown
        last = self._last_signal_time.get(market.condition_id)
        if last and seconds_since(last) < self._signal_cooldown_sec:
            return []

        # Skip if we already have a position
        for pos in positions:
            if pos.condition_id == market.condition_id and pos.size > 0:
                return []

        yes_token = market.tokens[0]
        no_token = market.tokens[1]
        yes_book = books.get(yes_token.token_id)
        no_book = books.get(no_token.token_id)

        if not yes_book or not no_book:
            return []
        if not yes_book.has_both_sides or not no_book.has_both_sides:
            return []

        # Record prices for momentum tracking
        self._record_price(yes_token.token_id, yes_book)
        self._record_price(no_token.token_id, no_book)

        fees = 0.02 if market.fees_enabled else 0.0
        candidates: list[Signal] = []

        # --- Only trade tokens in the 0.05-0.95 range (skip penny/near-certain tokens) ---
        tradeable_pairs = []
        for token, book, other_book in [
            (yes_token, yes_book, no_book),
            (no_token, no_book, yes_book),
        ]:
            if 0.05 <= book.midpoint <= 0.95:
                tradeable_pairs.append((token, book, other_book))

        # --- Check 1: Complement deviation (primary signal — theoretically sound) ---
        complement_sum = yes_book.midpoint + no_book.midpoint
        deviation = abs(complement_sum - 1.0)
        if deviation >= self._complement_threshold:
            signal = self._check_complement(
                market, yes_token.token_id, no_token.token_id,
                yes_book, no_book, fees,
            )
            if signal:
                candidates.append(signal)

        # --- Check 2: Momentum signal (only on tradeable-priced tokens) ---
        for token, book, other_book in tradeable_pairs:
            signal = self._check_momentum(
                market, token.token_id, book, other_book, fees,
            )
            if signal:
                candidates.append(signal)

        # --- Check 3: Spread widening (only on tradeable-priced tokens) ---
        for token, book, other_book in tradeable_pairs:
            signal = self._check_spread_widening(
                market, token.token_id, book, other_book, fees,
            )
            if signal:
                candidates.append(signal)

        # --- Check 4: Depth imbalance (only on mid-range tokens, 50:1+ ratio) ---
        for token, book, other_book in tradeable_pairs:
            signal = self._check_depth_imbalance(
                market, token.token_id, book, other_book, fees,
            )
            if signal:
                candidates.append(signal)

        # --- H1 FIX: Only emit the SINGLE BEST signal per market ---
        # This prevents buying YES and selling NO simultaneously.
        if candidates:
            best = max(candidates, key=lambda s: s.edge * s.confidence)
            self._last_signal_time[market.condition_id] = utc_now()
            return [best]
        return []

    def _check_momentum(
        self, market: Market, token_id: str,
        book: OrderBookSnapshot, other_book: OrderBookSnapshot,
        fees: float,
    ) -> Signal | None:
        """Detect recent price momentum and trade continuation.

        If midpoint moved 0.5+ cents in the last 60s, expect 40% continuation.
        Fair value = current_mid + (recent_move * continuation_factor).
        """
        history = self._price_history.get(token_id)
        if not history or len(history) < 3:
            return None

        now_ts = utc_now().timestamp()
        current_mid = book.midpoint

        # Find the oldest price within our momentum window
        # Deque appends right (newest last), so iterate from left (oldest first)
        # We want the OLDEST entry that falls within the window
        oldest_in_window = None
        for pp in history:
            if now_ts - pp.timestamp <= self._momentum_window_sec:
                oldest_in_window = pp
                break  # First match from left = oldest in window

        if oldest_in_window is None:
            return None

        recent_move = current_mid - oldest_in_window.midpoint
        abs_move = abs(recent_move)

        if abs_move < self._momentum_threshold:
            return None

        # Predict continuation
        expected_continuation = recent_move * self._momentum_continuation_factor
        fair_value = current_mid + expected_continuation

        if book.spread > self._max_spread:
            return None

        if recent_move > 0:
            # Price went up — expect more up — BUY
            edge = fair_value - book.best_ask - fees - self._slippage_buffer
            if edge >= self._min_edge:
                confidence = min(abs_move / 0.02, 1.0)  # 2 cent move = full confidence
                if confidence < self._min_confidence:
                    return None
                return Signal(
                    id=str(uuid4()), strategy=self.name,
                    market_condition_id=market.condition_id,
                    token_id=token_id, side=Side.BUY,
                    fair_value=fair_value, market_price=book.best_ask,
                    edge=edge, confidence=confidence, timestamp=utc_now(),
                    rationale=f"momentum_up: move={recent_move:+.4f} in {self._momentum_window_sec:.0f}s, "
                              f"expected_cont={expected_continuation:+.4f}",
                )
        else:
            # Price went down — expect more down — SELL
            edge = book.best_bid - fair_value - fees - self._slippage_buffer
            if edge >= self._min_edge:
                confidence = min(abs_move / 0.02, 1.0)
                if confidence < self._min_confidence:
                    return None
                return Signal(
                    id=str(uuid4()), strategy=self.name,
                    market_condition_id=market.condition_id,
                    token_id=token_id, side=Side.SELL,
                    fair_value=fair_value, market_price=book.best_bid,
                    edge=edge, confidence=confidence, timestamp=utc_now(),
                    rationale=f"momentum_down: move={recent_move:+.4f} in {self._momentum_window_sec:.0f}s, "
                              f"expected_cont={expected_continuation:+.4f}",
                )
        return None

    def _check_spread_widening(
        self, market: Market, token_id: str,
        book: OrderBookSnapshot, other_book: OrderBookSnapshot,
        fees: float,
    ) -> Signal | None:
        """When spread suddenly widens, the last tight midpoint is likely fair value."""
        history = self._price_history.get(token_id)
        if not history or len(history) < 5:
            return None

        current_spread = book.spread
        # Average spread over recent history
        recent_spreads = [pp.spread for pp in list(history)[-10:]]
        avg_spread = sum(recent_spreads) / len(recent_spreads)

        if avg_spread <= 0:
            return None

        # Spread must be at least 3x the recent average to be considered "widening"
        if current_spread < avg_spread * 3.0:
            return None

        # Fair value = midpoint from before the spread blowout
        stable_points = [pp for pp in history if pp.spread <= avg_spread * 1.5]
        if not stable_points:
            return None

        fair_value = stable_points[-1].midpoint
        edge_buy = fair_value - book.best_ask - fees - self._slippage_buffer
        edge_sell = book.best_bid - fair_value - fees - self._slippage_buffer

        if edge_buy >= self._min_edge:
            confidence = min((current_spread / avg_spread) / 5.0, 1.0)
            if confidence < self._min_confidence:
                return None
            return Signal(
                id=str(uuid4()), strategy=self.name,
                market_condition_id=market.condition_id,
                token_id=token_id, side=Side.BUY,
                fair_value=fair_value, market_price=book.best_ask,
                edge=edge_buy, confidence=confidence, timestamp=utc_now(),
                rationale=f"spread_widening: current={current_spread:.4f}, avg={avg_spread:.4f}, "
                          f"ratio={current_spread/avg_spread:.1f}x",
            )
        if edge_sell >= self._min_edge:
            confidence = min((current_spread / avg_spread) / 5.0, 1.0)
            if confidence < self._min_confidence:
                return None
            return Signal(
                id=str(uuid4()), strategy=self.name,
                market_condition_id=market.condition_id,
                token_id=token_id, side=Side.SELL,
                fair_value=fair_value, market_price=book.best_bid,
                edge=edge_sell, confidence=confidence, timestamp=utc_now(),
                rationale=f"spread_widening: current={current_spread:.4f}, avg={avg_spread:.4f}",
            )
        return None

    def _check_complement(
        self, market: Market,
        yes_token_id: str, no_token_id: str,
        yes_book: OrderBookSnapshot, no_book: OrderBookSnapshot,
        fees: float,
    ) -> Signal | None:
        """Original: when YES + NO != 1.0, the wider-spread side is stale."""
        if yes_book.spread > no_book.spread:
            stale_id, stale_book = yes_token_id, yes_book
            fresh_book = no_book
        else:
            stale_id, stale_book = no_token_id, no_book
            fresh_book = yes_book

        fair_value = 1.0 - fresh_book.midpoint

        buy_edge = fair_value - stale_book.best_ask - fees - self._slippage_buffer - self._uncertainty_buffer
        sell_edge = stale_book.best_bid - fair_value - fees - self._slippage_buffer - self._uncertainty_buffer

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
            id=str(uuid4()), strategy=self.name,
            market_condition_id=market.condition_id,
            token_id=stale_id, side=side,
            fair_value=fair_value, market_price=market_price,
            edge=edge, confidence=confidence, timestamp=utc_now(),
            rationale=f"complement: sum={yes_book.midpoint + no_book.midpoint:.4f}, "
                      f"stale_spread={stale_book.spread:.4f}",
        )

    def _check_depth_imbalance(
        self, market: Market, token_id: str,
        book: OrderBookSnapshot, other_book: OrderBookSnapshot,
        fees: float,
    ) -> Signal | None:
        """When bid depth heavily outweighs ask depth (or vice versa),
        price should drift toward the heavier side.

        Only triggers at 10:1+ ratios to avoid noise.
        """
        bid_depth = book.bid_depth
        ask_depth = book.ask_depth

        if bid_depth <= 0 or ask_depth <= 0:
            return None

        ratio = bid_depth / ask_depth

        # Need extreme imbalance (10:1) to be meaningful
        if 0.05 < ratio < 20.0:
            return None

        if book.spread > self._max_spread:
            return None

        midpoint = book.midpoint
        log_ratio = math.log2(max(ratio, 1.0 / ratio))
        shift = min(log_ratio * 0.005, 0.03)  # Max 3 cent shift

        if ratio >= 20.0:
            # Heavy bids → fair value above midpoint → BUY
            fair_value = midpoint + shift
            edge = fair_value - book.best_ask - fees - self._slippage_buffer
            if edge >= self._min_edge:
                confidence = min(log_ratio / 6.0, 1.0)
                if confidence < self._min_confidence:
                    return None
                return Signal(
                    id=str(uuid4()), strategy=self.name,
                    market_condition_id=market.condition_id,
                    token_id=token_id, side=Side.BUY,
                    fair_value=fair_value, market_price=book.best_ask,
                    edge=edge, confidence=confidence, timestamp=utc_now(),
                    rationale=f"depth_imbalance: bid={bid_depth:.0f}, ask={ask_depth:.0f}, "
                              f"ratio={ratio:.1f}x, shift={shift:.4f}",
                )
        elif ratio <= 0.05:
            # Heavy asks → fair value below midpoint → SELL
            fair_value = midpoint - shift
            edge = book.best_bid - fair_value - fees - self._slippage_buffer
            if edge >= self._min_edge:
                inv = 1.0 / ratio
                confidence = min(math.log2(inv) / 6.0, 1.0)
                if confidence < self._min_confidence:
                    return None
                return Signal(
                    id=str(uuid4()), strategy=self.name,
                    market_condition_id=market.condition_id,
                    token_id=token_id, side=Side.SELL,
                    fair_value=fair_value, market_price=book.best_bid,
                    edge=edge, confidence=confidence, timestamp=utc_now(),
                    rationale=f"depth_imbalance: bid={bid_depth:.0f}, ask={ask_depth:.0f}, "
                              f"ratio={ratio:.1f}x, shift={shift:.4f}",
                )
        return None
