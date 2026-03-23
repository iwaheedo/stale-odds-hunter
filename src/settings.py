from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class AppConfig:
    app_name: str = "stale-odds-hunter"
    mode: str = "paper"
    market_data_poll_interval_sec: int = 30
    ws_reconnect_sec: int = 3
    ws_ping_interval_sec: int = 10
    sqlite_db_path: str = "data/sqlite/stale_odds_hunter.db"
    duckdb_path: str = "data/duckdb/analytics.duckdb"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    dashboard_enabled: bool = True
    log_level: str = "INFO"
    log_format: str = "json"


@dataclass(frozen=True)
class MarketsConfig:
    categories_whitelist: list[str] = field(default_factory=list)
    slugs_whitelist: list[str] = field(default_factory=list)
    slugs_blacklist: list[str] = field(default_factory=list)
    min_liquidity: float = 1000.0
    max_spread: float = 0.15
    min_volume_24h: float = 500.0
    exclude_sports: bool = False
    exclude_fee_enabled: bool = False
    binary_only: bool = True
    max_tracked_markets: int = 50


@dataclass(frozen=True)
class StrategiesConfig:
    enabled_strategies: list[str] = field(default_factory=lambda: ["stale_odds"])
    entry_edge_threshold: float = 0.03
    exit_edge_threshold: float = 0.01
    slippage_buffer: float = 0.005
    uncertainty_buffer: float = 0.01
    hold_time_limit_minutes: int = 120
    default_order_size_usd: float = 10.0
    max_order_size_usd: float = 50.0
    stale_odds: dict[str, float | int | bool] = field(default_factory=lambda: {
        "complement_deviation_threshold": 0.03,
        "staleness_seconds": 60,
        "min_confidence": 0.3,
        "max_spread_for_entry": 0.10,
    })
    cross_market: dict[str, float | int | bool] = field(default_factory=lambda: {
        "enabled": False,
        "sum_deviation_threshold": 0.05,
    })


@dataclass(frozen=True)
class RiskConfig:
    max_risk_per_trade_pct: float = 1.0
    max_market_exposure_pct: float = 5.0
    max_category_exposure_pct: float = 10.0
    max_correlated_exposure_pct: float = 8.0
    max_daily_drawdown_pct: float = 3.0
    max_open_positions: int = 20
    starting_equity_usd: float = 1000.0
    max_orders_per_minute: int = 10
    stale_feed_timeout_sec: int = 60
    max_spread_ceiling: float = 0.25
    rapid_adverse_move_pct: float = 5.0
    max_order_reject_rate_pct: float = 50.0
    panic_flatten_enabled: bool = True
    no_entry_before_close_minutes: int = 30


@dataclass(frozen=True)
class Settings:
    app: AppConfig
    markets: MarketsConfig
    strategies: StrategiesConfig
    risk: RiskConfig

    @property
    def is_live(self) -> bool:
        return self.app.mode == "live"


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def _apply_env_overrides(data: dict, prefix: str) -> dict:
    """Override config values with env vars like SOH_APP__LOG_LEVEL=DEBUG."""
    for key, value in data.items():
        env_key = f"{prefix}__{key}".upper()
        env_val = os.environ.get(env_key)
        if env_val is not None:
            if isinstance(value, bool):
                data[key] = env_val.lower() in ("true", "1", "yes")
            elif isinstance(value, int):
                data[key] = int(env_val)
            elif isinstance(value, float):
                data[key] = float(env_val)
            else:
                data[key] = env_val
    return data


def load_settings(config_dir: str = "config/") -> Settings:
    load_dotenv()

    config_path = Path(config_dir)

    app_data = _apply_env_overrides(_load_yaml(config_path / "app.yaml"), "SOH_APP")
    markets_data = _apply_env_overrides(_load_yaml(config_path / "markets.yaml"), "SOH_MARKETS")
    strategies_data = _apply_env_overrides(
        _load_yaml(config_path / "strategies.yaml"), "SOH_STRATEGIES"
    )
    risk_data = _apply_env_overrides(_load_yaml(config_path / "risk.yaml"), "SOH_RISK")

    # Override mode from env
    live_enabled = os.environ.get("LIVE_TRADING_ENABLED", "false").lower() in ("true", "1")
    if live_enabled:
        app_data["mode"] = "live"

    log_level = os.environ.get("LOG_LEVEL")
    if log_level:
        app_data["log_level"] = log_level

    return Settings(
        app=AppConfig(**{k: v for k, v in app_data.items() if k in AppConfig.__dataclass_fields__}),
        markets=MarketsConfig(
            **{k: v for k, v in markets_data.items() if k in MarketsConfig.__dataclass_fields__}
        ),
        strategies=StrategiesConfig(
            **{
                k: v
                for k, v in strategies_data.items()
                if k in StrategiesConfig.__dataclass_fields__
            }
        ),
        risk=RiskConfig(
            **{k: v for k, v in risk_data.items() if k in RiskConfig.__dataclass_fields__}
        ),
    )
