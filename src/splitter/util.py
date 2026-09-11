"""Small shared helpers."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Naive UTC now — all DB timestamps are naive UTC."""
    return datetime.now(UTC).replace(tzinfo=None)
