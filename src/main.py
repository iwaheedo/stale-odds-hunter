from __future__ import annotations

import argparse
import asyncio
import sys

import httpx
import uvicorn

from src.adapters.geoblock import check_geoblock
from src.adapters.polymarket_public import PolymarketPublicClient
from src.adapters.websocket_client import PolymarketWebSocket
from src.api import app as fastapi_app, configure as configure_api
from src.domain.events import EventBus, MarketDiscovered
from src.services.market_discovery import MarketDiscoveryService
from src.services.market_state import MarketStateService
from src.services.paper_execution import PaperExecutionService
from src.services.portfolio_engine import PortfolioEngine
from src.services.risk_engine import RiskEngine
from src.services.signal_engine import SignalEngine
from src.settings import load_settings
from src.storage.sqlite_store import SQLiteStore
from src.strategies.stale_odds import StaleOddsStrategy
from src.utils.logging import get_logger, setup_logging


async def run_bot() -> None:
    settings = load_settings()
    setup_logging(settings.app.log_level, settings.app.log_format)
    logger = get_logger("main")

    if settings.is_live:
        logger.info("Starting Stale Odds Hunter in LIVE mode")
    else:
        logger.info("Starting Stale Odds Hunter in paper mode")

    event_bus = EventBus()
    store = SQLiteStore(settings.app.sqlite_db_path)
    await store.initialize()

    http_client = httpx.AsyncClient(timeout=15.0)
    poly_client = PolymarketPublicClient(http_client)

    blocked = await check_geoblock(http_client)
    if blocked:
        logger.warning("Geoblock detected — continuing in paper mode, some data may be limited")

    # Build services
    market_state = MarketStateService(store, event_bus, http_client=poly_client)
    ws_client = PolymarketWebSocket(event_bus, settings)
    discovery = MarketDiscoveryService(poly_client, settings, event_bus)
    risk_engine = RiskEngine(settings.risk, store, event_bus=event_bus, market_state=market_state)
    portfolio = PortfolioEngine(store, event_bus)

    # Build strategies
    strategies = []
    if "stale_odds" in settings.strategies.enabled_strategies:
        strategies.append(StaleOddsStrategy(settings.strategies))
    logger.info("Loaded strategies: %s", [s.name for s in strategies])

    signal_engine = SignalEngine(strategies, market_state, store, event_bus)

    # Execution — live or paper
    if settings.is_live:
        from src.services.live_execution import LiveExecutionService
        execution = LiveExecutionService(market_state, risk_engine, store, event_bus, settings)
    else:
        execution = PaperExecutionService(market_state, risk_engine, store, event_bus, settings)

    configure_api(signal_engine, risk_engine, portfolio)

    async def on_market_discovered() -> None:
        q = event_bus.subscribe(MarketDiscovered)
        while True:
            event: MarketDiscovered = await q.get()
            token_ids = [t.token_id for t in event.market.tokens]
            await ws_client.subscribe(token_ids)

    api_config = uvicorn.Config(
        fastapi_app, host=settings.app.api_host, port=settings.app.api_port, log_level="warning",
    )
    api_server = uvicorn.Server(api_config)

    tasks = [
        asyncio.create_task(discovery.run(), name="discovery"),
        asyncio.create_task(ws_client.run(), name="websocket"),
        asyncio.create_task(market_state.run(), name="market_state"),
        asyncio.create_task(signal_engine.run(), name="signal_engine"),
        asyncio.create_task(execution.run(), name="execution"),
        asyncio.create_task(portfolio.run(), name="portfolio"),
        asyncio.create_task(risk_engine.run_monitor(), name="risk_monitor"),
        asyncio.create_task(on_market_discovered(), name="ws_subscriber"),
        asyncio.create_task(api_server.serve(), name="api"),
    ]

    logger.info("All services started — API at http://%s:%d", settings.app.api_host, settings.app.api_port)

    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for t in done:
            exc = t.exception()
            if exc:
                logger.error("Task %s failed: %s", t.get_name(), exc)
                raise exc
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutdown requested")
    finally:
        logger.info("Shutting down...")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await http_client.aclose()
        await store.close()
        logger.info("Shutdown complete")


async def discover_markets_once() -> None:
    settings = load_settings()
    setup_logging(settings.app.log_level, "text")
    logger = get_logger("main")

    http_client = httpx.AsyncClient(timeout=15.0)
    poly_client = PolymarketPublicClient(http_client)
    event_bus = EventBus()
    discovery = MarketDiscoveryService(poly_client, settings, event_bus)

    markets = await discovery.discover_once()
    logger.info("Found %d markets:", len(markets))
    for m in markets:
        print(f"  {m.slug:40s} liq={m.liquidity:>10.0f}  vol24h={m.volume_24h:>10.0f}  {m.question[:60]}")
    await http_client.aclose()


async def run_backtest(strategy: str, date_from: str, date_to: str) -> None:
    settings = load_settings()
    setup_logging(settings.app.log_level, "text")
    logger = get_logger("backtest")

    from src.services.backtest_engine import BacktestEngine
    store = SQLiteStore(settings.app.sqlite_db_path)
    await store.initialize()

    strategies_list = []
    if strategy == "stale_odds" or strategy == "all":
        strategies_list.append(StaleOddsStrategy(settings.strategies))

    engine = BacktestEngine(store, strategies_list, settings)
    result = await engine.run(date_from=date_from, date_to=date_to)

    print("\n" + "=" * 60)
    print("  BACKTEST RESULTS")
    print("=" * 60)
    print(f"  Period:        {date_from} → {date_to}")
    print(f"  Strategy:      {strategy}")
    print(f"  Markets:       {result['markets_tested']}")
    print(f"  Signals:       {result['total_signals']}")
    print(f"  Trades:        {result['total_trades']}")
    print(f"  Fills:         {result['total_fills']}")
    print(f"  Win Rate:      {result['win_rate']:.1%}")
    print(f"  Total P&L:     ${result['total_pnl']:+.2f}")
    print(f"  Avg Edge:      {result['avg_edge']:.4f}")
    print(f"  Max Drawdown:  ${result['max_drawdown']:.2f}")
    print("=" * 60)

    await store.close()


async def flatten_positions() -> None:
    """Emergency halt — marks all positions as flattened."""
    import httpx as hx
    try:
        resp = hx.post("http://127.0.0.1:8000/risk/halt", timeout=5.0)
        if resp.status_code == 200:
            print("HALT: Trading halted successfully via API")
        else:
            print(f"HALT FAILED: API returned {resp.status_code}")
    except Exception as e:
        print(f"HALT FAILED: Could not reach API — {e}")
        print("Make sure the bot is running (make run)")


def cli() -> None:
    parser = argparse.ArgumentParser(description="Stale Odds Hunter")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    subparsers.add_parser("run", help="Start the bot")
    subparsers.add_parser("discover-markets", help="One-shot market discovery")
    subparsers.add_parser("paper-trade", help="Start bot in paper mode (alias for run)")
    subparsers.add_parser("flatten", help="Emergency halt — cancel all positions")

    bt_parser = subparsers.add_parser("backtest", help="Run backtest on historical data")
    bt_parser.add_argument("--strategy", default="stale_odds", help="Strategy to test")
    bt_parser.add_argument("--from", dest="date_from", default="2026-01-01", help="Start date")
    bt_parser.add_argument("--to", dest="date_to", default="2026-12-31", help="End date")

    subparsers.add_parser("replay", help="Replay stored market data")

    args = parser.parse_args()

    if args.command in ("run", "paper-trade", None):
        asyncio.run(run_bot())
    elif args.command == "discover-markets":
        asyncio.run(discover_markets_once())
    elif args.command == "flatten":
        asyncio.run(flatten_positions())
    elif args.command == "backtest":
        asyncio.run(run_backtest(args.strategy, args.date_from, args.date_to))
    elif args.command == "replay":
        print("REPLAY: Use backtest with stored data range")
    else:
        parser.print_help()


if __name__ == "__main__":
    cli()
