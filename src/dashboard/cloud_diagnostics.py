"""Cloud diagnostics — test each subsystem independently.

Run from the dashboard's Diagnostics tab to identify exactly which
component fails on Streamlit Cloud vs local.
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DiagnosticResult:
    name: str
    status: str  # PASS, FAIL, WARN
    duration_ms: float = 0.0
    details: list[str] = field(default_factory=list)


def _run_async(coro: object, timeout: float = 30.0):  # type: ignore[return]  # noqa: ANN201
    """Run an async coroutine from sync context using a fresh event loop in a thread."""
    container: dict = {"result": None, "error": None}

    def _target():  # noqa: ANN202
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            container["result"] = loop.run_until_complete(asyncio.wait_for(coro, timeout=timeout))
        except Exception as exc:
            container["error"] = exc
        finally:
            loop.close()

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout + 5)

    if container["error"]:
        raise container["error"]
    return container["result"]


# --- Test 1: Environment ---

def test_environment() -> DiagnosticResult:
    t0 = time.monotonic()
    details = [
        f"Python: {sys.version}",
        f"Platform: {platform.platform()}",
        f"Arch: {platform.machine()}",
        f"CWD: {os.getcwd()}",
        f"PID: {os.getpid()}",
    ]

    project_root = Path(__file__).resolve().parents[2]
    details.append(f"PROJECT_ROOT: {project_root}")
    details.append(f"config/ exists: {(project_root / 'config').is_dir()}")

    for f in ["app.yaml", "markets.yaml", "strategies.yaml", "risk.yaml"]:
        exists = (project_root / "config" / f).exists()
        details.append(f"  {f}: {'OK' if exists else 'MISSING'}")

    # Check env vars
    soh_vars = {k: v for k, v in os.environ.items() if k.startswith("SOH_")}
    details.append(f"SOH env vars: {len(soh_vars)}")
    for k, v in soh_vars.items():
        details.append(f"  {k}={v[:50]}")

    details.append(f"sys.path entries: {len(sys.path)}")

    ms = (time.monotonic() - t0) * 1000
    return DiagnosticResult("Environment", "PASS", ms, details)


# --- Test 2: Filesystem ---

def test_filesystem() -> DiagnosticResult:
    t0 = time.monotonic()
    details = []
    status = "PASS"

    # Test /tmp write
    test_file = Path("/tmp/soh_diag_test_write")
    try:
        test_file.write_text("test_data_12345")
        readback = test_file.read_text()
        test_file.unlink()
        if readback == "test_data_12345":
            details.append("/tmp write: OK")
        else:
            details.append(f"/tmp write: MISMATCH (got {readback!r})")
            status = "FAIL"
    except Exception as exc:
        details.append(f"/tmp write: FAILED ({exc})")
        status = "FAIL"

    # Test flock (SQLite WAL needs this)
    try:
        import fcntl
        lock_file = Path("/tmp/soh_diag_flock_test")
        lock_file.touch()
        with open(lock_file) as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        lock_file.unlink()
        details.append("flock: OK")
    except Exception as exc:
        details.append(f"flock: FAILED ({exc})")
        status = "WARN"

    # Test /mount/src write (expect failure on Cloud)
    mount_src = Path("/mount/src")
    if mount_src.exists():
        try:
            (mount_src / "_diag_test").write_text("x")
            (mount_src / "_diag_test").unlink()
            details.append("/mount/src write: writable (unexpected)")
        except Exception:
            details.append("/mount/src write: read-only (expected on Cloud)")
    else:
        details.append("/mount/src: does not exist (local env)")

    # Disk space
    try:
        stat = os.statvfs("/tmp")
        free_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
        details.append(f"/tmp free space: {free_mb:.0f} MB")
    except Exception as exc:
        details.append(f"/tmp statvfs: {exc}")

    ms = (time.monotonic() - t0) * 1000
    return DiagnosticResult("Filesystem", status, ms, details)


# --- Test 3: SQLite ---

def test_sqlite() -> DiagnosticResult:
    t0 = time.monotonic()
    details = []
    status = "PASS"

    db_path = "/tmp/soh_diag_sqlite_test.db"

    try:
        # Clean up first
        for suffix in ["", "-wal", "-shm"]:
            Path(db_path + suffix).unlink(missing_ok=True)

        conn = sqlite3.connect(db_path)

        # WAL mode
        wal = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        details.append(f"WAL mode: {wal[0] if wal else 'unknown'}")
        if wal and wal[0] != "wal":
            status = "WARN"

        # Create and query
        conn.execute("CREATE TABLE diag_test (id INTEGER PRIMARY KEY, val TEXT)")
        conn.execute("INSERT INTO diag_test VALUES (1, 'hello')")
        conn.commit()
        row = conn.execute("SELECT val FROM diag_test WHERE id=1").fetchone()
        details.append(f"Insert/Select: {'OK' if row and row[0] == 'hello' else 'FAILED'}")

        # Bulk insert latency
        t_bulk = time.monotonic()
        for i in range(100):
            conn.execute("INSERT INTO diag_test VALUES (?, ?)", (i + 100, f"val_{i}"))
        conn.commit()
        bulk_ms = (time.monotonic() - t_bulk) * 1000
        details.append(f"100 inserts: {bulk_ms:.1f}ms")

        # Concurrent read
        conn2 = sqlite3.connect(db_path)
        conn2.execute("PRAGMA journal_mode=WAL")
        row2 = conn2.execute("SELECT COUNT(*) FROM diag_test").fetchone()
        details.append(f"Concurrent read: {row2[0]} rows")
        conn2.close()

        conn.close()

        # Cleanup
        for suffix in ["", "-wal", "-shm"]:
            Path(db_path + suffix).unlink(missing_ok=True)

    except Exception as exc:
        details.append(f"SQLite FAILED: {exc}")
        status = "FAIL"

    ms = (time.monotonic() - t0) * 1000
    return DiagnosticResult("SQLite", status, ms, details)


# --- Test 4: HTTP Connectivity ---

def test_http() -> DiagnosticResult:
    t0 = time.monotonic()
    details = []
    status = "PASS"
    token_id = None  # Will be set if we find a market

    async def _test() -> tuple[str, str | None]:
        nonlocal token_id
        import httpx

        s = "PASS"
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Test Gamma API
            t1 = time.monotonic()
            resp = await client.get("https://gamma-api.polymarket.com/markets?limit=1&active=true")
            gamma_ms = (time.monotonic() - t1) * 1000
            details.append(f"Gamma API: {resp.status_code} ({gamma_ms:.0f}ms)")

            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and data:
                    market = data[0]
                    details.append(f"First market: {market.get('question', '?')[:50]}")
                    clob_ids = market.get("clobTokenIds")
                    if isinstance(clob_ids, str):
                        clob_ids = json.loads(clob_ids)
                    if clob_ids:
                        token_id = clob_ids[0]
                        details.append(f"Token ID: {token_id[:20]}...")
            elif resp.status_code == 403:
                details.append("Gamma API: GEOBLOCKED (403)")
                s = "WARN"

            # Test CLOB API
            if token_id:
                t2 = time.monotonic()
                resp2 = await client.get(
                    f"https://clob.polymarket.com/book?token_id={token_id}"
                )
                clob_ms = (time.monotonic() - t2) * 1000
                details.append(f"CLOB /book: {resp2.status_code} ({clob_ms:.0f}ms)")

                if resp2.status_code == 200:
                    book = resp2.json()
                    bids = len(book.get("bids", []))
                    asks = len(book.get("asks", []))
                    details.append(f"Book depth: {bids} bids, {asks} asks")
                else:
                    details.append(f"CLOB failed: {resp2.text[:100]}")
                    s = "WARN"

        return s, token_id

    try:
        result_status, token_id = _run_async(_test(), timeout=15.0)
        status = result_status
    except Exception as exc:
        details.append(f"HTTP FAILED: {exc}")
        status = "FAIL"

    ms = (time.monotonic() - t0) * 1000
    result = DiagnosticResult("HTTP", status, ms, details)
    result._token_id = token_id  # type: ignore[attr-defined]  # stash for WS test
    return result


# --- Test 5: WebSocket ---

def test_websocket(token_id: str | None = None) -> DiagnosticResult:
    t0 = time.monotonic()
    details = []
    status = "PASS"

    if not token_id:
        details.append("No token ID — run HTTP test first")
        return DiagnosticResult("WebSocket", "WARN", 0, details)

    async def _test() -> str:
        import websockets

        ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        details.append(f"Connecting to {ws_url}")

        t1 = time.monotonic()
        async with websockets.connect(ws_url, ping_interval=None, close_timeout=5) as ws:
            conn_ms = (time.monotonic() - t1) * 1000
            details.append(f"Connected in {conn_ms:.0f}ms")

            # Subscribe
            sub_msg = json.dumps({"assets_ids": [token_id], "type": "market"})
            await ws.send(sub_msg)
            details.append(f"Subscribed to token {token_id[:20]}...")

            # Send PING
            await ws.send("PING")

            # Wait for messages
            msg_count = 0
            data_msg_count = 0
            t_start = time.monotonic()
            timeout = 15.0

            while time.monotonic() - t_start < timeout:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                    msg_count += 1
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")

                    if raw == "PONG":
                        details.append(f"Received PONG (msg #{msg_count})")
                    else:
                        data_msg_count += 1
                        # Preview first data message
                        if data_msg_count <= 2:
                            preview = raw[:150] if len(raw) > 150 else raw
                            details.append(f"Data msg #{data_msg_count}: {preview}")

                    if data_msg_count >= 3:
                        break
                except TimeoutError:
                    details.append(f"No message for 3s (total: {msg_count} msgs, {data_msg_count} data)")

            elapsed = time.monotonic() - t_start
            details.append(f"Total: {msg_count} msgs, {data_msg_count} data msgs in {elapsed:.1f}s")

            if data_msg_count == 0:
                return "FAIL"
            return "PASS"

    try:
        status = _run_async(_test(), timeout=25.0)
    except Exception as exc:
        details.append(f"WebSocket FAILED: {exc}")
        status = "FAIL"

    ms = (time.monotonic() - t0) * 1000
    return DiagnosticResult("WebSocket", status, ms, details)


# --- Test 6: Event Bus ---

def test_event_bus() -> DiagnosticResult:
    t0 = time.monotonic()
    details = []
    status = "PASS"

    async def _test() -> str:
        from src.domain.events import EventBus, OrderBookUpdated
        from src.domain.models import OrderBookSnapshot, PriceLevel
        from src.utils.time import utc_now

        bus = EventBus()
        q = bus.subscribe(OrderBookUpdated)
        details.append(f"Subscriber count: {bus.subscriber_count(OrderBookUpdated)}")

        snap = OrderBookSnapshot(
            token_id="test_token", timestamp=utc_now(),
            bids=[PriceLevel(0.50, 100)], asks=[PriceLevel(0.52, 100)],
        )
        await bus.emit(OrderBookUpdated(snapshot=snap))

        try:
            event = await asyncio.wait_for(q.get(), timeout=2.0)
            details.append(f"Event received: {type(event).__name__}")
            details.append(f"Token ID: {event.snapshot.token_id}")
            details.append(f"Midpoint: {event.snapshot.midpoint}")
            return "PASS"
        except TimeoutError:
            details.append("Event NOT received (timeout)")
            return "FAIL"

    try:
        status = _run_async(_test(), timeout=5.0)
    except Exception as exc:
        details.append(f"Event bus FAILED: {exc}")
        status = "FAIL"

    ms = (time.monotonic() - t0) * 1000
    return DiagnosticResult("Event Bus", status, ms, details)


# --- Test 7: Signal Pipeline ---

def test_signal_pipeline() -> DiagnosticResult:
    t0 = time.monotonic()
    details = []
    status = "PASS"

    async def _test() -> str:
        from src.domain.models import Market, OrderBookSnapshot, PriceLevel, Token
        from src.settings import load_settings
        from src.strategies.stale_odds import StaleOddsStrategy
        from src.utils.time import utc_now

        # Minimal setup — no SQLite needed
        settings = load_settings()
        details.append(f"Settings: entry_edge={settings.strategies.entry_edge_threshold}")

        strategy = StaleOddsStrategy(settings.strategies)
        details.append(f"Strategy: {strategy.name}")

        # Create a fake market with clear mispricing
        market = Market(
            condition_id="test_cid", question="Test market", slug="test",
            tokens=[
                Token(token_id="tok_yes", outcome="Yes", price=0.60),
                Token(token_id="tok_no", outcome="No", price=0.35),
            ],
            category="test", active=True, volume=10000, liquidity=5000,
        )

        # Books with a depth imbalance (should trigger signal)
        yes_book = OrderBookSnapshot(
            token_id="tok_yes", timestamp=utc_now(),
            bids=[PriceLevel(0.59, 50000)], asks=[PriceLevel(0.61, 100)],
        )
        no_book = OrderBookSnapshot(
            token_id="tok_no", timestamp=utc_now(),
            bids=[PriceLevel(0.34, 100)], asks=[PriceLevel(0.36, 50000)],
        )

        books = {"tok_yes": yes_book, "tok_no": no_book}
        details.append(f"YES: bid_depth={yes_book.bid_depth}, ask_depth={yes_book.ask_depth}")
        details.append(f"NO: bid_depth={no_book.bid_depth}, ask_depth={no_book.ask_depth}")
        details.append(f"Complement: {yes_book.midpoint + no_book.midpoint:.4f}")

        signals = await strategy.evaluate(market, books, [])
        details.append(f"Signals generated: {len(signals)}")

        for sig in signals:
            details.append(f"  {sig.side.value} edge={sig.edge:.4f} conf={sig.confidence:.2f} | {sig.rationale[:60]}")

        return "PASS" if signals else "WARN"

    try:
        status = _run_async(_test(), timeout=10.0)
    except Exception as exc:
        details.append(f"Signal pipeline FAILED: {exc}")
        status = "FAIL"

    ms = (time.monotonic() - t0) * 1000
    return DiagnosticResult("Signal Pipeline", status, ms, details)


# --- Run All ---

def run_all() -> list[DiagnosticResult]:
    """Run all diagnostics in sequence. Returns list of results."""
    results = []

    results.append(test_environment())
    results.append(test_filesystem())
    results.append(test_sqlite())

    http_result = test_http()
    results.append(http_result)

    # Pass token_id from HTTP test to WebSocket test
    token_id = getattr(http_result, "_token_id", None)
    results.append(test_websocket(token_id))

    results.append(test_event_bus())
    results.append(test_signal_pipeline())

    return results


def format_report(results: list[DiagnosticResult]) -> str:
    """Format results as plain text for copy-paste."""
    from src.utils.time import utc_now

    lines = [
        "=== STALE ODDS HUNTER — CLOUD DIAGNOSTICS ===",
        f"Timestamp: {utc_now().isoformat()}",
        f"Python: {sys.version.split()[0]} ({platform.platform()})",
        "",
    ]

    for r in results:
        icon = {"PASS": "[PASS]", "FAIL": "[FAIL]", "WARN": "[WARN]"}.get(r.status, "[????]")
        lines.append(f"{icon} {r.name} ({r.duration_ms:.0f}ms)")
        for d in r.details:
            lines.append(f"  {d}")
        lines.append("")

    passed = sum(1 for r in results if r.status == "PASS")
    failed = sum(1 for r in results if r.status == "FAIL")
    warned = sum(1 for r in results if r.status == "WARN")
    lines.append(f"Summary: {passed} passed, {warned} warnings, {failed} failed")

    return "\n".join(lines)
