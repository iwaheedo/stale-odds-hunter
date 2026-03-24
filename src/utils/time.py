from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def timestamp_ms() -> int:
    return int(utc_now().timestamp() * 1000)


def from_timestamp_ms(ts: int) -> datetime:
    return datetime.fromtimestamp(ts / 1000, tz=UTC)


def seconds_since(dt: datetime) -> float:
    return (utc_now() - dt).total_seconds()
