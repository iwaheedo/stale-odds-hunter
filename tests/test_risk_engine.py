"""Tests for the hardened risk engine."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from src.domain.enums import OrderStatus, Side
from src.domain.models import Order, Signal
from src.services.risk_engine import RiskEngine
from src.settings import RiskConfig
from src.storage.sqlite_store import SQLiteStore


@pytest.fixture
async def store(tmp_path):
    db_path = str(tmp_path / "test.db")
    s = SQLiteStore(db_path)
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def risk_config():
    return RiskConfig(
        max_risk_per_trade_pct=1.0,
        max_market_exposure_pct=5.0,
        max_category_exposure_pct=10.0,
        max_daily_drawdown_pct=3.0,
        max_open_positions=5,
        starting_equity_usd=1000.0,
        max_orders_per_minute=3,
        stale_feed_timeout_sec=30,
        max_spread_ceiling=0.25,
        no_entry_before_close_minutes=30,
    )


def make_signal(edge=0.05, confidence=0.5, token_id="tok1", market_id="mkt1"):
    return Signal(
        id=str(uuid4()), strategy="stale_odds",
        market_condition_id=market_id, token_id=token_id,
        side=Side.BUY, fair_value=0.55, market_price=0.50,
        edge=edge, confidence=confidence,
        timestamp=datetime.now(UTC),
    )


def make_order(price=0.50, size=10.0, token_id="tok1", market_id="mkt1"):
    return Order(
        id=str(uuid4()), token_id=token_id,
        market_condition_id=market_id, signal_id=str(uuid4()),
        side=Side.BUY, price=price, size=size,
        status=OrderStatus.PENDING,
    )


@pytest.mark.asyncio
async def test_approve_normal_order(store, risk_config):
    engine = RiskEngine(risk_config, store)
    result = await engine.check(make_order(size=1.0), make_signal())
    assert result.approved


@pytest.mark.asyncio
async def test_reject_position_limit(store, risk_config):
    engine = RiskEngine(risk_config, store)
    result = await engine.check(make_order(price=0.50, size=25.0), make_signal())
    assert not result.approved
    assert "Position limit" in result.reason


@pytest.mark.asyncio
async def test_reject_when_halted(store, risk_config):
    engine = RiskEngine(risk_config, store)
    engine.halt("Test halt")
    result = await engine.check(make_order(size=1.0), make_signal())
    assert not result.approved
    assert "halted" in result.reason.lower()


@pytest.mark.asyncio
async def test_resume_after_halt(store, risk_config):
    engine = RiskEngine(risk_config, store)
    engine.halt("Test")
    engine.resume()
    assert not engine.is_halted
    result = await engine.check(make_order(size=1.0), make_signal())
    assert result.approved


@pytest.mark.asyncio
async def test_rate_limit(store, risk_config):
    engine = RiskEngine(risk_config, store)
    for _ in range(3):
        result = await engine.check(make_order(size=1.0), make_signal())
        assert result.approved
    result = await engine.check(make_order(size=1.0), make_signal())
    assert not result.approved
    assert "Rate limit" in result.reason


@pytest.mark.asyncio
async def test_negative_edge_rejected(store, risk_config):
    engine = RiskEngine(risk_config, store)
    result = await engine.check(make_order(size=1.0), make_signal(edge=-0.01))
    assert not result.approved
    assert "Negative edge" in result.reason


@pytest.mark.asyncio
async def test_stale_feed_veto(store, risk_config):
    engine = RiskEngine(risk_config, store)
    engine._last_book_update["tok1"] = datetime.now(UTC) - timedelta(seconds=60)
    result = await engine.check(make_order(token_id="tok1"), make_signal(token_id="tok1"))
    assert not result.approved
    assert "Stale feed" in result.reason


@pytest.mark.asyncio
async def test_risk_summary(store, risk_config):
    engine = RiskEngine(risk_config, store)
    summary = await engine.get_risk_summary()
    assert summary["halted"] is False
    assert summary["total_exposure"] == 0.0
    assert summary["max_positions"] == 5
