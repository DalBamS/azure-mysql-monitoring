"""Metric collection: status counters and statement digests.

Raw cumulative counters are stored as-is. Rates are derived at query time in KQL, so a
counter reset — which is real signal, usually a server restart — stays visible in the data
instead of being smoothed away at collection.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterator

from connection import load_sql
from envelope import Envelope, utc_now

log = logging.getLogger(__name__)

# performance_schema reports latency in picoseconds. Converting once, here, keeps the unit
# out of every dashboard query.
PICOSECONDS_PER_MS = 1_000_000_000


def collect_global_status(
    conn: Any, env: Envelope, ts: datetime | None = None
) -> Iterator[dict[str, Any]]:
    """Sample the curated status-variable allow-list."""
    ts = ts or utc_now()
    with conn.cursor() as cur:
        cur.execute(load_sql("global_status.sql"))
        rows = cur.fetchall()

    skipped = 0
    for name, raw in rows:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            # A handful of status variables are strings (e.g. Innodb_buffer_pool_dump_status).
            # The allow-list should exclude them; count rather than crash if one slips in.
            skipped += 1
            continue
        yield env.metric(ts, "global_status", name, value)

    if skipped:
        log.debug("skipped %d non-numeric status variables", skipped)


def collect_statement_digests(
    conn: Any, env: Envelope, limit: int = 20, ts: datetime | None = None
) -> Iterator[dict[str, Any]]:
    """Sample top statement digests as cumulative aggregates.

    Emitted as metrics keyed by digest so ADX can diff consecutive samples. The digest text
    itself is deliberately not emitted here: it can embed customer-shaped data, and the
    digest hash is enough to correlate with a slow-query log entry.
    """
    ts = ts or utc_now()
    with conn.cursor() as cur:
        cur.execute(load_sql("ps_statement_digest.sql"), (limit,))
        rows = cur.fetchall()

    for digest, _text, count_star, sum_timer, rows_examined, rows_sent, no_index, lock_time in rows:
        key = (digest or "unknown")[:16]
        yield env.metric(ts, "ps_digest", f"digest.{key}.count", float(count_star or 0))
        yield env.metric(
            ts, "ps_digest", f"digest.{key}.latency_ms",
            float(sum_timer or 0) / PICOSECONDS_PER_MS,
        )
        yield env.metric(
            ts, "ps_digest", f"digest.{key}.lock_ms",
            float(lock_time or 0) / PICOSECONDS_PER_MS,
        )
        yield env.metric(ts, "ps_digest", f"digest.{key}.rows_examined", float(rows_examined or 0))
        yield env.metric(ts, "ps_digest", f"digest.{key}.rows_sent", float(rows_sent or 0))
        yield env.metric(ts, "ps_digest", f"digest.{key}.no_index_used", float(no_index or 0))


def heartbeat(env: Envelope, ts: datetime | None = None) -> dict[str, Any]:
    """The row that makes collector death detectable.

    Without it a dead collector produces a flatline, and a flatline reads as a healthy idle
    server. This is emitted before any query work that could fail, so the heartbeat survives
    a MySQL outage and distinguishes "database down" from "collector down".
    """
    return env.metric(ts or utc_now(), "collector", "collector_heartbeat", 1.0)
