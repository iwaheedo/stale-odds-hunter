from __future__ import annotations

from abc import ABC, abstractmethod

from src.domain.models import Market, OrderBookSnapshot, Position, Signal


class BaseStrategy(ABC):
    """All strategies implement this interface."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    async def evaluate(
        self,
        market: Market,
        books: dict[str, OrderBookSnapshot],
        positions: list[Position],
    ) -> list[Signal]:
        """Return zero or more signals for this market.

        Args:
            market: The market to evaluate.
            books: token_id -> latest OrderBookSnapshot for this market's tokens.
            positions: Current open positions (for exposure awareness).

        Returns:
            List of signals. Empty list means no opportunity detected.
        """
        ...
