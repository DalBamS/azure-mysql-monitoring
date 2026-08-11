"""Append-only JSON Lines sink.

Doubles as the benchmark artifact and the local buffer for the cold path: if ADX rejects a
window, the file is what lets you replay it. Standard library only.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Sequence, TextIO

log = logging.getLogger(__name__)


class JsonlSink:
    name = "jsonl"

    def __init__(self, out_path: str | None) -> None:
        self.path = Path(out_path) if out_path else None
        self.last_error: str | None = None
        self._fh: TextIO

        if self.path is None:
            self._fh = sys.stdout
            self._owns_handle = False
            log.info("jsonl sink writing to stdout")
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Append, never truncate: re-running a collector against an existing RUN_ID must
            # extend the archive rather than destroy it.
            self._fh = self.path.open("a", encoding="utf-8")
            self._owns_handle = True
            log.info("jsonl sink appending to %s", self.path)

    def _write(self, rows: Sequence[dict[str, Any]]) -> None:
        if not rows:
            return
        try:
            for row in rows:
                self._fh.write(json.dumps(row, separators=(",", ":")) + "\n")
            # Flush every batch so a killed process loses at most one interval, and so the
            # verification script can read the file while collection is still running.
            self._fh.flush()
            self.last_error = None
        except OSError as exc:
            self.last_error = str(exc)
            log.error("jsonl write failed: %s", exc)

    def write_metrics(self, rows: Sequence[dict[str, Any]]) -> None:
        self._write(rows)

    def write_events(self, rows: Sequence[dict[str, Any]]) -> None:
        self._write(rows)

    def write_points(self, points: Sequence[Any], _catalog: Any) -> None:
        self._write([point.packed_row() for point in points])

    def close(self) -> None:
        if self._owns_handle:
            self._fh.close()
