from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

from src.utils.logging import get_logger

logger = get_logger("storage.duckdb")


class DuckDBAnalytics:
    """Analytical queries on historical data using DuckDB."""

    def __init__(self, duckdb_path: str, sqlite_path: str) -> None:
        Path(duckdb_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(duckdb_path)
        self._sqlite_path = sqlite_path
        self._attached = False

    def close(self) -> None:
        self._conn.close()

    def _ensure_attached(self) -> None:
        if not self._attached:
            try:
                self._conn.execute(f"ATTACH '{self._sqlite_path}' AS soh (TYPE sqlite, READ_ONLY)")
                self._attached = True
            except duckdb.BinderException:
                pass  # Already attached

    def sync_from_sqlite(self) -> dict:
        """Copy data from SQLite into DuckDB for fast analytics."""
        self._ensure_attached()

        tables = ["orderbook_snapshots", "signals", "orders", "fills", "positions",
                  "markets", "risk_events"]
        counts = {}
        for table in tables:
            try:
                self._conn.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM soh.{table}")
                result = self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                counts[table] = result[0] if result else 0
            except Exception as exc:
                logger.warning("Failed to sync table %s: %s", table, exc)
                counts[table] = 0

        logger.info("DuckDB sync complete: %s", counts)
        return counts

    def query_signal_performance(self, strategy: str | None = None,
                                  date_from: str | None = None,
                                  date_to: str | None = None) -> pd.DataFrame:
        """Signal win rate and edge by strategy."""
        where = ["1=1"]
        params: list = []
        if strategy:
            where.append("s.strategy = ?")
            params.append(strategy)
        if date_from:
            where.append("s.timestamp >= ?")
            params.append(date_from)
        if date_to:
            where.append("s.timestamp <= ?")
            params.append(date_to)

        query = f"""
            SELECT
                s.strategy,
                COUNT(*) as signal_count,
                AVG(s.edge) as avg_edge,
                AVG(s.confidence) as avg_confidence,
                SUM(CASE WHEN o.status = 'FILLED' THEN 1 ELSE 0 END) as fills,
                SUM(CASE WHEN o.status = 'REJECTED' THEN 1 ELSE 0 END) as rejections,
                AVG(CASE WHEN o.fill_price IS NOT NULL THEN o.fill_price - o.price ELSE NULL END) as avg_slippage
            FROM signals s
            LEFT JOIN orders o ON s.id = o.signal_id
            WHERE {' AND '.join(where)}
            GROUP BY s.strategy
        """
        return self._conn.execute(query, params).fetchdf()

    def query_pnl_timeseries(self, date_from: str | None = None,
                              date_to: str | None = None) -> pd.DataFrame:
        """P&L over time from fills."""
        where = ["1=1"]
        params: list = []
        if date_from:
            where.append("f.timestamp >= ?")
            params.append(date_from)
        if date_to:
            where.append("f.timestamp <= ?")
            params.append(date_to)

        query = f"""
            SELECT
                f.timestamp,
                f.fill_price,
                f.fill_size,
                f.fee_estimate,
                o.side,
                o.price as intended_price,
                m.question
            FROM fills f
            JOIN orders o ON f.order_id = o.id
            LEFT JOIN markets m ON o.market_condition_id = m.condition_id
            WHERE {' AND '.join(where)}
            ORDER BY f.timestamp
        """
        return self._conn.execute(query, params).fetchdf()

    def query_market_activity(self, condition_id: str) -> pd.DataFrame:
        """Price history and signal count for a specific market."""
        return self._conn.execute("""
            SELECT
                os.timestamp,
                os.midpoint,
                os.spread,
                os.bid_depth,
                os.ask_depth
            FROM orderbook_snapshots os
            JOIN markets m ON os.token_id IN (
                SELECT json_extract_string(value, '$.token_id')
                FROM (SELECT unnest(json(m2.tokens_json)) as value FROM markets m2 WHERE m2.condition_id = ?)
            )
            WHERE 1=1
            ORDER BY os.timestamp
        """, [condition_id]).fetchdf()

    def query_snapshot_range(self, date_from: str, date_to: str) -> pd.DataFrame:
        """Get all orderbook snapshots in a date range for backtesting."""
        self._ensure_attached()
        return self._conn.execute("""
            SELECT token_id, timestamp, best_bid, best_ask, midpoint, spread,
                   bids_json, asks_json, bid_depth, ask_depth
            FROM soh.orderbook_snapshots
            WHERE timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp
        """, [date_from, date_to]).fetchdf()

    def query_markets_with_data(self, date_from: str, date_to: str) -> pd.DataFrame:
        """Get markets that have snapshot data in the given range."""
        self._ensure_attached()
        return self._conn.execute("""
            SELECT DISTINCT m.condition_id, m.question, m.slug, m.tokens_json, m.fees_enabled
            FROM soh.markets m
            JOIN soh.orderbook_snapshots os ON os.token_id IN (
                SELECT json_extract_string(value, '$.token_id')
                FROM (SELECT unnest(json(m.tokens_json)) as value)
            )
            WHERE os.timestamp >= ? AND os.timestamp <= ?
              AND m.active = 1
        """, [date_from, date_to]).fetchdf()
