from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.domain.enums import Side
from src.domain.models import Market, OrderBookSnapshot, PriceLevel, Token
from src.settings import StrategiesConfig
from src.strategies.stale_odds import StaleOddsStrategy


def _make_book(token_id: str, best_bid: float, best_ask: float,
               bid_depth: float = 100, ask_depth: float = 100) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id=token_id,
        timestamp=datetime.now(timezone.utc),
        bids=[PriceLevel(price=best_bid, size=bid_depth)],
        asks=[PriceLevel(price=best_ask, size=ask_depth)],
    )


def _make_market(yes_id: str = "yes_token", no_id: str = "no_token") -> Market:
    return Market(
        condition_id="test_condition",
        question="Will X happen?",
        slug="will-x-happen",
        tokens=[
            Token(token_id=yes_id, outcome="Yes", price=0.50),
            Token(token_id=no_id, outcome="No", price=0.50),
        ],
        category="test",
        fees_enabled=False,
    )


@pytest.fixture
def strategy() -> StaleOddsStrategy:
    config = StrategiesConfig(
        entry_edge_threshold=0.03,
        slippage_buffer=0.005,
        uncertainty_buffer=0.01,
        stale_odds={
            "complement_deviation_threshold": 0.03,
            "staleness_seconds": 60,
            "min_confidence": 0.1,
            "max_spread_for_entry": 0.10,
        },
    )
    return StaleOddsStrategy(config)


@pytest.mark.asyncio
async def test_complement_deviation_generates_signal(strategy: StaleOddsStrategy) -> None:
    """When yes_mid + no_mid != 1.0, a signal should be generated."""
    market = _make_market()
    # YES book: tight spread, midpoint=0.55
    yes_book = _make_book("yes_token", best_bid=0.54, best_ask=0.56)
    # NO book: wider spread, midpoint=0.55 → sum = 1.10 (deviation = 0.10)
    no_book = _make_book("no_token", best_bid=0.50, best_ask=0.60)

    books = {"yes_token": yes_book, "no_token": no_book}
    signals = await strategy.evaluate(market, books, [])

    assert len(signals) >= 1
    signal = signals[0]
    assert signal.strategy == "stale_odds"
    assert signal.edge > 0
    # NO side has wider spread, should be identified as stale
    assert signal.token_id == "no_token"


@pytest.mark.asyncio
async def test_no_signal_when_complement_is_fair(strategy: StaleOddsStrategy) -> None:
    """When prices are fairly valued (sum ≈ 1.0), no signal should be generated."""
    market = _make_market()
    yes_book = _make_book("yes_token", best_bid=0.54, best_ask=0.56)  # mid=0.55
    no_book = _make_book("no_token", best_bid=0.44, best_ask=0.46)   # mid=0.45, sum=1.00

    books = {"yes_token": yes_book, "no_token": no_book}
    signals = await strategy.evaluate(market, books, [])

    assert len(signals) == 0


@pytest.mark.asyncio
async def test_no_signal_when_edge_below_threshold(strategy: StaleOddsStrategy) -> None:
    """Small deviation should not generate a signal if edge < threshold."""
    market = _make_market()
    # Sum = 1.035 — deviation 0.035 is above complement threshold (0.03)
    # but the actual edge after fees/slippage may be below entry threshold
    yes_book = _make_book("yes_token", best_bid=0.51, best_ask=0.53)  # mid=0.52
    no_book = _make_book("no_token", best_bid=0.505, best_ask=0.515)  # mid=0.51, sum=1.03

    books = {"yes_token": yes_book, "no_token": no_book}
    signals = await strategy.evaluate(market, books, [])

    # Edge = fair_value(1.0-0.52=0.48) - best_ask(0.515) - fees(0) - slippage(0.005) - uncertainty(0.01)
    # = 0.48 - 0.515 - 0.015 = -0.05 → negative edge, no signal
    # And for YES: fair_value(1.0-0.51=0.49) - best_ask(0.53) - 0.015 = -0.055 → no signal
    assert len(signals) == 0


@pytest.mark.asyncio
async def test_no_signal_with_empty_books(strategy: StaleOddsStrategy) -> None:
    """Missing book data should produce no signals."""
    market = _make_market()
    signals = await strategy.evaluate(market, {}, [])
    assert len(signals) == 0


@pytest.mark.asyncio
async def test_signal_has_correct_fields(strategy: StaleOddsStrategy) -> None:
    """Verify signal fields are populated correctly."""
    market = _make_market()
    yes_book = _make_book("yes_token", best_bid=0.54, best_ask=0.56)
    no_book = _make_book("no_token", best_bid=0.50, best_ask=0.60)

    books = {"yes_token": yes_book, "no_token": no_book}
    signals = await strategy.evaluate(market, books, [])

    assert len(signals) >= 1
    signal = signals[0]
    assert signal.market_condition_id == "test_condition"
    assert signal.side in (Side.BUY, Side.SELL)
    assert 0 < signal.fair_value < 1
    assert signal.confidence > 0
    assert signal.rationale != ""
