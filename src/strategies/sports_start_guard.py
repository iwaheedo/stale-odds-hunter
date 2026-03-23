from __future__ import annotations

from src.domain.models import Market, OrderBookSnapshot, Position, Signal
from src.strategies.base import BaseStrategy
from src.utils.logging import get_logger
from src.utils.time import utc_now

logger = get_logger("strategies.sports_guard")


class SportsStartGuardStrategy(BaseStrategy):
    """Guards against placing orders too close to sports event start times.

    Polymarket auto-cancels resting orders when games begin. This strategy
    does not generate buy signals — it generates EXIT signals for positions
    in sports markets nearing start time.
    """

    @property
    def name(self) -> str:
        return "sports_start_guard"

    def __init__(self, guard_minutes: int = 30) -> None:
        self._guard_minutes = guard_minutes

    async def evaluate(
        self,
        market: Market,
        books: dict[str, OrderBookSnapshot],
        positions: list[Position],
    ) -> list[Signal]:
        # Only relevant for sports markets near their end_date/start_time
        if not market.end_date:
            return []

        now = utc_now()
        time_until_close = (market.end_date - now).total_seconds() / 60.0

        if time_until_close > self._guard_minutes:
            return []

        # If we have positions in this market, warn
        signals: list[Signal] = []
        for pos in positions:
            if pos.condition_id == market.condition_id and pos.size > 0:
                logger.warning(
                    "Sports guard: market %s closes in %.0f min, position still open",
                    market.question[:50], time_until_close,
                )

        return signals
