from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable

# --- Configuration ---
# Always use /tmp for SQLite. Streamlit Cloud's /mount/src/ allows basic
# file ops but SQLite WAL/journal fails. /tmp is real writable tmpfs.
_DB_DIR = Path("/tmp/soh/sqlite")
_DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = os.environ.get("SOH_SQLITE_PATH", str(_DB_DIR / "stale_odds_hunter.db"))

# --- Apple-Inspired CSS ---
CUSTOM_CSS = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

    /* Global reset */
    .stApp {
        background-color: #FAFAFA;
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    }

    /* Hide Streamlit chrome */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    .stDeployButton {display: none;}

    /* Top nav bar */
    .top-bar {
        background: rgba(255,255,255,0.72);
        backdrop-filter: saturate(180%) blur(20px);
        -webkit-backdrop-filter: saturate(180%) blur(20px);
        border-bottom: 1px solid rgba(0,0,0,0.06);
        padding: 12px 24px;
        margin: -1rem -1rem 24px -1rem;
        display: flex;
        align-items: center;
        justify-content: space-between;
    }
    .top-bar h1 {
        font-size: 20px;
        font-weight: 600;
        color: #1D1D1F;
        margin: 0;
        letter-spacing: -0.3px;
    }
    .top-bar .mode-badge {
        background: #F5F5F7;
        color: #86868B;
        padding: 4px 12px;
        border-radius: 20px;
        font-size: 12px;
        font-weight: 500;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    .top-bar .mode-badge.live {
        background: #FFF2F0;
        color: #FF3B30;
    }

    /* Metric cards */
    .metric-card {
        background: rgba(255,255,255,0.72);
        backdrop-filter: blur(20px);
        -webkit-backdrop-filter: blur(20px);
        border: 1px solid rgba(0,0,0,0.04);
        border-radius: 16px;
        padding: 20px 24px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.04), 0 4px 12px rgba(0,0,0,0.03);
        transition: transform 0.2s ease, box-shadow 0.2s ease;
    }
    .metric-card:hover {
        transform: translateY(-1px);
        box-shadow: 0 2px 8px rgba(0,0,0,0.06), 0 8px 24px rgba(0,0,0,0.04);
    }
    .metric-card .label {
        font-size: 12px;
        font-weight: 500;
        color: #86868B;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        margin-bottom: 8px;
    }
    .metric-card .value {
        font-size: 28px;
        font-weight: 600;
        color: #1D1D1F;
        letter-spacing: -0.5px;
        line-height: 1.1;
    }
    .metric-card .value.positive { color: #34C759; }
    .metric-card .value.negative { color: #FF3B30; }
    .metric-card .value.blue { color: #007AFF; }
    .metric-card .sub {
        font-size: 12px;
        color: #86868B;
        margin-top: 6px;
    }

    /* Section headers */
    .section-header {
        font-size: 20px;
        font-weight: 600;
        color: #1D1D1F;
        letter-spacing: -0.3px;
        margin: 32px 0 16px 0;
        padding-bottom: 8px;
    }

    /* Data tables */
    .stDataFrame {
        border-radius: 12px;
        overflow: hidden;
    }
    .stDataFrame table {
        font-family: 'Inter', sans-serif;
        font-size: 13px;
    }
    .stDataFrame th {
        background: #F5F5F7 !important;
        color: #86868B !important;
        font-weight: 500 !important;
        font-size: 11px !important;
        text-transform: uppercase !important;
        letter-spacing: 0.5px !important;
        padding: 12px 16px !important;
        border-bottom: 1px solid rgba(0,0,0,0.06) !important;
    }
    .stDataFrame td {
        padding: 10px 16px !important;
        color: #1D1D1F !important;
        border-bottom: 1px solid rgba(0,0,0,0.03) !important;
    }
    .stDataFrame tr:hover td {
        background: #F5F5F7 !important;
    }

    /* Status dots */
    .status-dot {
        display: inline-block;
        width: 8px;
        height: 8px;
        border-radius: 50%;
        margin-right: 8px;
    }
    .status-dot.green { background: #34C759; box-shadow: 0 0 6px rgba(52,199,89,0.4); }
    .status-dot.yellow { background: #FF9F0A; box-shadow: 0 0 6px rgba(255,159,10,0.4); }
    .status-dot.red { background: #FF3B30; box-shadow: 0 0 6px rgba(255,59,48,0.4); }

    /* Status card */
    .status-card {
        background: rgba(255,255,255,0.72);
        backdrop-filter: blur(20px);
        border: 1px solid rgba(0,0,0,0.04);
        border-radius: 12px;
        padding: 16px 20px;
        display: flex;
        align-items: center;
        gap: 12px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.03);
    }
    .status-card .status-label {
        font-size: 13px;
        font-weight: 500;
        color: #1D1D1F;
    }
    .status-card .status-detail {
        font-size: 12px;
        color: #86868B;
    }

    /* Pill badges */
    .pill {
        display: inline-block;
        padding: 3px 10px;
        border-radius: 20px;
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 0.3px;
    }
    .pill.filled { background: #E8F9ED; color: #34C759; }
    .pill.rejected { background: #FFF2F0; color: #FF3B30; }
    .pill.pending { background: #F5F5F7; color: #86868B; }
    .pill.open { background: #E8F0FE; color: #007AFF; }

    /* Risk gauge */
    .gauge-container {
        background: rgba(255,255,255,0.72);
        backdrop-filter: blur(20px);
        border: 1px solid rgba(0,0,0,0.04);
        border-radius: 12px;
        padding: 16px 20px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.03);
    }
    .gauge-label {
        font-size: 12px;
        font-weight: 500;
        color: #86868B;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        margin-bottom: 8px;
    }
    .gauge-bar {
        height: 6px;
        border-radius: 3px;
        background: #F5F5F7;
        overflow: hidden;
    }
    .gauge-fill {
        height: 100%;
        border-radius: 3px;
        transition: width 0.6s ease;
    }
    .gauge-fill.safe { background: linear-gradient(90deg, #34C759, #30D158); }
    .gauge-fill.warning { background: linear-gradient(90deg, #FF9F0A, #FFD60A); }
    .gauge-fill.danger { background: linear-gradient(90deg, #FF3B30, #FF453A); }
    .gauge-value {
        font-size: 13px;
        font-weight: 500;
        color: #1D1D1F;
        margin-top: 6px;
    }

    /* Tab overrides */
    .stTabs [data-baseweb="tab-list"] {
        gap: 0;
        background: #F5F5F7;
        border-radius: 10px;
        padding: 3px;
    }
    .stTabs [data-baseweb="tab"] {
        border-radius: 8px;
        font-family: 'Inter', sans-serif;
        font-size: 13px;
        font-weight: 500;
        color: #86868B;
        padding: 8px 16px;
    }
    .stTabs [aria-selected="true"] {
        background: white !important;
        color: #1D1D1F !important;
        box-shadow: 0 1px 3px rgba(0,0,0,0.08);
    }
    .stTabs [data-baseweb="tab-highlight"] { display: none; }
    .stTabs [data-baseweb="tab-border"] { display: none; }

    /* Kill switch button */
    .kill-switch button {
        background: #FF3B30 !important;
        color: white !important;
        border: none !important;
        border-radius: 10px !important;
        font-weight: 600 !important;
        font-size: 14px !important;
        padding: 12px 24px !important;
        letter-spacing: 0.3px;
        transition: all 0.2s ease;
    }
    .kill-switch button:hover {
        background: #FF453A !important;
        transform: translateY(-1px);
        box-shadow: 0 4px 12px rgba(255,59,48,0.3) !important;
    }

    /* Sidebar styling */
    section[data-testid="stSidebar"] {
        background: #F5F5F7;
        border-right: 1px solid rgba(0,0,0,0.06);
    }
    section[data-testid="stSidebar"] .stButton button {
        font-family: 'Inter', sans-serif;
        font-size: 13px;
        font-weight: 500;
        border-radius: 8px;
        padding: 8px 12px;
        border: 1px solid rgba(0,0,0,0.08);
        transition: all 0.2s ease;
    }
    section[data-testid="stSidebar"] .stButton button:hover {
        transform: translateY(-1px);
        box-shadow: 0 2px 8px rgba(0,0,0,0.08);
    }
    section[data-testid="stSidebar"] .stButton button[kind="primary"] {
        background: #007AFF;
        color: white;
        border: none;
    }
    section[data-testid="stSidebar"] .stButton button[kind="primary"]:hover {
        background: #0066D6;
    }

    /* Streamlit element overrides */
    div[data-testid="stMetric"] {
        background: rgba(255,255,255,0.72);
        backdrop-filter: blur(20px);
        border: 1px solid rgba(0,0,0,0.04);
        border-radius: 16px;
        padding: 20px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    }
    div[data-testid="stMetric"] label {
        font-size: 12px !important;
        font-weight: 500 !important;
        color: #86868B !important;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    div[data-testid="stMetric"] [data-testid="stMetricValue"] {
        font-size: 28px !important;
        font-weight: 600 !important;
        color: #1D1D1F !important;
        letter-spacing: -0.5px;
    }
    div[data-testid="stMetric"] [data-testid="stMetricDelta"] {
        font-size: 12px !important;
    }

    /* Empty state */
    .empty-state {
        text-align: center;
        padding: 60px 20px;
        color: #86868B;
    }
    .empty-state .icon {
        font-size: 48px;
        margin-bottom: 16px;
        opacity: 0.3;
    }
    .empty-state .message {
        font-size: 15px;
        font-weight: 500;
    }
    .empty-state .hint {
        font-size: 13px;
        margin-top: 8px;
        opacity: 0.7;
    }
</style>
"""


# --- In-Process Bot Management ---
# Bot state lives in an imported module (bot_runner) so it persists
# across Streamlit script reruns. The main script file gets re-executed
# on every rerun, but imported module globals are cached by Python.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from src.dashboard import bot_runner


def is_bot_running() -> bool:
    return bot_runner.is_running()

def start_bot() -> str:
    return bot_runner.start(db_path=DB_PATH, project_root=str(PROJECT_ROOT))

def stop_bot() -> str:
    return bot_runner.stop()


# --- Database Connection ---
@st.cache_resource
def get_db() -> sqlite3.Connection:
    """Connect to SQLite with WAL-safe read-only access."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def safe_query(query: str, params: tuple = ()) -> pd.DataFrame:
    """Execute a query and return a DataFrame, gracefully handling missing tables."""
    try:
        conn = get_db()
        return pd.read_sql_query(query, conn, params=params)
    except Exception:
        return pd.DataFrame()


# --- Component Helpers ---

def metric_card(label: str, value: str, color_class: str = "", sub: str = "") -> str:
    val_class = f" {color_class}" if color_class else ""
    sub_html = f'<div class="sub">{sub}</div>' if sub else ""
    return f"""
    <div class="metric-card">
        <div class="label">{label}</div>
        <div class="value{val_class}">{value}</div>
        {sub_html}
    </div>
    """


def status_card(label: str, detail: str, color: str = "green") -> str:
    return f"""
    <div class="status-card">
        <span class="status-dot {color}"></span>
        <div>
            <div class="status-label">{label}</div>
            <div class="status-detail">{detail}</div>
        </div>
    </div>
    """


def pill_badge(text: str, status: str) -> str:
    return f'<span class="pill {status}">{text}</span>'


def risk_gauge(label: str, value: float, max_val: float) -> str:
    pct = min(value / max_val * 100, 100) if max_val > 0 else 0
    if pct < 50:
        fill_class = "safe"
    elif pct < 80:
        fill_class = "warning"
    else:
        fill_class = "danger"
    return f"""
    <div class="gauge-container">
        <div class="gauge-label">{label}</div>
        <div class="gauge-bar"><div class="gauge-fill {fill_class}" style="width:{pct:.0f}%"></div></div>
        <div class="gauge-value">{value:.2f} / {max_val:.2f} ({pct:.0f}%)</div>
    </div>
    """


def section_header(text: str) -> str:
    return f'<div class="section-header">{text}</div>'


def empty_state(icon: str, message: str, hint: str = "") -> str:
    hint_html = f'<div class="hint">{hint}</div>' if hint else ""
    return f"""
    <div class="empty-state">
        <div class="icon">{icon}</div>
        <div class="message">{message}</div>
        {hint_html}
    </div>
    """


# --- Data Queries ---

def get_portfolio_summary() -> dict:
    df = safe_query(
        "SELECT side, size, avg_entry, realized_pnl, unrealized_pnl FROM positions"
    )
    if df.empty:
        return {"exposure": 0, "realized": 0, "unrealized": 0, "total_pnl": 0, "open": 0}

    open_df = df[df["size"] > 0]
    return {
        "exposure": (open_df["size"] * open_df["avg_entry"]).sum() if not open_df.empty else 0,
        "realized": df["realized_pnl"].sum(),
        "unrealized": df["unrealized_pnl"].sum(),
        "total_pnl": df["realized_pnl"].sum() + df["unrealized_pnl"].sum(),
        "open": len(open_df),
    }


def get_recent_signals(limit: int = 30) -> pd.DataFrame:
    return safe_query(
        """SELECT s.timestamp, s.strategy, s.side, s.fair_value, s.market_price,
                  s.edge, s.confidence, s.rationale, m.question, m.slug
           FROM signals s
           LEFT JOIN markets m ON s.market_condition_id = m.condition_id
           ORDER BY s.timestamp DESC LIMIT ?""",
        (limit,),
    )


def get_recent_orders(limit: int = 50) -> pd.DataFrame:
    return safe_query(
        """SELECT o.created_at, o.side, o.price, o.size, o.status,
                  o.fill_price, o.fill_size, o.reject_reason,
                  m.question, m.slug
           FROM orders o
           LEFT JOIN markets m ON o.market_condition_id = m.condition_id
           ORDER BY o.created_at DESC LIMIT ?""",
        (limit,),
    )


def get_recent_fills(limit: int = 50) -> pd.DataFrame:
    return safe_query(
        """SELECT f.timestamp, f.fill_price, f.fill_size, f.fee_estimate,
                  o.side, o.price as intended_price, m.question, m.slug
           FROM fills f
           JOIN orders o ON f.order_id = o.id
           LEFT JOIN markets m ON o.market_condition_id = m.condition_id
           ORDER BY f.timestamp DESC LIMIT ?""",
        (limit,),
    )


def get_tracked_markets() -> pd.DataFrame:
    return safe_query(
        """SELECT condition_id, question, slug, category, volume, volume_24h,
                  liquidity, fees_enabled, updated_at
           FROM markets WHERE active = 1
           ORDER BY liquidity DESC"""
    )


def get_open_positions() -> pd.DataFrame:
    return safe_query(
        """SELECT p.token_id, p.side, p.size, p.avg_entry, p.realized_pnl, p.unrealized_pnl,
                  m.question, m.slug
           FROM positions p
           LEFT JOIN markets m ON p.condition_id = m.condition_id
           WHERE p.size > 0
           ORDER BY p.size * p.avg_entry DESC"""
    )


def get_risk_events(limit: int = 20) -> pd.DataFrame:
    return safe_query(
        "SELECT timestamp, severity, event_type, details_json FROM risk_events ORDER BY rowid DESC LIMIT ?",
        (limit,),
    )


def get_order_stats() -> dict:
    df = safe_query(
        "SELECT status, COUNT(*) as cnt FROM orders GROUP BY status"
    )
    if df.empty:
        return {"total": 0, "filled": 0, "rejected": 0, "open": 0, "pending": 0}
    stats = dict(zip(df["status"], df["cnt"], strict=False))
    return {
        "total": int(df["cnt"].sum()),
        "filled": int(stats.get("FILLED", 0)),
        "rejected": int(stats.get("REJECTED", 0)),
        "open": int(stats.get("OPEN", 0)),
        "pending": int(stats.get("PENDING", 0)),
    }


def get_fill_stats() -> dict:
    df = safe_query(
        "SELECT COUNT(*) as cnt, SUM(fill_size) as total_size, SUM(fee_estimate) as total_fees FROM fills"
    )
    if df.empty or df["cnt"].iloc[0] == 0:
        return {"count": 0, "total_size": 0, "total_fees": 0}
    return {
        "count": int(df["cnt"].iloc[0]),
        "total_size": float(df["total_size"].iloc[0] or 0),
        "total_fees": float(df["total_fees"].iloc[0] or 0),
    }


# --- Main App ---

def main() -> None:
    st.set_page_config(
        page_title="Stale Odds Hunter",
        page_icon="",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # Inject CSS
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

    # Auto-refresh every 5 seconds
    st_autorefresh(interval=5000, key="autorefresh")

    # --- Bot Status ---
    bot_running = is_bot_running()
    is_halted = False

    # --- Sidebar: Controls ---
    with st.sidebar:
        st.markdown("""
        <div style="padding: 8px 0 24px 0;">
            <div style="font-size: 20px; font-weight: 600; color: #1D1D1F; letter-spacing: -0.3px;">Controls</div>
        </div>
        """, unsafe_allow_html=True)

        # Bot status indicator
        if bot_running:
            dot_color = "#FF3B30" if is_halted else "#34C759"
            status_text = "HALTED" if is_halted else "RUNNING"
        else:
            dot_color = "#86868B"
            status_text = "STOPPED"

        st.markdown(f"""
        <div style="display:flex; align-items:center; gap:10px; padding:12px 16px;
                    background:rgba(255,255,255,0.72); border-radius:12px;
                    border:1px solid rgba(0,0,0,0.04); margin-bottom:16px;">
            <div style="width:10px; height:10px; border-radius:50%; background:{dot_color};
                        box-shadow: 0 0 8px {dot_color}40;"></div>
            <div>
                <div style="font-size:14px; font-weight:600; color:#1D1D1F;">Bot {status_text}</div>
                <div style="font-size:12px; color:#86868B;">Paper Mode</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        # Start / Stop buttons
        col_start, col_stop = st.columns(2)
        with col_start:
            if st.button("Start Bot", disabled=bot_running, use_container_width=True, type="primary"):
                msg = start_bot()
                st.success(msg)
                st.rerun()
        with col_stop:
            if st.button("Stop Bot", disabled=not bot_running, use_container_width=True):
                msg = stop_bot()
                st.info(msg)
                st.rerun()

        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

        # Trading controls
        col_halt, col_resume = st.columns(2)
        with col_halt:
            if st.button("Halt Trading", disabled=not bot_running or is_halted, use_container_width=True):
                msg = stop_bot()
                st.warning(msg)
                st.rerun()
        with col_resume:
            if st.button("Resume", disabled=not bot_running or not is_halted, use_container_width=True):
                msg = start_bot()
                st.success(msg)
                st.rerun()

        st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)

        # Risk status from database (works without API)
        if bot_running:
            risk_df = safe_query(
                "SELECT COUNT(*) as cnt FROM positions WHERE size > 0"
            )
            exposure_df = safe_query(
                "SELECT COALESCE(SUM(size * avg_entry), 0) as exp FROM positions WHERE size > 0"
            )
            open_pos = int(risk_df["cnt"].iloc[0]) if not risk_df.empty else 0
            total_exp = float(exposure_df["exp"].iloc[0]) if not exposure_df.empty else 0.0

            st.markdown("""
            <div style="font-size:13px; font-weight:600; color:#86868B; text-transform:uppercase;
                        letter-spacing:0.5px; margin-bottom:8px;">Risk Status</div>
            """, unsafe_allow_html=True)
            st.markdown(risk_gauge("Exposure", total_exp, 50.0), unsafe_allow_html=True)
            st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
            st.markdown(risk_gauge("Positions", float(open_pos), 20.0), unsafe_allow_html=True)

        st.markdown("<div style='height:24px'></div>", unsafe_allow_html=True)

        # Log viewer — expanded by default if bot not running (to show crash info)
        with st.expander("Bot Logs", expanded=not bot_running):
            log_text = bot_runner.get_logs(50)
            st.code(log_text, language=None)

        # Debug info (always visible — helps diagnose Cloud issues)
        with st.expander("Debug Info", expanded=False):
            st.code(
                f"PROJECT_ROOT: {PROJECT_ROOT}\n"
                f"DB_PATH: {DB_PATH}\n"
                f"CWD: {os.getcwd()}\n"
                f"config/ exists: {(PROJECT_ROOT / 'config').exists()}\n"
                f"app.yaml exists: {(PROJECT_ROOT / 'config' / 'app.yaml').exists()}\n"
                f"bot_runner module: {bot_runner.__file__}\n"
                f"bot_thread alive: {bot_runner.bot_thread is not None and bot_runner.bot_thread.is_alive()}\n"
                f"sentinel exists: {bot_runner.SENTINEL.exists()}\n"
                f"Python: {sys.executable}\n"
                f"sys.path[0]: {sys.path[0] if sys.path else 'empty'}",
                language=None,
            )

    # --- Top Bar ---
    status_dot = f'<span class="status-dot {"green" if bot_running and not is_halted else "red" if is_halted else "yellow"}"></span>'
    st.markdown(f"""
    <div class="top-bar">
        <h1>Stale Odds Hunter</h1>
        <div style="display:flex; align-items:center; gap:12px;">
            {status_dot}
            <span class="mode-badge">Paper Mode</span>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # --- Tabs ---
    tab_command, tab_orders, tab_portfolio, tab_health, tab_backtest = st.tabs([
        "Command Center",
        "Orders & Fills",
        "Portfolio",
        "System Health",
        "Backtest",
    ])

    # ==============================
    # TAB 1: Command Center
    # ==============================
    with tab_command:
        portfolio = get_portfolio_summary()
        order_stats = get_order_stats()
        fill_stats = get_fill_stats()

        # KPI Row
        k1, k2, k3, k4 = st.columns(4)
        with k1:
            color = "positive" if portfolio["total_pnl"] >= 0 else "negative"
            pnl_str = f"${portfolio['total_pnl']:+.2f}"
            st.markdown(metric_card("Total P&L", pnl_str, color,
                                     f"Realized: ${portfolio['realized']:.2f}"),
                        unsafe_allow_html=True)
        with k2:
            st.markdown(metric_card("Exposure", f"${portfolio['exposure']:.2f}", "blue",
                                     f"{portfolio['open']} open positions"),
                        unsafe_allow_html=True)
        with k3:
            st.markdown(metric_card("Total Fills", str(fill_stats["count"]), "",
                                     f"Volume: ${fill_stats['total_size']:.2f}"),
                        unsafe_allow_html=True)
        with k4:
            win_rate = "—"
            if order_stats["total"] > 0:
                rate = order_stats["filled"] / order_stats["total"] * 100
                win_rate = f"{rate:.0f}%"
            st.markdown(metric_card("Fill Rate", win_rate, "",
                                     f"{order_stats['filled']} filled / {order_stats['total']} orders"),
                        unsafe_allow_html=True)

        st.markdown("<div style='height: 8px'></div>", unsafe_allow_html=True)

        # Signals Table
        st.markdown(section_header("Live Signals"), unsafe_allow_html=True)
        signals_df = get_recent_signals(30)
        if signals_df.empty:
            st.markdown(
                empty_state("", "No signals yet",
                            "The bot is scanning markets. Signals appear when edges are detected."),
                unsafe_allow_html=True,
            )
        else:
            display_df = signals_df[["timestamp", "strategy", "side", "edge", "confidence",
                                      "fair_value", "market_price", "question"]].copy()
            display_df.columns = ["Time", "Strategy", "Side", "Edge", "Confidence",
                                   "Fair Value", "Market Price", "Market"]
            display_df["Edge"] = display_df["Edge"].apply(lambda x: f"{x:+.4f}")
            display_df["Confidence"] = display_df["Confidence"].apply(lambda x: f"{x:.0%}")
            display_df["Fair Value"] = display_df["Fair Value"].apply(lambda x: f"{x:.4f}")
            display_df["Market Price"] = display_df["Market Price"].apply(lambda x: f"{x:.4f}")
            if "Market" in display_df.columns:
                display_df["Market"] = display_df["Market"].apply(
                    lambda x: str(x)[:50] + "..." if isinstance(x, str) and len(str(x)) > 50 else x
                )
            st.dataframe(display_df, width="stretch", hide_index=True, height=400)

        # Tracked Markets
        st.markdown(section_header("Tracked Markets"), unsafe_allow_html=True)
        markets_df = get_tracked_markets()
        if markets_df.empty:
            st.markdown(empty_state("", "No markets tracked yet"), unsafe_allow_html=True)
        else:
            m_display = markets_df[["question", "category", "liquidity", "volume_24h", "fees_enabled"]].copy()
            m_display.columns = ["Market", "Category", "Liquidity", "24h Volume", "Fees"]
            m_display["Liquidity"] = m_display["Liquidity"].apply(lambda x: f"${x:,.0f}")
            m_display["24h Volume"] = m_display["24h Volume"].apply(lambda x: f"${x:,.0f}")
            m_display["Fees"] = m_display["Fees"].apply(lambda x: "Yes" if x else "No")
            if "Market" in m_display.columns:
                m_display["Market"] = m_display["Market"].apply(
                    lambda x: str(x)[:60] + "..." if isinstance(x, str) and len(str(x)) > 60 else x
                )
            st.dataframe(m_display, width="stretch", hide_index=True, height=350)

    # ==============================
    # TAB 2: Orders & Fills
    # ==============================
    with tab_orders:
        order_stats = get_order_stats()

        o1, o2, o3, o4 = st.columns(4)
        with o1:
            st.markdown(metric_card("Total Orders", str(order_stats["total"])), unsafe_allow_html=True)
        with o2:
            st.markdown(metric_card("Filled", str(order_stats["filled"]), "positive"), unsafe_allow_html=True)
        with o3:
            st.markdown(metric_card("Rejected", str(order_stats["rejected"]), "negative"), unsafe_allow_html=True)
        with o4:
            st.markdown(metric_card("Open", str(order_stats["open"]), "blue"), unsafe_allow_html=True)

        st.markdown("<div style='height: 8px'></div>", unsafe_allow_html=True)

        sub_tab1, sub_tab2, sub_tab3 = st.tabs(["All Orders", "Fills", "Rejections"])

        with sub_tab1:
            orders_df = get_recent_orders(50)
            if orders_df.empty:
                st.markdown(empty_state("", "No orders yet"), unsafe_allow_html=True)
            else:
                o_display = orders_df[["created_at", "side", "price", "size", "status",
                                        "fill_price", "question"]].copy()
                o_display.columns = ["Time", "Side", "Price", "Size", "Status", "Fill Price", "Market"]
                o_display["Price"] = o_display["Price"].apply(lambda x: f"{x:.4f}")
                o_display["Size"] = o_display["Size"].apply(lambda x: f"{x:.2f}")
                o_display["Fill Price"] = o_display["Fill Price"].apply(
                    lambda x: f"{x:.4f}" if pd.notna(x) and x else "—"
                )
                if "Market" in o_display.columns:
                    o_display["Market"] = o_display["Market"].apply(
                        lambda x: str(x)[:45] + "..." if isinstance(x, str) and len(str(x)) > 45 else x
                    )
                st.dataframe(o_display, width="stretch", hide_index=True, height=400)

        with sub_tab2:
            fills_df = get_recent_fills(50)
            if fills_df.empty:
                st.markdown(empty_state("", "No fills yet"), unsafe_allow_html=True)
            else:
                f_display = fills_df[["timestamp", "side", "fill_price", "fill_size",
                                       "intended_price", "fee_estimate", "question"]].copy()
                f_display.columns = ["Time", "Side", "Fill Price", "Size", "Intended", "Fee", "Market"]
                f_display["Fill Price"] = f_display["Fill Price"].apply(lambda x: f"{x:.4f}")
                f_display["Size"] = f_display["Size"].apply(lambda x: f"{x:.2f}")
                f_display["Intended"] = f_display["Intended"].apply(lambda x: f"{x:.4f}")
                f_display["Fee"] = f_display["Fee"].apply(lambda x: f"${x:.4f}")
                slippage = fills_df["fill_price"].astype(float) - fills_df["intended_price"].astype(float)
                f_display["Slippage"] = slippage.apply(lambda x: f"{x:+.4f}")
                if "Market" in f_display.columns:
                    f_display["Market"] = f_display["Market"].apply(
                        lambda x: str(x)[:45] + "..." if isinstance(x, str) and len(str(x)) > 45 else x
                    )
                st.dataframe(f_display, width="stretch", hide_index=True, height=400)

        with sub_tab3:
            rejected_df = safe_query(
                """SELECT o.created_at, o.side, o.price, o.size, o.reject_reason, m.question
                   FROM orders o
                   LEFT JOIN markets m ON o.market_condition_id = m.condition_id
                   WHERE o.status = 'REJECTED'
                   ORDER BY o.created_at DESC LIMIT 50"""
            )
            if rejected_df.empty:
                st.markdown(empty_state("", "No rejected orders",
                                        "Risk engine has not vetoed any trades yet."),
                            unsafe_allow_html=True)
            else:
                r_display = rejected_df.copy()
                r_display.columns = ["Time", "Side", "Price", "Size", "Reason", "Market"]
                r_display["Price"] = r_display["Price"].apply(lambda x: f"{x:.4f}")
                r_display["Size"] = r_display["Size"].apply(lambda x: f"{x:.2f}")
                if "Market" in r_display.columns:
                    r_display["Market"] = r_display["Market"].apply(
                        lambda x: str(x)[:45] + "..." if isinstance(x, str) and len(str(x)) > 45 else x
                    )
                st.dataframe(r_display, width="stretch", hide_index=True, height=400)

    # ==============================
    # TAB 3: Portfolio
    # ==============================
    with tab_portfolio:
        portfolio = get_portfolio_summary()

        p1, p2, p3 = st.columns(3)
        with p1:
            color = "positive" if portfolio["realized"] >= 0 else "negative"
            st.markdown(metric_card("Realized P&L", f"${portfolio['realized']:+.2f}", color),
                        unsafe_allow_html=True)
        with p2:
            color = "positive" if portfolio["unrealized"] >= 0 else "negative"
            st.markdown(metric_card("Unrealized P&L", f"${portfolio['unrealized']:+.2f}", color),
                        unsafe_allow_html=True)
        with p3:
            st.markdown(metric_card("Total Exposure", f"${portfolio['exposure']:.2f}", "blue",
                                     f"{portfolio['open']} positions"),
                        unsafe_allow_html=True)

        st.markdown("<div style='height: 8px'></div>", unsafe_allow_html=True)

        # Risk gauges
        st.markdown(section_header("Risk Limits"), unsafe_allow_html=True)
        g1, g2, g3 = st.columns(3)
        with g1:
            st.markdown(risk_gauge("Portfolio Exposure", portfolio["exposure"], 50.0),
                        unsafe_allow_html=True)
        with g2:
            dd = abs(portfolio["total_pnl"]) if portfolio["total_pnl"] < 0 else 0
            st.markdown(risk_gauge("Daily Drawdown", dd, 30.0), unsafe_allow_html=True)
        with g3:
            st.markdown(risk_gauge("Open Positions", float(portfolio["open"]), 20.0),
                        unsafe_allow_html=True)

        st.markdown("<div style='height: 8px'></div>", unsafe_allow_html=True)

        # Open positions table
        st.markdown(section_header("Open Positions"), unsafe_allow_html=True)
        positions_df = get_open_positions()
        if positions_df.empty:
            st.markdown(empty_state("", "No open positions"), unsafe_allow_html=True)
        else:
            pos_display = positions_df[["question", "side", "size", "avg_entry",
                                         "realized_pnl", "unrealized_pnl"]].copy()
            pos_display.columns = ["Market", "Side", "Size", "Avg Entry", "Realized P&L", "Unrealized P&L"]
            pos_display["Size"] = pos_display["Size"].apply(lambda x: f"{x:.2f}")
            pos_display["Avg Entry"] = pos_display["Avg Entry"].apply(lambda x: f"{x:.4f}")
            pos_display["Realized P&L"] = pos_display["Realized P&L"].apply(lambda x: f"${x:+.4f}")
            pos_display["Unrealized P&L"] = pos_display["Unrealized P&L"].apply(lambda x: f"${x:+.4f}")
            if "Market" in pos_display.columns:
                pos_display["Market"] = pos_display["Market"].apply(
                    lambda x: str(x)[:50] + "..." if isinstance(x, str) and len(str(x)) > 50 else x
                )
            st.dataframe(pos_display, width="stretch", hide_index=True)

    # ==============================
    # TAB 4: System Health
    # ==============================
    with tab_health:
        # System status cards
        s1, s2, s3 = st.columns(3)

        # Check if we have recent data
        latest_snapshot = safe_query(
            "SELECT MAX(timestamp) as ts FROM orderbook_snapshots"
        )
        latest_signal = safe_query(
            "SELECT MAX(timestamp) as ts FROM signals"
        )
        market_count = safe_query("SELECT COUNT(*) as cnt FROM markets WHERE active = 1")

        with s1:
            has_data = not latest_snapshot.empty and latest_snapshot["ts"].iloc[0] is not None
            if has_data:
                st.markdown(status_card("Data Feed", "Receiving updates", "green"),
                            unsafe_allow_html=True)
            else:
                st.markdown(status_card("Data Feed", "No data yet", "yellow"),
                            unsafe_allow_html=True)

        with s2:
            cnt = int(market_count["cnt"].iloc[0]) if not market_count.empty else 0
            st.markdown(status_card("Markets Tracked", f"{cnt} active markets", "green" if cnt > 0 else "yellow"),
                        unsafe_allow_html=True)

        with s3:
            has_signals = not latest_signal.empty and latest_signal["ts"].iloc[0] is not None
            if has_signals:
                st.markdown(status_card("Signal Engine", "Generating signals", "green"),
                            unsafe_allow_html=True)
            else:
                st.markdown(status_card("Signal Engine", "Waiting for data", "yellow"),
                            unsafe_allow_html=True)

        st.markdown("<div style='height: 16px'></div>", unsafe_allow_html=True)

        # Data freshness
        st.markdown(section_header("Data Freshness"), unsafe_allow_html=True)
        d1, d2 = st.columns(2)
        with d1:
            snapshot_count = safe_query("SELECT COUNT(*) as cnt FROM orderbook_snapshots")
            cnt = int(snapshot_count["cnt"].iloc[0]) if not snapshot_count.empty else 0
            ts_str = str(latest_snapshot["ts"].iloc[0])[:19] if has_data else "Never"
            st.markdown(metric_card("Orderbook Snapshots", f"{cnt:,}", "",
                                     f"Last: {ts_str}"),
                        unsafe_allow_html=True)
        with d2:
            trade_count = safe_query("SELECT COUNT(*) as cnt FROM trades_tape")
            tcnt = int(trade_count["cnt"].iloc[0]) if not trade_count.empty else 0
            st.markdown(metric_card("Trades Recorded", f"{tcnt:,}"), unsafe_allow_html=True)

        st.markdown("<div style='height: 16px'></div>", unsafe_allow_html=True)

        # Risk events
        st.markdown(section_header("Recent Risk Events"), unsafe_allow_html=True)
        risk_df = get_risk_events(15)
        if risk_df.empty:
            st.markdown(empty_state("", "No risk events", "The risk engine is monitoring."),
                        unsafe_allow_html=True)
        else:
            re_display = risk_df[["timestamp", "severity", "event_type", "details_json"]].copy()
            re_display.columns = ["Time", "Severity", "Type", "Details"]
            re_display["Details"] = re_display["Details"].apply(
                lambda x: str(json.loads(x).get("reason", x))[:80] if x else ""
            )
            st.dataframe(re_display, width="stretch", hide_index=True, height=300)

        st.markdown("<div style='height: 16px'></div>", unsafe_allow_html=True)

        # Kill switch
        st.markdown(section_header("Emergency Controls"), unsafe_allow_html=True)
        col_kill, col_info = st.columns([1, 3])
        with col_kill:
            st.markdown('<div class="kill-switch">', unsafe_allow_html=True)
            if st.button("HALT ALL TRADING", type="primary", width="stretch"):
                msg = stop_bot()
                st.success(f"Bot stopped: {msg}")
                st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)
        with col_info:
            st.markdown("""
            <div style="padding: 16px 0; color: #86868B; font-size: 13px; line-height: 1.6;">
                Pressing this button will immediately stop the bot and halt all trading activity.
                No new orders will be placed. Use the Start Bot button in the sidebar to restart.
            </div>
            """, unsafe_allow_html=True)

    # ==============================
    # TAB 5: Backtest
    # ==============================
    with tab_backtest:
        st.markdown(section_header("Run Backtest"), unsafe_allow_html=True)

        bc1, bc2, bc3 = st.columns([2, 2, 1])
        with bc1:
            bt_from = st.date_input("From", value=None, key="bt_from")
        with bc2:
            bt_to = st.date_input("To", value=None, key="bt_to")
        with bc3:
            st.markdown("<div style='height: 28px'></div>", unsafe_allow_html=True)
            run_bt = st.button("Run Backtest", type="primary")

        if run_bt and bt_from and bt_to:
            with st.spinner("Running backtest..."):
                import subprocess
                result = subprocess.run(
                    [
                        sys.executable, "-m", "src.main", "backtest",
                        "--strategy", "stale_odds",
                        "--from", str(bt_from),
                        "--to", str(bt_to),
                    ],
                    capture_output=True, text=True,
                    cwd=str(Path(__file__).resolve().parents[2]),
                    timeout=60,
                )
                output = result.stdout + result.stderr
                # Parse results
                lines = output.strip().split("\n")
                st.code("\n".join(lines[-12:]), language=None)

        st.markdown("<div style='height: 16px'></div>", unsafe_allow_html=True)

        # Historical signals chart
        st.markdown(section_header("Signal History"), unsafe_allow_html=True)
        signal_history = safe_query(
            """SELECT DATE(timestamp) as date, COUNT(*) as signals,
                      AVG(edge) as avg_edge, AVG(confidence) as avg_conf
               FROM signals
               GROUP BY DATE(timestamp)
               ORDER BY date"""
        )
        if signal_history.empty:
            st.markdown(empty_state("", "No historical signal data",
                                    "Run the bot to collect data, then backtest here."),
                        unsafe_allow_html=True)
        else:
            st.line_chart(signal_history.set_index("date")[["signals"]], height=250)
            st.line_chart(signal_history.set_index("date")[["avg_edge"]], height=200)

        # Order outcome breakdown
        st.markdown(section_header("Order Outcomes"), unsafe_allow_html=True)
        outcome_df = safe_query(
            """SELECT status, COUNT(*) as count FROM orders GROUP BY status"""
        )
        if not outcome_df.empty:
            st.bar_chart(outcome_df.set_index("status"), height=250)


if __name__ == "__main__":
    main()
