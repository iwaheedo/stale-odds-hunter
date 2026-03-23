from __future__ import annotations

import json
from datetime import datetime, timezone

from src.domain.enums import Side
from src.domain.models import Market, OrderBookSnapshot, PriceLevel, Signal, Token
from src.settings import Settings
from src.storage.sqlite_store import SQLiteStore
from src.strategies.base import BaseStrategy
from src.utils.logging import get_logger

logger = get_logger("services.backtest")


class BacktestEngine:
    """Replays historical orderbook snapshots through strategies.

    Runs deterministically on stored data — no live connections needed.
    """

    def __init__(
        self,
        store: SQLiteStore,
        strategies: list[BaseStrategy],
        settings: Settings,
    ) -> None:
        self._store = store
        self._strategies = strategies
        self._settings = settings

    async def run(self, date_from: str, date_to: str) -> dict:
        """Run backtest over the given date range.

        Returns summary metrics dict.
        """
        logger.info("Backtest starting: %s → %s", date_from, date_to)

        # Load all markets
        markets = await self._store.get_all_markets()
        markets_by_cid = {m.condition_id: m for m in markets}

        # Build token → condition_id mapping
        token_to_cid: dict[str, str] = {}
        for m in markets:
            for t in m.tokens:
                token_to_cid[t.token_id] = m.condition_id

        # Load snapshots in range
        cursor = await self._store.conn.execute(
            """SELECT token_id, timestamp, best_bid, best_ask, midpoint, spread,
                      bids_json, asks_json, bid_depth, ask_depth
               FROM orderbook_snapshots
               WHERE timestamp >= ? AND timestamp <= ?
               ORDER BY timestamp""",
            (date_from, date_to),
        )
        rows = await cursor.fetchall()

        if not rows:
            logger.warning("No snapshot data found in range %s → %s", date_from, date_to)
            return self._empty_result()

        logger.info("Loaded %d snapshots for backtest", len(rows))

        # Simulate
        books: dict[str, OrderBookSnapshot] = {}
        all_signals: list[Signal] = []
        all_fills: list[dict] = []
        pnl_curve: list[float] = []
        cumulative_pnl = 0.0

        for row in rows:
            token_id = row[0]
            timestamp_str = row[1]
            try:
                ts = datetime.fromisoformat(timestamp_str)
            except ValueError:
                continue

            bids = self._parse_levels(row[6])
            asks = self._parse_levels(row[7])

            snap = OrderBookSnapshot(
                token_id=token_id,
                timestamp=ts,
                bids=bids,
                asks=asks,
            )
            books[token_id] = snap

            cid = token_to_cid.get(token_id)
            if not cid:
                continue
            market = markets_by_cid.get(cid)
            if not market:
                continue

            # Get all books for this market's tokens
            market_books = {}
            for t in market.tokens:
                b = books.get(t.token_id)
                if b:
                    market_books[t.token_id] = b

            if len(market_books) < 2:
                continue

            # Run strategies
            for strategy in self._strategies:
                try:
                    signals = await strategy.evaluate(market, market_books, [])
                    for sig in signals:
                        all_signals.append(sig)
                        fill = self._simulate_fill(sig, market_books)
                        if fill:
                            all_fills.append(fill)
                            cumulative_pnl += fill["edge_captured"]
                            pnl_curve.append(cumulative_pnl)
                except Exception:
                    logger.exception("Strategy %s failed during backtest", strategy.name)

        # Compute metrics
        markets_tested = len({token_to_cid.get(r[0]) for r in rows if token_to_cid.get(r[0])})
        win_count = sum(1 for f in all_fills if f["edge_captured"] > 0)
        total_fills = len(all_fills)
        win_rate = win_count / total_fills if total_fills > 0 else 0.0
        avg_edge = sum(s.edge for s in all_signals) / len(all_signals) if all_signals else 0.0
        max_drawdown = self._compute_max_drawdown(pnl_curve)

        result = {
            "markets_tested": markets_tested,
            "total_signals": len(all_signals),
            "total_trades": len(all_signals),
            "total_fills": total_fills,
            "win_rate": win_rate,
            "total_pnl": cumulative_pnl,
            "avg_edge": avg_edge,
            "max_drawdown": max_drawdown,
            "pnl_curve": pnl_curve,
        }

        logger.info("Backtest complete: %d signals, %d fills, PnL=$%.2f, WR=%.1f%%",
                     len(all_signals), total_fills, cumulative_pnl, win_rate * 100)
        return result

    def _simulate_fill(self, signal: Signal, books: dict[str, OrderBookSnapshot]) -> dict | None:
        """Simple fill simulation for backtest."""
        book = books.get(signal.token_id)
        if not book:
            return None

        if signal.side == Side.BUY:
            if not book.asks:
                return None
            fill_price = book.best_ask
            edge_captured = signal.fair_value - fill_price
        else:
            if not book.bids:
                return None
            fill_price = book.best_bid
            edge_captured = fill_price - signal.fair_value

        # Only count as fill if the order would have been marketable
        if signal.side == Side.BUY and signal.market_price < book.best_ask:
            return None
        if signal.side == Side.SELL and signal.market_price > book.best_bid:
            return None

        return {
            "signal_id": signal.id,
            "token_id": signal.token_id,
            "side": signal.side.value,
            "fill_price": fill_price,
            "edge_captured": edge_captured,
            "timestamp": signal.timestamp.isoformat(),
        }

    def _parse_levels(self, json_str: str | None) -> list[PriceLevel]:
        if not json_str:
            return []
        try:
            data = json.loads(json_str)
            return [PriceLevel(price=float(d["price"]), size=float(d["size"])) for d in data]
        except (json.JSONDecodeError, KeyError, TypeError):
            return []

    def _compute_max_drawdown(self, pnl_curve: list[float]) -> float:
        if not pnl_curve:
            return 0.0
        peak = pnl_curve[0]
        max_dd = 0.0
        for pnl in pnl_curve:
            if pnl > peak:
                peak = pnl
            dd = peak - pnl
            if dd > max_dd:
                max_dd = dd
        return max_dd

    def _empty_result(self) -> dict:
        return {
            "markets_tested": 0, "total_signals": 0, "total_trades": 0,
            "total_fills": 0, "win_rate": 0.0, "total_pnl": 0.0,
            "avg_edge": 0.0, "max_drawdown": 0.0, "pnl_curve": [],
        }
