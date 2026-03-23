from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException

if TYPE_CHECKING:
    from src.services.portfolio_engine import PortfolioEngine
    from src.services.risk_engine import RiskEngine
    from src.services.signal_engine import SignalEngine

app = FastAPI(title="Stale Odds Hunter", docs_url="/docs")

_signal_engine: SignalEngine | None = None
_risk_engine: RiskEngine | None = None
_portfolio_engine: PortfolioEngine | None = None


def configure(
    signal_engine: SignalEngine,
    risk_engine: RiskEngine,
    portfolio_engine: PortfolioEngine,
) -> None:
    global _signal_engine, _risk_engine, _portfolio_engine
    _signal_engine = signal_engine
    _risk_engine = risk_engine
    _portfolio_engine = portfolio_engine


@app.get("/health")
async def health() -> dict:
    halted = _risk_engine.is_halted if _risk_engine else False
    return {"status": "halted" if halted else "ok", "mode": "paper"}


@app.get("/portfolio")
async def get_portfolio() -> dict:
    if not _portfolio_engine:
        raise HTTPException(503, "Portfolio engine not ready")
    return await _portfolio_engine.get_portfolio_summary()


@app.get("/risk")
async def get_risk() -> dict:
    if not _risk_engine:
        raise HTTPException(503, "Risk engine not ready")
    return await _risk_engine.get_risk_summary()


@app.post("/strategies/{name}/pause")
async def pause_strategy(name: str) -> dict:
    if not _signal_engine:
        raise HTTPException(503, "Signal engine not ready")
    _signal_engine.pause_strategy(name)
    return {"status": "paused", "strategy": name}


@app.post("/strategies/{name}/resume")
async def resume_strategy(name: str) -> dict:
    if not _signal_engine:
        raise HTTPException(503, "Signal engine not ready")
    _signal_engine.resume_strategy(name)
    return {"status": "resumed", "strategy": name}


@app.post("/risk/halt")
async def halt_trading() -> dict:
    if not _risk_engine:
        raise HTTPException(503, "Risk engine not ready")
    _risk_engine.halt("Manual halt via API")
    return {"status": "halted"}


@app.post("/risk/resume")
async def resume_trading() -> dict:
    if not _risk_engine:
        raise HTTPException(503, "Risk engine not ready")
    _risk_engine.resume()
    return {"status": "resumed"}
