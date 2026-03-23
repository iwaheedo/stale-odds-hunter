from __future__ import annotations


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    if denominator == 0.0:
        return default
    return numerator / denominator


def calculate_edge(
    fair_value: float,
    market_price: float,
    fees: float = 0.0,
    slippage_buffer: float = 0.0,
    uncertainty_buffer: float = 0.0,
) -> float:
    """Positive edge means fair value > market price (buy signal)."""
    return fair_value - market_price - fees - slippage_buffer - uncertainty_buffer


def implied_probability_sum(prices: list[float]) -> float:
    """Sum of outcome prices — should be ~1.0 for a fair binary market."""
    return sum(prices)


def complement_fair_value(other_side_midpoint: float) -> float:
    """For binary markets: fair value of one side = 1.0 - other side."""
    return clamp(1.0 - other_side_midpoint, 0.0, 1.0)
