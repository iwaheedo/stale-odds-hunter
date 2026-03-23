# Stale Odds Hunter

Local-first prediction market trading bot for Polymarket. Paper-trading by default. Live trading disabled unless explicitly enabled.

## Quick Start

```bash
# 1. Clone and enter
cd stale-odds-hunter

# 2. Create environment
cp .env.example .env

# 3. Install dependencies
make setup

# 4. Run the bot (paper mode)
make run

# 5. Open dashboard (separate terminal)
make dashboard
```

The bot will:
- Discover active Polymarket markets
- Stream live orderbook data via WebSocket
- Compute fair value and detect mispricings
- Paper-trade automatically based on configured strategy
- Serve a dashboard at http://localhost:8501
- Expose a control API at http://localhost:8000

## Docker

```bash
cp .env.example .env
docker compose up
```

## Commands

| Command | Description |
|---------|-------------|
| `make setup` | Install dependencies |
| `make run` | Start bot in paper mode |
| `make dashboard` | Launch Streamlit dashboard |
| `make test` | Run tests |
| `make lint` | Run ruff + mypy |
| `make format` | Auto-format code |
| `make backtest` | Run backtest on historical data |
| `make clean` | Remove local databases |

## Configuration

All config is in `config/`:

- `app.yaml` — mode, intervals, database paths
- `markets.yaml` — which markets to track
- `strategies.yaml` — strategy parameters and thresholds
- `risk.yaml` — position limits, drawdown caps, kill switches

Override any value via environment variable: `SOH_APP__LOG_LEVEL=DEBUG`

## Architecture

```
Market Discovery → WebSocket Ingestion → Market State
                                              ↓
                                        Signal Engine
                                              ↓
                                        Risk Engine
                                              ↓
                                      Paper Execution
                                              ↓
                                      Portfolio Engine
```

All services communicate via an in-process async event bus. SQLite stores operational data. Streamlit reads the same database for the dashboard.

## Live Trading

Live trading is **off by default**. To enable (only from eligible regions):

1. Set `LIVE_TRADING_ENABLED=true` in `.env`
2. Add your wallet private key to `POLYMARKET_PRIVATE_KEY`
3. The bot will check geoblock status before starting

## Stack

- Python 3.12, httpx, websockets, asyncio
- FastAPI (control API), Streamlit (dashboard)
- SQLite (storage), DuckDB (analytics)
- No paid APIs, no cloud, no LLM calls at runtime
