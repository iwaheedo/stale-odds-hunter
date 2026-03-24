"""Bot runner — holds bot thread state in an imported module.

Streamlit re-executes the main script on every rerun, which resets
module-level variables. But imported modules are cached by Python's
import system, so their state persists. This module holds the bot
thread, stop event, and log lines so they survive Streamlit reruns.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import threading
from pathlib import Path

# --- Persistent state (survives Streamlit reruns) ---
bot_thread: threading.Thread | None = None
bot_stop_event: asyncio.Event | None = None
bot_loop: asyncio.AbstractEventLoop | None = None
log_lines: list[str] = []
MAX_LOG_LINES = 200
SENTINEL = Path("/tmp/soh_bot_running")


class BotLogHandler(logging.Handler):
    """Captures log lines for the dashboard."""
    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            log_lines.append(line)
            if len(log_lines) > MAX_LOG_LINES:
                log_lines.pop(0)
        except Exception:
            pass


def _thread_target(stop_event: asyncio.Event, db_path: str, project_root: str) -> None:
    """Bot background thread entry point."""
    global bot_loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot_loop = loop

    # Capture logs
    handler = BotLogHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    try:
        log_lines.append("Bot thread starting...")
        log_lines.append(f"PID: {os.getpid()}, Thread: {threading.current_thread().name}")
        log_lines.append(f"DB: {db_path}")
        log_lines.append(f"Project: {project_root}")
        log_lines.append(f"CWD: {os.getcwd()}")

        # Ensure writable DB directory
        db_dir = Path(db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)
        log_lines.append(f"DB dir writable: {os.access(str(db_dir), os.W_OK)}")

        # Set env for settings loader
        os.environ["SOH_APP__SQLITE_DB_PATH"] = db_path

        # Ensure project root is importable
        if project_root not in sys.path:
            sys.path.insert(0, project_root)

        # Try to chdir (may fail on read-only filesystem)
        try:
            os.chdir(project_root)
        except OSError as e:
            log_lines.append(f"chdir failed (ok): {e}")

        log_lines.append(f"Config exists: {Path(project_root, 'config').exists()}")
        log_lines.append(f"config/app.yaml exists: {Path(project_root, 'config', 'app.yaml').exists()}")

        # Import and run
        import importlib

        import src.main as main_mod
        importlib.reload(main_mod)

        log_lines.append("Calling run_bot_headless()...")
        loop.run_until_complete(main_mod.run_bot_headless(stop_event=stop_event))
        log_lines.append("run_bot_headless returned (unexpected)")

    except asyncio.CancelledError:
        log_lines.append("Bot stopped (cancelled)")
    except Exception as e:
        log_lines.append(f"CRASHED: {type(e).__name__}: {e}")
        import traceback
        for line in traceback.format_exc().split("\n"):
            if line.strip():
                log_lines.append(line)
    finally:
        bot_loop = None
        SENTINEL.unlink(missing_ok=True)
        log_lines.append("Bot thread exited")
        with contextlib.suppress(Exception):
            loop.close()


def is_running() -> bool:
    """Check if the bot thread is alive."""
    global bot_thread
    if bot_thread is not None and bot_thread.is_alive():
        return True
    # Fallback: sentinel file
    if SENTINEL.exists():
        try:
            pid = int(SENTINEL.read_text().strip())
            os.kill(pid, 0)
            return True
        except (ValueError, ProcessLookupError, PermissionError):
            SENTINEL.unlink(missing_ok=True)
    return False


def start(db_path: str, project_root: str) -> str:
    """Start the bot in a background thread."""
    global bot_thread, bot_stop_event
    if is_running():
        return "Bot is already running"

    bot_stop_event = asyncio.Event()
    log_lines.clear()
    bot_thread = threading.Thread(
        target=_thread_target,
        args=(bot_stop_event, db_path, project_root),
        daemon=True,
        name="soh-bot",
    )
    bot_thread.start()
    SENTINEL.write_text(str(os.getpid()))
    return "Bot started"


def stop() -> str:
    """Stop the bot gracefully."""
    global bot_thread, bot_stop_event
    if not is_running():
        return "Bot is not running"
    if bot_stop_event:
        bot_stop_event.set()
    if bot_thread:
        bot_thread.join(timeout=5.0)
    bot_thread = None
    bot_stop_event = None
    SENTINEL.unlink(missing_ok=True)
    return "Bot stopped"


def get_logs(n: int = 50) -> str:
    """Get the last N log lines."""
    if not log_lines:
        return "No log output yet — start the bot first"
    return "\n".join(log_lines[-n:])
