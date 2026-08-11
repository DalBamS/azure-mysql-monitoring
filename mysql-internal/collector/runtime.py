"""Concurrent runtime for validated multi-target Collection Plans."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from config import Config, ConfigError
from connection import assert_tls, connect, server_identity
from envelope import Envelope
from events import ErrorLogReader
from plan import CollectionPlan, GroupPlan, Target
from secrets import SecretResolutionError, SecretResolver
from telemetry import TelemetryContext, TelemetryPoint

log = logging.getLogger(__name__)

INITIAL_RECONNECT_SECONDS = 1.0
MAX_RECONNECT_SECONDS = 30.0
DEFAULT_HEARTBEAT_SECONDS = 10.0
_SAFE_TARGET = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True, slots=True)
class PlanSinkConfig:
    """The global settings consumed by sinks in Collection Plan mode."""

    adx_ingest_uri: str = ""
    adx_cluster_uri: str = ""
    adx_database: str = ""

    @classmethod
    def from_env(cls) -> "PlanSinkConfig":
        return cls(
            adx_ingest_uri=os.environ.get("ADX_INGEST_URI", "").strip(),
            adx_cluster_uri=os.environ.get("ADX_CLUSTER_URI", "").strip(),
            adx_database=os.environ.get("ADX_DATABASE", "").strip(),
        )

    def require_adx(self) -> None:
        missing = [
            name
            for name, value in (
                ("ADX_INGEST_URI", self.adx_ingest_uri),
                ("ADX_DATABASE", self.adx_database),
            )
            if not value
        ]
        if missing:
            raise ConfigError(f"ADX sink selected but {', '.join(missing)} not set")


def cursor_path_for_target(cursor_dir: str | Path, target_id: str) -> Path:
    """Return a traversal-safe, collision-resistant cursor path for one Target."""

    safe = _SAFE_TARGET.sub("-", target_id).strip(".-") or "target"
    digest = hashlib.sha256(target_id.encode("utf-8")).hexdigest()[:10]
    return Path(cursor_dir) / f"{safe}-{digest}.json"


class SinkWriter:
    """Serialise shared sink access while keeping failures target-local."""

    def __init__(self, sinks: Sequence[Any]) -> None:
        self._sinks = tuple(sinks)
        self._lock = threading.Lock()

    def write_points(self, points: Sequence[TelemetryPoint]) -> None:
        if not points:
            return
        from catalog import CATALOG

        validated = [CATALOG.validate(point) for point in points]
        with self._lock:
            for sink in self._sinks:
                try:
                    sink.write_points(validated, CATALOG)
                except Exception as exc:  # noqa: BLE001 - one sink must not stop collection
                    log.error("%s sink write failed: %s", getattr(sink, "name", "unknown"), exc)

    def write_events(self, events: Sequence[dict[str, Any]]) -> None:
        if not events:
            return
        with self._lock:
            for sink in self._sinks:
                try:
                    sink.write_events(events)
                except Exception as exc:  # noqa: BLE001 - one sink must not stop collection
                    log.error("%s sink write failed: %s", getattr(sink, "name", "unknown"), exc)


class TargetWorker:
    """Own one Target's connection, reconnect state, and group schedules."""

    def __init__(
        self,
        target: Target,
        groups: Mapping[str, GroupPlan],
        *,
        registry: Mapping[
            str,
            Callable[[Any, TelemetryContext, GroupPlan, datetime], list[TelemetryPoint]],
        ],
        resolver: SecretResolver,
        sink_writer: SinkWriter,
        stop_event: threading.Event,
        cursor_dir: str | Path,
        collector_id: str,
        connector: Callable[[Config], Any] = connect,
        tls_checker: Callable[[Any], str] = assert_tls,
        identity_reader: Callable[[Any], Mapping[str, str]] = server_identity,
        error_reader_factory: Callable[[Envelope, Path], Any] = ErrorLogReader,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        initial_backoff: float = INITIAL_RECONNECT_SECONDS,
        max_backoff: float = MAX_RECONNECT_SECONDS,
    ) -> None:
        self.target = target
        self.groups = dict(groups)
        self.groups.setdefault(
            "collector-health",
            GroupPlan(
                "collector-health",
                timedelta(seconds=DEFAULT_HEARTBEAT_SECONDS),
            ),
        )
        self.registry = registry
        self.resolver = resolver
        self.sink_writer = sink_writer
        self.stop_event = stop_event
        self.connector = connector
        self.tls_checker = tls_checker
        self.identity_reader = identity_reader
        self.error_reader_factory = error_reader_factory
        self.monotonic = monotonic
        self.utcnow = utcnow
        self.initial_backoff = initial_backoff
        self.max_backoff = max_backoff
        self._backoff = initial_backoff
        self._next_reconnect = 0.0
        self._due = {name: 0.0 for name in self.groups}
        self._connection: Any | None = None
        self._context: TelemetryContext | None = None
        self._error_reader: Any | None = None
        self._error_reader_loaded = False
        self._collector_id = collector_id
        self._cursor_path = cursor_path_for_target(cursor_dir, target.target_id)
        self._last_cycle_duration_ms: float | None = None

    @property
    def reconnect_delay(self) -> float:
        return self._backoff

    @property
    def context(self) -> TelemetryContext | None:
        return self._context

    def run(self, *, max_cycles: int = 0) -> None:
        cycles = 0
        try:
            while not self.stop_event.is_set():
                now = self.monotonic()
                started = now
                self.run_cycle(now=now, ts=self.utcnow())
                self._last_cycle_duration_ms = (
                    self.monotonic() - started
                ) * 1000.0
                cycles += 1
                if max_cycles and cycles >= max_cycles:
                    return
                self.stop_event.wait(self.next_delay(self.monotonic()))
        except Exception:  # noqa: BLE001 - a worker must never take down sibling Targets
            log.exception("Target %s worker stopped unexpectedly", self.target.target_id)
        finally:
            self._close_connection()

    def run_cycle(self, *, now: float, ts: datetime) -> None:
        due_names = [name for name, due in self._due.items() if due <= now]
        if not due_names:
            return

        if not self._ensure_context(now):
            return
        assert self._context is not None

        # Ship liveness before a connection attempt can block or fail.
        if "collector-health" in due_names:
            self.sink_writer.write_points(
                [self._health_point(ts)]
            )
            self._advance("collector-health", now)
            due_names.remove("collector-health")

        if not due_names:
            return
        if self._connection is None and not self._ensure_connection(now):
            return

        for name in due_names:
            if name == "error-log":
                try:
                    self._write_error_log_events()
                    self._advance(name, now)
                except Exception as exc:  # noqa: BLE001 - reconnect this Target independently
                    log.error("Target %s error-log failed: %s", self.target.target_id, exc)
                    self._connection_failed(now)
                    break
                continue

            collector = self.registry.get(name)
            if collector is None:
                log.error(
                    "Target %s has no registered collector for %s",
                    self.target.target_id,
                    name,
                )
                self._advance(name, now)
                continue

            try:
                points = collector(self._connection, self._context, self.groups[name], ts)
                self.sink_writer.write_points(points)
                self._advance(name, now)
            except Exception as exc:  # noqa: BLE001 - reconnect this Target; siblings continue
                log.error(
                    "Target %s group %s failed: %s",
                    self.target.target_id,
                    name,
                    exc,
                )
                self._connection_failed(now)
                break

    def next_delay(self, now: float) -> float:
        if self._context is None:
            return max(0.0, self._next_reconnect - now)
        candidates = []
        for name, due in self._due.items():
            if name == "collector-health" or self._connection is not None:
                candidates.append(due)
        if self._connection is None and any(name != "collector-health" for name in self._due):
            candidates.append(self._next_reconnect)
        if not candidates:
            return 1.0
        return max(0.0, min(candidates) - now)

    def _ensure_context(self, now: float) -> bool:
        if self._context is not None:
            return True
        if now < self._next_reconnect:
            return False
        try:
            run_id = self.resolver.resolve(
                self.target.run_id, path=f"Target {self.target.target_id} run_id"
            )
            self._context = TelemetryContext(
                run_id=run_id,
                target_id=self.target.target_id,
                host=self.target.host,
                tier=self.target.tier,
                azure_resource_id=self.target.azure_resource_id,
                collector_id=self._collector_id,
            )
            if "error-log" in self.groups:
                self._error_reader = self.error_reader_factory(
                    Envelope(run_id=run_id, host=self.target.host, tier=self.target.tier),
                    self._cursor_path,
                )
            return True
        except SecretResolutionError as exc:
            log.error("%s", exc)
            self._schedule_reconnect(now)
            return False

    def _ensure_connection(self, now: float) -> bool:
        if self._connection is not None:
            return True
        if now < self._next_reconnect:
            return False
        try:
            cfg = Config(
                host=self.target.host,
                port=self.target.port,
                user=self.resolver.resolve(
                    self.target.username,
                    path=f"Target {self.target.target_id} credentials.username",
                ),
                password=self.resolver.resolve(
                    self.target.password,
                    path=f"Target {self.target.target_id} credentials.password",
                    strip=False,
                ),
                database=self.target.database,
                tier=self.target.tier,
                run_id=self._context.run_id if self._context else "",
                ssl_ca=self.target.ssl_ca,
            )
            self._connection = self.connector(cfg)
            cipher = self.tls_checker(self._connection)
            identity = self.identity_reader(self._connection)
            version = identity.get("version", "unknown")
            if not version.startswith("8.4"):
                raise RuntimeError(
                    f"Target {self.target.target_id} reports MySQL {version}; "
                    "only MySQL 8.4 is supported"
                )
            self._backoff = self.initial_backoff
            self._next_reconnect = now
            log.info(
                "Target %s connected to %s:%d (TLS %s)",
                self.target.target_id,
                self.target.host,
                self.target.port,
                cipher,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - retry this Target independently
            log.error("Target %s connection failed: %s", self.target.target_id, exc)
            self._close_connection()
            self._schedule_reconnect(now)
            return False

    def _health_point(self, ts: datetime) -> TelemetryPoint:
        assert self._context is not None
        fields = {
            "heartbeat": 1.0,
            "mysql_reachable": 1.0 if self._connection is not None else 0.0,
        }
        if self._last_cycle_duration_ms is not None:
            fields["cycle_duration_ms"] = self._last_cycle_duration_ms
        return TelemetryPoint(
            observed_at=ts,
            context=self._context,
            measurement="collector.health",
            fields=fields,
        )

    def _connection_failed(self, now: float) -> None:
        self._close_connection()
        self._schedule_reconnect(now)

    def _write_error_log_events(self) -> None:
        assert self._connection is not None
        assert self._error_reader is not None
        if not self._error_reader_loaded:
            self._error_reader.load_cursor(self._connection)
            self._error_reader_loaded = True
        events = list(self._error_reader.poll(self._connection))
        is_connected = getattr(self._connection, "is_connected", None)
        if callable(is_connected) and not is_connected():
            raise ConnectionError("MySQL connection was lost while reading error_log")
        self.sink_writer.write_events(events)

    def _schedule_reconnect(self, now: float) -> None:
        delay = self._backoff
        self._next_reconnect = now + delay
        self._backoff = min(self.max_backoff, max(self.initial_backoff, delay * 2))

    def _advance(self, name: str, now: float) -> None:
        interval = self.groups[name].interval.total_seconds()
        due = self._due[name]
        while due <= now:
            due += interval
        self._due[name] = due

    def _close_connection(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                log.warning("Target %s connection close failed: %s", self.target.target_id, exc)


class CollectionPlanRuntime:
    """Run one independent worker and MySQL connection per Target."""

    def __init__(
        self,
        plan: CollectionPlan,
        sinks: Sequence[Any],
        *,
        resolver: SecretResolver | None = None,
        registry: Mapping[str, Callable[..., list[TelemetryPoint]]] | None = None,
        cursor_dir: str | Path = ".error_log_cursors",
        stop_event: threading.Event | None = None,
        connector: Callable[[Config], Any] = connect,
        tls_checker: Callable[[Any], str] = assert_tls,
        identity_reader: Callable[[Any], Mapping[str, str]] = server_identity,
        error_reader_factory: Callable[[Envelope, Path], Any] = ErrorLogReader,
        collector_id: str | None = None,
    ) -> None:
        if registry is None:
            from groups import GROUP_COLLECTORS

            registry = GROUP_COLLECTORS
        configured_groups = {
            name
            for target in plan.targets
            for name in plan.profiles[target.profile].groups
            if name not in {"collector-health", "error-log"}
        }
        missing_collectors = configured_groups - set(registry)
        if missing_collectors:
            raise ValueError(
                "Collection Plan contains groups with no registered collector: "
                + ", ".join(sorted(missing_collectors))
            )
        self.plan = plan
        self.resolver = resolver or SecretResolver()
        self.stop_event = stop_event or threading.Event()
        self.sink_writer = SinkWriter(sinks)
        identity = collector_id or (
            f"{os.environ.get('COMPUTERNAME', 'collector')}-{uuid.uuid4().hex[:8]}"
        )
        self.workers = [
            TargetWorker(
                target,
                plan.profiles[target.profile].groups,
                registry=registry,
                resolver=self.resolver,
                sink_writer=self.sink_writer,
                stop_event=self.stop_event,
                cursor_dir=cursor_dir,
                collector_id=identity,
                connector=connector,
                tls_checker=tls_checker,
                identity_reader=identity_reader,
                error_reader_factory=error_reader_factory,
            )
            for target in plan.targets
        ]
        self._threads: list[threading.Thread] = []

    def run(self, *, max_cycles: int = 0) -> int:
        self._threads = [
            threading.Thread(
                target=worker.run,
                kwargs={"max_cycles": max_cycles},
                name=f"mysql-target-{worker.target.target_id}",
            )
            for worker in self.workers
        ]
        try:
            for thread in self._threads:
                thread.start()
            for thread in self._threads:
                thread.join()
            return 0
        finally:
            self.stop_event.set()
            for thread in self._threads:
                if thread.is_alive():
                    thread.join()
            self.resolver.close()

    def shutdown(self) -> None:
        self.stop_event.set()
