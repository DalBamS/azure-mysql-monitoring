"""Incremental read of ``performance_schema.error_log``.

This table is the only route to MySQL error-log content on Flexible Server — Azure's
diagnostic settings offer ``MySQL Audit Logs`` and ``MySQL Slow Logs`` and nothing else.

It is a **ring buffer**: entries are evicted as new ones arrive. Two consequences drive the
design here.

1. Polling must outpace eviction, or data is lost permanently with no error raised.
2. The read cursor must survive a restart, or the collector either re-ingests the whole
   buffer or skips whatever arrived while it was down.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from connection import load_sql
from envelope import Envelope, utc_now

log = logging.getLogger(__name__)

# Bounds one poll after a long stall so a backlog cannot produce a single enormous batch
# that blows past the 4 MB streaming-ingestion limit.
MAX_ROWS_PER_POLL = 500


class ErrorLogReader:
    """Cursor-based reader over the error_log ring buffer."""

    def __init__(self, env: Envelope, cursor_path: Path) -> None:
        self.env = env
        self.cursor_path = cursor_path
        self.cursor: datetime | None = None
        self.available = True

    def load_cursor(self, conn: Any) -> None:
        """Restore the persisted cursor, or establish one from the newest buffered entry."""
        persisted = self._read_cursor_file()
        if persisted is not None:
            self.cursor = persisted
            log.info("error_log cursor restored from %s: %s", self.cursor_path, persisted)
            return

        # Cold start: begin at the newest entry rather than replaying the whole buffer.
        # Those entries are historical and would land far outside the current run window.
        try:
            with conn.cursor() as cur:
                cur.execute(load_sql("error_log_bootstrap.sql"))
                row = cur.fetchone()
        except Exception as exc:  # noqa: BLE001 - probing capability, not handling a fault
            self.available = False
            log.warning(
                "performance_schema.error_log is not readable (%s). Error events will not be "
                "collected; metrics are unaffected.", exc,
            )
            return

        newest = row[0] if row else None
        self.cursor = _as_utc(newest) if newest else utc_now()
        log.info("error_log cursor initialised at %s", self.cursor)

    def poll(self, conn: Any) -> Iterator[dict[str, Any]]:
        """Yield event rows logged since the cursor, then advance it."""
        if not self.available or self.cursor is None:
            return

        try:
            with conn.cursor() as cur:
                cur.execute(load_sql("error_log.sql"), (self.cursor, MAX_ROWS_PER_POLL))
                rows = cur.fetchall()
        except Exception as exc:  # noqa: BLE001 - a read failure must not kill the poll loop
            log.warning("error_log poll failed: %s", exc)
            return

        newest = self.cursor
        for logged, prio, error_code, subsystem, data in rows:
            ts = _as_utc(logged)
            if newest is None or ts > newest:
                newest = ts
            yield self.env.event(
                ts=ts,
                source="error_log",
                level=str(prio or ""),
                error_code=str(error_code or ""),
                subsystem=str(subsystem or ""),
                message=str(data or ""),
            )

        if rows:
            self.cursor = newest
            self._write_cursor_file(newest)
            if len(rows) == MAX_ROWS_PER_POLL:
                log.warning(
                    "error_log poll hit the %d-row cap; the buffer is filling faster than it "
                    "is being read and entries may already have been evicted.",
                    MAX_ROWS_PER_POLL,
                )

    def _read_cursor_file(self) -> datetime | None:
        if not self.cursor_path.is_file():
            return None
        try:
            payload = json.loads(self.cursor_path.read_text(encoding="utf-8"))
            return datetime.fromisoformat(payload["cursor"])
        except (ValueError, KeyError, OSError) as exc:
            log.warning("ignoring unreadable cursor file %s: %s", self.cursor_path, exc)
            return None

    def _write_cursor_file(self, ts: datetime | None) -> None:
        if ts is None:
            return
        try:
            self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
            self.cursor_path.write_text(
                json.dumps({"cursor": ts.isoformat()}), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("could not persist error_log cursor: %s", exc)


def _as_utc(value: Any) -> datetime:
    """Normalise a driver TIMESTAMP(6) to an aware UTC datetime.

    The session is pinned to +00:00 in ``connection.connect``, so a naive value from the
    driver is already UTC and is labelled rather than converted.
    """
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return utc_now()
