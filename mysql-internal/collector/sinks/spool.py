"""Crash-safe local spool for ADX streaming ingestion.

Each batch is projected to its final ADX table, fsynced as append-only JSONL,
and only then offered to streaming ingestion. Failed batches remain on disk and
are submitted through queued ingestion by a background replay worker.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from sinks.adx import (
    EVENTS_MAPPING,
    EVENTS_TABLE,
    METRICS_MAPPING,
    METRICS_TABLE,
    SERIES_MAPPING,
    SERIES_TABLE,
    TELEMETRY_MAPPING,
    TELEMETRY_TABLE,
    MAX_ROWS_PER_REQUEST,
)

log = logging.getLogger(__name__)

DEFAULT_SPOOL_DIR = "/var/lib/azure-mysql-monitoring/spool"
DEFAULT_MAX_BYTES = 1024 * 1024 * 1024
DEFAULT_REPLAY_SECONDS = 30.0
DEFAULT_CONFIRMATION_TIMEOUT_SECONDS = 3600.0
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True, slots=True)
class SpoolConfig:
    directory: Path
    max_bytes: int = DEFAULT_MAX_BYTES
    replay_seconds: float = DEFAULT_REPLAY_SECONDS
    confirmation_timeout_seconds: float = DEFAULT_CONFIRMATION_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls) -> "SpoolConfig":
        directory = Path(os.environ.get("COLLECTOR_SPOOL_DIR", DEFAULT_SPOOL_DIR))
        try:
            max_bytes = int(
                os.environ.get("COLLECTOR_SPOOL_MAX_BYTES", str(DEFAULT_MAX_BYTES))
            )
            replay_seconds = float(
                os.environ.get(
                    "COLLECTOR_SPOOL_REPLAY_SECONDS",
                    str(DEFAULT_REPLAY_SECONDS),
                )
            )
            confirmation_timeout_seconds = float(
                os.environ.get(
                    "COLLECTOR_SPOOL_CONFIRMATION_TIMEOUT_SECONDS",
                    str(DEFAULT_CONFIRMATION_TIMEOUT_SECONDS),
                )
            )
        except ValueError as exc:
            raise ValueError(
                "COLLECTOR_SPOOL_MAX_BYTES, COLLECTOR_SPOOL_REPLAY_SECONDS and "
                "COLLECTOR_SPOOL_CONFIRMATION_TIMEOUT_SECONDS must be numeric"
            ) from exc
        if max_bytes <= 0:
            raise ValueError("COLLECTOR_SPOOL_MAX_BYTES must be greater than zero")
        if replay_seconds <= 0:
            raise ValueError("COLLECTOR_SPOOL_REPLAY_SECONDS must be greater than zero")
        if confirmation_timeout_seconds <= 0:
            raise ValueError(
                "COLLECTOR_SPOOL_CONFIRMATION_TIMEOUT_SECONDS must be greater than zero"
            )
        return cls(
            directory,
            max_bytes,
            replay_seconds,
            confirmation_timeout_seconds,
        )


class DurableSpoolSink:
    """ADX sink with bounded disk persistence and queued replay."""

    name = "adx-resilient"

    def __init__(
        self,
        live_sink: Any,
        replay_sink: Any,
        config: SpoolConfig,
        *,
        start_replay: bool = True,
    ) -> None:
        self.live_sink = live_sink
        self.replay_sink = replay_sink
        self.config = config
        self.last_error: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._replay_thread: threading.Thread | None = None
        self.config.directory.mkdir(parents=True, exist_ok=True)
        self._recover_temporary_segments()
        if start_replay:
            self._replay_thread = threading.Thread(
                target=self._replay_loop,
                name="adx-spool-replay",
                daemon=True,
            )
            self._replay_thread.start()
        log.info(
            "durable ADX spool ready (directory=%s, limit=%d bytes)",
            self.config.directory,
            self.config.max_bytes,
        )

    @classmethod
    def from_adx_config(
        cls,
        adx_config: Any,
        spool_config: SpoolConfig,
    ) -> "DurableSpoolSink":
        from sinks.adx import AdxSink

        return cls(
            AdxSink(adx_config, streaming=True),
            AdxSink(adx_config, streaming=False),
            spool_config,
        )

    def write_metrics(self, rows: Sequence[dict[str, Any]]) -> None:
        self._write_projected(rows, METRICS_TABLE, METRICS_MAPPING)

    def write_events(self, rows: Sequence[dict[str, Any]]) -> None:
        self._write_projected(rows, EVENTS_TABLE, EVENTS_MAPPING)

    def write_points(self, points: Sequence[Any], catalog: Any) -> None:
        packed = [point.packed_row() for point in points]
        series = [row for point in points for row in catalog.series_rows(point)]
        self._write_projected(packed, TELEMETRY_TABLE, TELEMETRY_MAPPING)
        self._write_projected(series, SERIES_TABLE, SERIES_MAPPING)

    def _write_projected(
        self,
        rows: Sequence[dict[str, Any]],
        table: str,
        mapping: str,
    ) -> None:
        if not rows:
            return
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            identity = str(row.get("target_id") or row.get("host") or "legacy")
            groups.setdefault(identity, []).append(row)

        errors = []
        for target, target_rows in groups.items():
            for start in range(0, len(target_rows), MAX_ROWS_PER_REQUEST):
                chunk = target_rows[start : start + MAX_ROWS_PER_REQUEST]
                try:
                    segment = self._persist(target, table, mapping, chunk)
                except OSError as exc:
                    self._mark_overflow(str(exc))
                    message = (
                        f"spool write rejected for {target}/{table}: {exc}. "
                        "Existing segments are retained; newest telemetry is being dropped."
                    )
                    log.critical(message)
                    errors.append(message)
                    continue

                self.live_sink.write_raw_rows(chunk, table, mapping)
                if self.live_sink.last_error:
                    errors.append(str(self.live_sink.last_error))
                    log.warning(
                        "streaming ingestion failed for %s/%s; retained %s for queued replay",
                        target,
                        table,
                        segment,
                    )
                else:
                    self._remove_segment(segment)

        self.last_error = "; ".join(errors) if errors else None

    def _persist(
        self,
        target: str,
        table: str,
        mapping: str,
        rows: Sequence[dict[str, Any]],
    ) -> Path:
        target_dir = self.config.directory / _safe_component(target)
        target_dir_existed = target_dir.is_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        if not target_dir_existed:
            _fsync_directory(self.config.directory)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        stem = f"{stamp}-{uuid.uuid4().hex}-{table}"
        temporary = target_dir / f"{stem}.tmp"
        ready = target_dir / f"{stem}.ready.jsonl"
        encoded = [
            json.dumps(
                {"table": table, "mapping": mapping, "row": row},
                separators=(",", ":"),
            )
            + "\n"
            for row in rows
        ]
        required = sum(len(line.encode("utf-8")) for line in encoded)

        with self._lock:
            current = self.pending_bytes()
            if current + required > self.config.max_bytes:
                raise OSError(
                    f"spool limit {self.config.max_bytes} bytes exceeded "
                    f"(pending={current}, new={required})"
                )
            with temporary.open("x", encoding="utf-8") as handle:
                for line in encoded:
                    handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(ready)
            _fsync_directory(target_dir)
        return ready

    def _mark_overflow(self, reason: str) -> None:
        marker = self.config.directory / ".overflow"
        try:
            with marker.open("w", encoding="utf-8") as handle:
                handle.write(
                    f"{datetime.now(timezone.utc).isoformat()} {reason}\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_directory(self.config.directory)
        except OSError as exc:
            log.critical("could not persist spool overflow marker: %s", exc)

    def pending_segments(self) -> list[Path]:
        return sorted(
            [
                *self.config.directory.glob("*/*.ready.jsonl"),
                *self.config.directory.glob("*/*.submitted.*.jsonl"),
                *self.config.directory.glob("*/*.failed.jsonl"),
            ]
        )

    def pending_bytes(self) -> int:
        total = 0
        for path in self.config.directory.glob("*/*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except FileNotFoundError:
                # The replay thread may have removed a segment returned by glob.
                continue
        return total

    def replay_once(self) -> int:
        completed = self._apply_terminal_statuses()
        self._retry_unconfirmed_submissions()
        submitted = 0
        for segment in sorted(self.config.directory.glob("*/*.ready.jsonl")):
            try:
                table, mapping, rows = _read_segment(segment)
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                corrupt = segment.with_suffix(segment.suffix + ".corrupt")
                segment.replace(corrupt)
                log.critical(
                    "spool segment %s is unreadable and was quarantined as %s: %s",
                    segment,
                    corrupt,
                    exc,
                )
                self.last_error = str(exc)
                continue

            ingestion_tag = f"spool:{_segment_id(segment)}"
            source_ids = self.replay_sink.write_raw_rows(
                rows,
                table,
                mapping,
                ingestion_tag=ingestion_tag,
            )
            if self.replay_sink.last_error:
                self.last_error = str(self.replay_sink.last_error)
                log.warning("queued replay stopped at %s: %s", segment, self.last_error)
                break
            if len(source_ids) != 1:
                self.last_error = (
                    f"queued replay for {segment} returned {len(source_ids)} source IDs; "
                    "segment retained"
                )
                log.error("%s", self.last_error)
                break
            submitted_path = segment.with_name(
                segment.name.replace(
                    ".ready.jsonl", f".submitted.{source_ids[0]}.jsonl"
                )
            )
            segment.replace(submitted_path)
            os.utime(submitted_path, None)
            _fsync_directory(segment.parent)
            submitted += 1
        if completed and not self.pending_segments():
            self.last_error = None
        return submitted

    def _apply_terminal_statuses(self) -> int:
        completed = 0
        submitted = {
            _submitted_source_id(path): path
            for path in self.config.directory.glob("*/*.submitted.*.jsonl")
        }
        for outcome, message in self.replay_sink.pop_ingestion_statuses():
            source_id = str(getattr(message, "IngestionSourceId", ""))
            segment = submitted.get(source_id)
            if segment is None:
                log.debug("ignoring queued ingestion status for unknown source %s", source_id)
                continue
            if outcome == "success":
                self._remove_segment(segment)
                completed += 1
                continue

            should_retry = getattr(message, "ShouldRetry", False)
            if should_retry is True or str(should_retry).lower() == "true":
                ready = _ready_path(segment)
                segment.replace(ready)
                _fsync_directory(segment.parent)
                log.warning("queued ingestion requested retry for %s", ready)
            else:
                failed = _failed_path(segment)
                segment.replace(failed)
                _fsync_directory(segment.parent)
                details = getattr(message, "Details", "unknown failure")
                self.last_error = f"terminal queued ingestion failure for {failed}: {details}"
                log.critical("%s", self.last_error)
        return completed

    def _retry_unconfirmed_submissions(self) -> None:
        cutoff = time.time() - self.config.confirmation_timeout_seconds
        for segment in self.config.directory.glob("*/*.submitted.*.jsonl"):
            try:
                stale = segment.stat().st_mtime < cutoff
            except FileNotFoundError:
                continue
            if not stale:
                continue
            ready = _ready_path(segment)
            segment.replace(ready)
            _fsync_directory(segment.parent)
            log.warning(
                "no ADX terminal status for %s within %.0fs; resubmitting with the same "
                "ingest-if-not-exists tag",
                ready,
                self.config.confirmation_timeout_seconds,
            )

    def _replay_loop(self) -> None:
        while not self._stop.wait(self.config.replay_seconds):
            try:
                self.replay_once()
            except Exception:  # noqa: BLE001 - replay must not terminate collection
                log.exception("unexpected durable spool replay failure")

    def _recover_temporary_segments(self) -> None:
        for temporary in self.config.directory.glob("*/*.tmp"):
            try:
                _read_segment(temporary)
                temporary.replace(temporary.with_suffix(".ready.jsonl"))
                _fsync_directory(temporary.parent)
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                corrupt = temporary.with_suffix(".corrupt")
                temporary.replace(corrupt)
                log.critical(
                    "incomplete spool segment %s quarantined as %s: %s",
                    temporary,
                    corrupt,
                    exc,
                )

    def _remove_segment(self, segment: Path) -> None:
        try:
            segment.unlink()
        except FileNotFoundError:
            return
        _fsync_directory(segment.parent)
        try:
            segment.parent.rmdir()
            _fsync_directory(self.config.directory)
        except OSError:
            pass

    def close(self) -> None:
        self._stop.set()
        if self._replay_thread is not None:
            self._replay_thread.join(timeout=max(1.0, self.config.replay_seconds + 1.0))
        self.live_sink.close()
        self.replay_sink.close()


def _safe_component(value: str) -> str:
    safe = _SAFE_COMPONENT.sub("-", value).strip(".-")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{safe[:100] or 'target'}-{digest}"


def _read_segment(
    path: Path,
) -> tuple[str, str, list[dict[str, Any]]]:
    table = ""
    mapping = ""
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            record: Mapping[str, Any] = json.loads(line)
            record_table = str(record["table"])
            record_mapping = str(record["mapping"])
            if table and (record_table != table or record_mapping != mapping):
                raise ValueError("segment mixes ADX tables or mappings")
            table, mapping = record_table, record_mapping
            row = record["row"]
            if not isinstance(row, dict):
                raise ValueError("segment row must be a JSON object")
            rows.append(row)
    if not rows:
        raise ValueError("segment is empty")
    return table, mapping, rows


def _segment_id(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _submitted_source_id(path: Path) -> str:
    marker = ".submitted."
    return path.name.split(marker, 1)[1].removesuffix(".jsonl")


def _ready_path(path: Path) -> Path:
    return path.with_name(path.name.split(".submitted.", 1)[0] + ".ready.jsonl")


def _failed_path(path: Path) -> Path:
    return path.with_name(path.name.split(".submitted.", 1)[0] + ".failed.jsonl")


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
