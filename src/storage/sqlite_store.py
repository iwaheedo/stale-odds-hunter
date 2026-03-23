from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from src.domain.enums import OrderStatus, Side
from src.domain.models import (
    Fill,
    Market,
    Order,
    OrderBookSnapshot,
    Position,
    Signal,
)
from src.utils.logging import get_logger

logger = get_logger("storage.sqlite")


class SQLiteStore:
    """Async SQLite storage layer. All queries use parameterized statements."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._create_tables()
        logger.info("SQLite initialized at %s", self._db_path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("SQLiteStore not initialized")
        return self._conn

    # --- Schema ---

    async def _create_tables(self) -> None:
        await self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS markets (
                condition_id TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                slug TEXT,
                category TEXT,
                tokens_json TEXT,
                active INTEGER DEFAULT 1,
                volume REAL DEFAULT 0,
                volume_24h REAL DEFAULT 0,
                liquidity REAL DEFAULT 0,
                fees_enabled INTEGER DEFAULT 0,
                neg_risk INTEGER DEFAULT 0,
                end_date TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS orderbook_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                best_bid REAL,
                best_ask REAL,
                midpoint REAL,
                spread REAL,
                bids_json TEXT,
                asks_json TEXT,
                bid_depth REAL,
                ask_depth REAL
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_token_ts
                ON orderbook_snapshots(token_id, timestamp);

            CREATE TABLE IF NOT EXISTS trades_tape (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                side TEXT,
                price REAL NOT NULL,
                size REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_trades_token_ts
                ON trades_tape(token_id, timestamp);

            CREATE TABLE IF NOT EXISTS signals (
                id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                market_condition_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                strategy TEXT NOT NULL,
                side TEXT NOT NULL,
                fair_value REAL NOT NULL,
                market_price REAL NOT NULL,
                edge REAL NOT NULL,
                confidence REAL NOT NULL,
                rationale TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_signals_ts
                ON signals(timestamp DESC);

            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                signal_id TEXT,
                token_id TEXT NOT NULL,
                market_condition_id TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                price REAL NOT NULL,
                size REAL NOT NULL,
                status TEXT NOT NULL,
                is_paper INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                filled_at TEXT,
                fill_price REAL,
                fill_size REAL,
                reject_reason TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_orders_status
                ON orders(status);

            CREATE TABLE IF NOT EXISTS fills (
                id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                fill_price REAL NOT NULL,
                fill_size REAL NOT NULL,
                fee_estimate REAL DEFAULT 0,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(id)
            );

            CREATE TABLE IF NOT EXISTS positions (
                token_id TEXT PRIMARY KEY,
                condition_id TEXT NOT NULL,
                side TEXT NOT NULL,
                size REAL DEFAULT 0,
                avg_entry REAL DEFAULT 0,
                realized_pnl REAL DEFAULT 0,
                unrealized_pnl REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS risk_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                severity TEXT NOT NULL,
                event_type TEXT NOT NULL,
                details_json TEXT
            );

            CREATE TABLE IF NOT EXISTS config_versions (
                version INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                config_blob TEXT NOT NULL
            );
        """)
        await self.conn.commit()

    # --- Markets ---

    async def upsert_market(self, market: Market) -> None:
        tokens_json = json.dumps([
            {"token_id": t.token_id, "outcome": t.outcome, "price": t.price}
            for t in market.tokens
        ])
        await self.conn.execute(
            """INSERT INTO markets (condition_id, question, slug, category, tokens_json,
                active, volume, volume_24h, liquidity, fees_enabled, neg_risk, end_date, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(condition_id) DO UPDATE SET
                question=excluded.question, slug=excluded.slug, category=excluded.category,
                tokens_json=excluded.tokens_json, active=excluded.active,
                volume=excluded.volume, volume_24h=excluded.volume_24h,
                liquidity=excluded.liquidity, fees_enabled=excluded.fees_enabled,
                neg_risk=excluded.neg_risk, end_date=excluded.end_date,
                updated_at=excluded.updated_at""",
            (market.condition_id, market.question, market.slug, market.category,
             tokens_json, int(market.active), market.volume, market.volume_24h,
             market.liquidity, int(market.fees_enabled), int(market.neg_risk),
             market.end_date.isoformat() if market.end_date else None,
             datetime.now(timezone.utc).isoformat()),
        )
        await self.conn.commit()

    async def get_all_markets(self) -> list[Market]:
        cursor = await self.conn.execute(
            "SELECT condition_id, question, slug, category, tokens_json, active, "
            "volume, volume_24h, liquidity, fees_enabled, neg_risk, end_date "
            "FROM markets WHERE active = 1"
        )
        rows = await cursor.fetchall()
        markets = []
        for row in rows:
            from src.domain.models import Token
            tokens_data = json.loads(row[4]) if row[4] else []
            tokens = [Token(token_id=t["token_id"], outcome=t["outcome"], price=t.get("price", 0))
                      for t in tokens_data]
            end_date = None
            if row[11]:
                try:
                    end_date = datetime.fromisoformat(row[11])
                except ValueError:
                    pass
            markets.append(Market(
                condition_id=row[0], question=row[1], slug=row[2], category=row[3],
                tokens=tokens, active=bool(row[5]), volume=row[6], volume_24h=row[7],
                liquidity=row[8], fees_enabled=bool(row[9]), neg_risk=bool(row[10]),
                end_date=end_date,
            ))
        return markets

    # --- Order Book Snapshots ---

    async def insert_orderbook_snapshot(self, snap: OrderBookSnapshot) -> None:
        bids_json = json.dumps([{"price": b.price, "size": b.size} for b in snap.bids])
        asks_json = json.dumps([{"price": a.price, "size": a.size} for a in snap.asks])
        await self.conn.execute(
            """INSERT INTO orderbook_snapshots
            (token_id, timestamp, best_bid, best_ask, midpoint, spread,
             bids_json, asks_json, bid_depth, ask_depth)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (snap.token_id, snap.timestamp.isoformat(), snap.best_bid, snap.best_ask,
             snap.midpoint, snap.spread, bids_json, asks_json,
             snap.bid_depth, snap.ask_depth),
        )
        await self.conn.commit()

    # --- Trades ---

    async def insert_trade(self, token_id: str, timestamp: float,
                           side: str, price: float, size: float) -> None:
        await self.conn.execute(
            "INSERT INTO trades_tape (token_id, timestamp, side, price, size) VALUES (?, ?, ?, ?, ?)",
            (token_id, timestamp, side, price, size),
        )
        await self.conn.commit()

    # --- Signals ---

    async def insert_signal(self, signal: Signal) -> None:
        await self.conn.execute(
            """INSERT INTO signals (id, timestamp, market_condition_id, token_id, strategy,
                side, fair_value, market_price, edge, confidence, rationale)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (signal.id, signal.timestamp.isoformat(), signal.market_condition_id,
             signal.token_id, signal.strategy, signal.side.value, signal.fair_value,
             signal.market_price, signal.edge, signal.confidence, signal.rationale),
        )
        await self.conn.commit()

    # --- Orders ---

    async def insert_order(self, order: Order) -> None:
        await self.conn.execute(
            """INSERT INTO orders (id, signal_id, token_id, market_condition_id, side,
                order_type, price, size, status, is_paper, created_at, reject_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (order.id, order.signal_id, order.token_id, order.market_condition_id,
             order.side.value, order.order_type, order.price, order.size,
             order.status.value, int(order.is_paper), order.created_at.isoformat(),
             order.reject_reason),
        )
        await self.conn.commit()

    async def update_order_fill(self, order_id: str, fill_price: float,
                                fill_size: float, filled_at: datetime) -> None:
        await self.conn.execute(
            """UPDATE orders SET status = ?, fill_price = ?, fill_size = ?, filled_at = ?
            WHERE id = ?""",
            (OrderStatus.FILLED.value, fill_price, fill_size, filled_at.isoformat(), order_id),
        )
        await self.conn.commit()

    async def update_order_status(self, order_id: str, status: OrderStatus,
                                  reject_reason: str = "") -> None:
        await self.conn.execute(
            "UPDATE orders SET status = ?, reject_reason = ? WHERE id = ?",
            (status.value, reject_reason, order_id),
        )
        await self.conn.commit()

    # --- Fills ---

    async def insert_fill(self, fill: Fill) -> None:
        await self.conn.execute(
            """INSERT INTO fills (id, order_id, fill_price, fill_size, fee_estimate, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (fill.id, fill.order_id, fill.fill_price, fill.fill_size,
             fill.fee_estimate, fill.timestamp.isoformat()),
        )
        await self.conn.commit()

    # --- Positions ---

    async def upsert_position(self, position: Position) -> None:
        await self.conn.execute(
            """INSERT INTO positions (token_id, condition_id, side, size, avg_entry,
                realized_pnl, unrealized_pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(token_id) DO UPDATE SET
                side=excluded.side, size=excluded.size, avg_entry=excluded.avg_entry,
                realized_pnl=excluded.realized_pnl, unrealized_pnl=excluded.unrealized_pnl""",
            (position.token_id, position.condition_id, position.side.value,
             position.size, position.avg_entry, position.realized_pnl,
             position.unrealized_pnl),
        )
        await self.conn.commit()

    async def get_open_positions(self) -> list[Position]:
        cursor = await self.conn.execute(
            "SELECT token_id, condition_id, side, size, avg_entry, realized_pnl, unrealized_pnl "
            "FROM positions WHERE size > 0"
        )
        rows = await cursor.fetchall()
        return [
            Position(
                token_id=r[0], condition_id=r[1], side=Side(r[2]),
                size=r[3], avg_entry=r[4], realized_pnl=r[5], unrealized_pnl=r[6],
            )
            for r in rows
        ]

    async def get_all_positions(self) -> list[Position]:
        cursor = await self.conn.execute(
            "SELECT token_id, condition_id, side, size, avg_entry, realized_pnl, unrealized_pnl "
            "FROM positions"
        )
        rows = await cursor.fetchall()
        return [
            Position(
                token_id=r[0], condition_id=r[1], side=Side(r[2]),
                size=r[3], avg_entry=r[4], realized_pnl=r[5], unrealized_pnl=r[6],
            )
            for r in rows
        ]

    # --- Risk Events ---

    async def insert_risk_event(self, severity: str, event_type: str,
                                details: dict) -> None:
        await self.conn.execute(
            "INSERT INTO risk_events (timestamp, severity, event_type, details_json) VALUES (?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), severity, event_type, json.dumps(details)),
        )
        await self.conn.commit()

    # --- Config Versions ---

    async def save_config_version(self, config_blob: str) -> None:
        await self.conn.execute(
            "INSERT INTO config_versions (timestamp, config_blob) VALUES (?, ?)",
            (datetime.now(timezone.utc).isoformat(), config_blob),
        )
        await self.conn.commit()
