"""Row envelope shared by every sink and both ingestion paths.

JSON Lines is the wire format for the file sink *and* for ADX, so a file replayed into the
cluster months later produces rows identical to the ones ingested live. Field names here map
directly to the ADX ingestion mappings in ``adx/tables/mappings.kql``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Kusto datetime is always UTC. Emitting a naive or local timestamp does not error — it
# silently shifts every dashboard — so formatting is centralised here and nowhere else.
_ISO_MICROSECONDS = "%Y-%m-%dT%H:%M:%S.%fZ"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: datetime) -> str:
    """Format as UTC ISO-8601 with an explicit Z suffix."""
    if ts.tzinfo is None:
        # A naive datetime from the driver is server-local; the connection pins the session
        # to +00:00, so treating it as UTC is correct rather than a guess.
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime(_ISO_MICROSECONDS)


class Envelope:
    """Builds tagged metric and event rows for one collector process."""

    def __init__(self, run_id: str, host: str, tier: str) -> None:
        self.run_id = run_id
        self.host = host
        self.tier = tier

    def metric(
        self, ts: datetime, source: str, name: str, value: float
    ) -> dict[str, Any]:
        return {
            "ts": iso(ts),
            "run_id": self.run_id,
            "host": self.host,
            "tier": self.tier,
            "source": source,
            "metric": name,
            "value": float(value),
        }

    def event(
        self,
        ts: datetime,
        source: str,
        level: str,
        error_code: str,
        subsystem: str,
        message: str,
    ) -> dict[str, Any]:
        return {
            "ts": iso(ts),
            "run_id": self.run_id,
            "host": self.host,
            "tier": self.tier,
            "source": source,
            "level": level,
            "error_code": error_code,
            "subsystem": subsystem,
            "message": message,
        }
