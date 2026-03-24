from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from src.domain.events import EventBus, OrderBookUpdated
from src.domain.models import Order, RiskDecision, Signal
from src.settings import RiskConfig
from src.storage.sqlite_store import SQLiteStore
from src.utils.logging import get_logger
from src.utils.time import utc_now, seconds_since

if TYPE_CHECKING:
    from src.services.market_state import MarketStateService

logger = get_logger("services.risk_engine")


class RiskEngine:
    """Evaluates risk constraints and vetoes orders that violate limits.

    Checks: position limit, portfolio limit, category concentration,
    daily drawdown, rate limit, max positions, spread ceiling,
    stale feed veto, market close proximity.
    """

    def __init__(
        self,
        config: RiskConfig,
        store: SQLiteStore,
        event_bus: EventBus | None = None,
        market_state: "MarketStateService | None" = None,
    ) -> None:
        self._config = config
        self._store = store
        self._bus = event_bus
        self._market_state = market_state
        self._halted = False
        self._halt_reason = ""
        self._orders_this_minute: list[datetime] = []
        self._last_book_update: dict[str, datetime] = {}
        self._daily_high_water: float = config.starting_equity_usd
        self._session_start = utc_now()
        self._recent_rejects = 0
        self._recent_orders = 0
        self._last_midpoints: dict[str, float] = {}  # token_id -> last midpoint

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    def set_market_state(self, market_state: "MarketStateService") -> None:
        self._market_state = market_state

    def halt(self, reason: str) -> None:
        self._halted = True
        self._halt_reason = reason
        logger.warning("RISK HALT: %s", reason)

    def resume(self) -> None:
        self._halted = False
        self._halt_reason = ""
        self._recent_rejects = 0
        self._recent_orders = 0
        logger.info("Risk engine resumed — reject counters reset")

    def record_book_update(self, token_id: str) -> None:
        self._last_book_update[token_id] = utc_now()

    async def check(self, order: Order, signal: Signal) -> RiskDecision:
        if self._halted:
            return RiskDecision(approved=False, reason=f"Trading halted: {self._halt_reason}")

        # Rate limit is checked first — rejections from it are "soft"
        # and don't count toward the reject rate (they're expected throttling)
        rate_result = await self._check_rate_limit()
        if not rate_result.approved:
            return RiskDecision(approved=False, reason=rate_result.reason)

        # "Hard" risk checks — rejections here count toward reject rate
        checks = [
            self._check_position_limit(order),
            self._check_portfolio_limit(order),
            self._check_category_concentration(order),
            self._check_correlated_exposure(order),
            self._check_daily_drawdown(),
            self._check_max_positions(),
            self._check_spread_ceiling(signal),
            self._check_stale_feed(order),
            self._check_market_close_proximity(order),
            self._check_rapid_adverse_move(order),
        ]

        reasons = []
        for check_coro in checks:
            result = await check_coro
            if not result.approved:
                reasons.append(result.reason)

        self._recent_orders += 1

        if reasons:
            self._recent_rejects += 1
            combined = "; ".join(reasons)
            await self._store.insert_risk_event(
                severity="WARNING",
                event_type="ORDER_REJECTED",
                details={
                    "order_id": order.id, "signal_id": signal.id,
                    "reason": combined, "edge": signal.edge,
                },
            )
            return RiskDecision(approved=False, reason=combined)

        # Check reject rate AFTER counting — only hard rejects matter
        reject_result = await self._check_reject_rate()
        if not reject_result.approved:
            return RiskDecision(approved=False, reason=reject_result.reason)

        self._orders_this_minute.append(utc_now())
        return RiskDecision(approved=True)

    async def run_monitor(self) -> None:
        """Background task — monitors feed health and triggers halts."""
        if not self._bus:
            return
        book_q = self._bus.subscribe(OrderBookUpdated)
        logger.info("Risk monitor started")
        while True:
            try:
                event: OrderBookUpdated = await asyncio.wait_for(book_q.get(), timeout=5.0)
                self.record_book_update(event.snapshot.token_id)
            except asyncio.TimeoutError:
                await self._check_global_feed_health()

    async def _check_global_feed_health(self) -> None:
        if not self._last_book_update:
            return
        stale_timeout = self._config.stale_feed_timeout_sec
        all_stale = all(
            seconds_since(ts) > stale_timeout for ts in self._last_book_update.values()
        )
        if all_stale and not self._halted:
            self.halt(f"All feeds stale for >{stale_timeout}s")
            await self._store.insert_risk_event(
                severity="CRITICAL", event_type="FEED_STALE_HALT",
                details={"timeout_sec": stale_timeout, "tracked_tokens": len(self._last_book_update)},
            )

    async def _check_position_limit(self, order: Order) -> RiskDecision:
        positions = await self._store.get_open_positions()
        current = sum(p.size * p.avg_entry for p in positions if p.token_id == order.token_id)
        proposed = current + order.size * order.price
        limit = self._config.starting_equity_usd * (self._config.max_risk_per_trade_pct / 100)
        if proposed > limit:
            return RiskDecision(False, f"Position limit: ${proposed:.2f} > ${limit:.2f}")
        return RiskDecision(True)

    async def _check_portfolio_limit(self, order: Order) -> RiskDecision:
        positions = await self._store.get_open_positions()
        total = sum(p.size * p.avg_entry for p in positions)
        proposed = total + order.size * order.price
        limit = self._config.starting_equity_usd * (self._config.max_market_exposure_pct / 100)
        if proposed > limit:
            return RiskDecision(False, f"Portfolio limit: ${proposed:.2f} > ${limit:.2f}")
        return RiskDecision(True)

    async def _check_category_concentration(self, order: Order) -> RiskDecision:
        if not self._market_state:
            return RiskDecision(True)
        market = self._market_state.get_market(order.market_condition_id)
        if not market or not market.category:
            return RiskDecision(True)
        positions = await self._store.get_open_positions()
        cat_exp = 0.0
        for pos in positions:
            pm = self._market_state.get_market(pos.condition_id)
            if pm and pm.category == market.category:
                cat_exp += pos.size * pos.avg_entry
        proposed = cat_exp + order.size * order.price
        limit = self._config.starting_equity_usd * (self._config.max_category_exposure_pct / 100)
        if proposed > limit:
            return RiskDecision(False, f"Category '{market.category}': ${proposed:.2f} > ${limit:.2f}")
        return RiskDecision(True)

    async def _check_daily_drawdown(self) -> RiskDecision:
        positions = await self._store.get_all_positions()
        total_pnl = sum(p.realized_pnl + p.unrealized_pnl for p in positions)
        equity = self._config.starting_equity_usd + total_pnl
        if equity > self._daily_high_water:
            self._daily_high_water = equity
        drawdown = self._daily_high_water - equity
        max_dd = self._config.starting_equity_usd * (self._config.max_daily_drawdown_pct / 100)
        if drawdown > max_dd:
            self.halt(f"Drawdown ${drawdown:.2f} from HWM ${self._daily_high_water:.2f}")
            return RiskDecision(False, f"Daily drawdown: ${drawdown:.2f} > ${max_dd:.2f}")
        return RiskDecision(True)

    async def _check_rate_limit(self) -> RiskDecision:
        now = utc_now()
        cutoff = now - timedelta(minutes=1)
        self._orders_this_minute = [t for t in self._orders_this_minute if t > cutoff]
        if len(self._orders_this_minute) >= self._config.max_orders_per_minute:
            return RiskDecision(False, f"Rate limit: {len(self._orders_this_minute)}/min")
        return RiskDecision(True)

    async def _check_max_positions(self) -> RiskDecision:
        positions = await self._store.get_open_positions()
        if len(positions) >= self._config.max_open_positions:
            return RiskDecision(False, f"Max positions: {len(positions)} >= {self._config.max_open_positions}")
        return RiskDecision(True)

    async def _check_spread_ceiling(self, signal: Signal) -> RiskDecision:
        if signal.edge < 0:
            return RiskDecision(False, f"Negative edge: {signal.edge:.4f}")
        if self._market_state:
            book = self._market_state.get_book(signal.token_id)
            if book and book.spread > self._config.max_spread_ceiling:
                return RiskDecision(False, f"Spread {book.spread:.4f} > ceiling {self._config.max_spread_ceiling:.4f}")
        return RiskDecision(True)

    async def _check_stale_feed(self, order: Order) -> RiskDecision:
        last = self._last_book_update.get(order.token_id)
        if last is None:
            return RiskDecision(True)
        staleness = seconds_since(last)
        if staleness > self._config.stale_feed_timeout_sec:
            return RiskDecision(False, f"Stale feed: {staleness:.0f}s (max {self._config.stale_feed_timeout_sec}s)")
        return RiskDecision(True)

    async def _check_market_close_proximity(self, order: Order) -> RiskDecision:
        if not self._market_state:
            return RiskDecision(True)
        market = self._market_state.get_market(order.market_condition_id)
        if not market or not market.end_date:
            return RiskDecision(True)
        minutes_left = (market.end_date - utc_now()).total_seconds() / 60.0
        if minutes_left < self._config.no_entry_before_close_minutes:
            return RiskDecision(False, f"Market closes in {minutes_left:.0f}min")
        return RiskDecision(True)

    async def _check_correlated_exposure(self, order: Order) -> RiskDecision:
        """Reject if exposure to correlated markets (same event/theme) exceeds limit."""
        if not self._market_state:
            return RiskDecision(True)
        market = self._market_state.get_market(order.market_condition_id)
        if not market:
            return RiskDecision(True)

        # Correlated = same category + similar slug patterns (heuristic)
        positions = await self._store.get_open_positions()
        corr_exp = 0.0
        for pos in positions:
            pm = self._market_state.get_market(pos.condition_id)
            if not pm:
                continue
            # Same category counts as correlated
            if pm.category and pm.category == market.category:
                corr_exp += pos.size * pos.avg_entry
            # Same slug prefix (e.g. "will-trump-" markets) counts as correlated
            elif market.slug and pm.slug and market.slug.split("-")[:2] == pm.slug.split("-")[:2]:
                corr_exp += pos.size * pos.avg_entry

        proposed = corr_exp + order.size * order.price
        limit = self._config.starting_equity_usd * (self._config.max_correlated_exposure_pct / 100)
        if proposed > limit:
            return RiskDecision(False, f"Correlated exposure: ${proposed:.2f} > ${limit:.2f}")
        return RiskDecision(True)

    async def _check_reject_rate(self) -> RiskDecision:
        """Halt if too many orders are being rejected (sign of bad market conditions)."""
        if self._recent_orders < 10:
            return RiskDecision(True)  # Need enough data
        reject_pct = (self._recent_rejects / self._recent_orders) * 100
        if reject_pct > self._config.max_order_reject_rate_pct:
            self.halt(f"Reject rate {reject_pct:.0f}% > {self._config.max_order_reject_rate_pct}%")
            return RiskDecision(False, f"Reject rate: {reject_pct:.0f}% > {self._config.max_order_reject_rate_pct}%")
        return RiskDecision(True)

    async def _check_rapid_adverse_move(self, order: Order) -> RiskDecision:
        """Reject if the market price has moved rapidly against the intended trade."""
        if not self._market_state:
            return RiskDecision(True)
        book = self._market_state.get_book(order.token_id)
        if not book:
            return RiskDecision(True)

        current_mid = book.midpoint
        last_mid = self._last_midpoints.get(order.token_id)
        self._last_midpoints[order.token_id] = current_mid

        if last_mid is None or last_mid == 0:
            return RiskDecision(True)

        move_pct = abs(current_mid - last_mid) / last_mid * 100
        if move_pct > self._config.rapid_adverse_move_pct:
            return RiskDecision(
                False,
                f"Rapid move: {move_pct:.1f}% > {self._config.rapid_adverse_move_pct}%",
            )
        return RiskDecision(True)

    async def get_risk_summary(self) -> dict:
        positions = await self._store.get_all_positions()
        total_exp = sum(p.size * p.avg_entry for p in positions if p.size > 0)
        total_pnl = sum(p.realized_pnl + p.unrealized_pnl for p in positions)
        equity = self._config.starting_equity_usd + total_pnl
        drawdown = max(0, self._daily_high_water - equity)
        open_count = sum(1 for p in positions if p.size > 0)
        stale = sum(1 for ts in self._last_book_update.values()
                    if seconds_since(ts) > self._config.stale_feed_timeout_sec)
        return {
            "halted": self._halted, "halt_reason": self._halt_reason,
            "total_exposure": total_exp,
            "max_exposure": self._config.starting_equity_usd * (self._config.max_market_exposure_pct / 100),
            "drawdown": drawdown,
            "max_drawdown": self._config.starting_equity_usd * (self._config.max_daily_drawdown_pct / 100),
            "open_positions": open_count, "max_positions": self._config.max_open_positions,
            "stale_tokens": stale, "tracked_tokens": len(self._last_book_update),
        }
