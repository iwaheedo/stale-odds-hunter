#!/usr/bin/env python3
"""Autoresearch experiment runner.

Runs the trading bot for a fixed duration in paper mode,
then extracts and reports metrics from the experiment database.

Usage:
    python experiment.py --duration 10
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
EXPERIMENT_DB = REPO_ROOT / "data" / "sqlite" / "experiment.db"
EXPERIMENT_LOG = REPO_ROOT / "data" / "experiment_last.log"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a trading experiment")
    parser.add_argument(
        "--duration",
        type=int,
        default=10,
        help="Experiment duration in minutes (default: 10)",
    )
    return parser.parse_args()


def clean_db() -> None:
    """Delete experiment DB for a clean slate."""
    for suffix in ("", "-shm", "-wal"):
        p = Path(str(EXPERIMENT_DB) + suffix)
        p.unlink(missing_ok=True)


def run_bot(duration_minutes: int) -> bool:
    """Run the bot as a subprocess for exactly N minutes. Returns True if clean exit."""
    # Ensure data directory exists
    EXPERIMENT_DB.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["SOH_APP__SQLITE_DB_PATH"] = str(EXPERIMENT_DB)
    env["LOG_LEVEL"] = "INFO"

    # Use run_bot_headless directly — no API server, no port conflicts
    bot_code = (
        "import asyncio; "
        "from src.main import run_bot_headless; "
        "asyncio.run(run_bot_headless())"
    )

    print(f"Starting bot for {duration_minutes} minutes...", file=sys.stderr)

    with open(EXPERIMENT_LOG, "w") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-c", bot_code],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )

    try:
        time.sleep(duration_minutes * 60)
    except KeyboardInterrupt:
        print("Interrupted early, shutting down bot...", file=sys.stderr)

    # Graceful shutdown
    print("Sending SIGINT to bot...", file=sys.stderr)
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        print("Bot didn't stop in 30s, killing...", file=sys.stderr)
        proc.kill()
        proc.wait(timeout=10)

    crashed = proc.returncode not in (0, -2, -signal.SIGINT)
    if crashed:
        print(f"Bot exited with code {proc.returncode}", file=sys.stderr)
    else:
        print("Bot shut down cleanly.", file=sys.stderr)
    return not crashed


def extract_metrics(duration_minutes: int) -> dict:
    """Extract metrics from the experiment database."""
    if not EXPERIMENT_DB.exists():
        return {
            "duration_minutes": duration_minutes,
            "bot_crashed": True,
            "error": "No experiment database found",
        }

    db = sqlite3.connect(str(EXPERIMENT_DB))
    db.row_factory = sqlite3.Row

    def scalar(sql: str, default: float = 0) -> float:
        row = db.execute(sql).fetchone()
        return float(row[0]) if row and row[0] is not None else default

    signals = int(scalar("SELECT COUNT(*) FROM signals"))
    orders = int(scalar("SELECT COUNT(*) FROM orders"))
    fills = int(scalar("SELECT COUNT(*) FROM fills"))
    rejected = int(scalar("SELECT COUNT(*) FROM orders WHERE status = 'REJECTED'"))
    exits = int(scalar("SELECT COUNT(*) FROM orders WHERE signal_id = 'EXIT'"))

    pos_row = db.execute(
        "SELECT "
        "  COUNT(CASE WHEN size > 0 THEN 1 END), "
        "  COALESCE(SUM(realized_pnl), 0), "
        "  COALESCE(SUM(unrealized_pnl), 0), "
        "  COALESCE(SUM(size * avg_entry), 0) "
        "FROM positions"
    ).fetchone()

    open_positions = pos_row[0] if pos_row else 0
    realized_pnl = float(pos_row[1]) if pos_row else 0.0
    unrealized_pnl = float(pos_row[2]) if pos_row else 0.0
    exposure = float(pos_row[3]) if pos_row else 0.0
    total_pnl = realized_pnl + unrealized_pnl

    fees = float(scalar("SELECT COALESCE(SUM(fee_estimate), 0) FROM fills", 0.0))

    avg_edge = float(
        scalar(
            "SELECT AVG(s.edge) FROM signals s "
            "JOIN orders o ON o.signal_id = s.id "
            "WHERE o.status = 'FILLED'",
            0.0,
        )
    )

    fill_rate = fills / orders if orders > 0 else 0.0
    pnl_per_fill = total_pnl / fills if fills > 0 else 0.0

    db.close()

    return {
        "duration_minutes": duration_minutes,
        "signals": signals,
        "orders": orders,
        "fills": fills,
        "exits": exits,
        "rejected": rejected,
        "open_positions": open_positions,
        "realized_pnl": round(realized_pnl, 4),
        "unrealized_pnl": round(unrealized_pnl, 4),
        "total_pnl": round(total_pnl, 4),
        "total_fees": round(fees, 4),
        "exposure_usd": round(exposure, 2),
        "fill_rate": round(fill_rate, 3),
        "avg_edge_claimed": round(avg_edge, 4),
        "pnl_per_fill": round(pnl_per_fill, 4),
        "bot_crashed": False,
    }


def main() -> None:
    args = parse_args()

    clean_db()
    crashed = not run_bot(args.duration)

    if crashed and not EXPERIMENT_DB.exists():
        result = {
            "duration_minutes": args.duration,
            "bot_crashed": True,
            "total_pnl": 0.0,
            "fills": 0,
        }
    else:
        result = extract_metrics(args.duration)
        result["bot_crashed"] = crashed

    # JSON to stdout (machine-readable)
    print(json.dumps(result, indent=2))

    # Human summary to stderr
    print("\n--- Experiment Summary ---", file=sys.stderr)
    print(f"  Duration:    {result['duration_minutes']} min", file=sys.stderr)
    print(f"  Total P&L:   ${result.get('total_pnl', 0):+.4f}", file=sys.stderr)
    print(f"  Realized:    ${result.get('realized_pnl', 0):+.4f}", file=sys.stderr)
    print(f"  Unrealized:  ${result.get('unrealized_pnl', 0):+.4f}", file=sys.stderr)
    print(f"  Fills:       {result.get('fills', 0)}", file=sys.stderr)
    print(f"  Exits:       {result.get('exits', 0)}", file=sys.stderr)
    print(f"  Open Pos:    {result.get('open_positions', 0)}", file=sys.stderr)
    print(f"  Fill Rate:   {result.get('fill_rate', 0):.1%}", file=sys.stderr)
    print(f"  Avg Edge:    {result.get('avg_edge_claimed', 0):.4f}", file=sys.stderr)
    print(f"  P&L/Fill:    ${result.get('pnl_per_fill', 0):+.4f}", file=sys.stderr)
    print(f"  Crashed:     {result.get('bot_crashed', False)}", file=sys.stderr)


if __name__ == "__main__":
    main()
