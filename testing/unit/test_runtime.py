from __future__ import annotations

import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

COLLECTOR = Path(__file__).parents[2] / "mysql-internal" / "collector"
sys.path.insert(0, str(COLLECTOR))

from plan import (  # noqa: E402
    CollectionPlan,
    EnvReference,
    GroupPlan,
    Profile,
    Target,
)
from runtime import (  # noqa: E402
    CollectionPlanRuntime,
    SinkWriter,
    TargetWorker,
    cursor_path_for_target,
)
from secrets import SecretResolver  # noqa: E402
from telemetry import ContractError, TelemetryContext, TelemetryPoint  # noqa: E402
from collector import parse_args  # noqa: E402

TS = datetime(2026, 8, 11, tzinfo=timezone.utc)


class FakeSink:
    name = "fake"

    def __init__(self) -> None:
        self.points: list[TelemetryPoint] = []
        self.events: list[dict] = []

    def write_points(self, points: list[TelemetryPoint], _catalog) -> None:
        self.points.extend(points)

    def write_events(self, events: list[dict]) -> None:
        self.events.extend(events)


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def target(target_id: str = "orders", *, run_env: str = "RUN_ID", host: str = "db") -> Target:
    return Target(
        target_id=target_id,
        host=host,
        database="app",
        tier="premium-ssd-v2",
        profile="standard",
        username=EnvReference(f"{target_id.upper()}_USER"),
        password=EnvReference(f"{target_id.upper()}_PASSWORD"),
        run_id=EnvReference(run_env),
    )


def point(context, ts: datetime) -> TelemetryPoint:
    return TelemetryPoint(
        observed_at=ts,
        context=context,
        measurement="mysql.global_status",
        fields={"Threads_connected": 1.0},
    )


def worker(
    groups: dict[str, GroupPlan],
    registry,
    *,
    sink: FakeSink,
    connector,
    target_value: Target | None = None,
    initial_backoff: float = 1.0,
    max_backoff: float = 30.0,
    error_reader_factory=None,
) -> TargetWorker:
    selected = target_value or target()
    values = {
        "RUN_ID": "run-orders",
        f"{selected.target_id.upper()}_USER": "monitor",
        f"{selected.target_id.upper()}_PASSWORD": "password",
    }
    kwargs = {}
    if error_reader_factory is not None:
        kwargs["error_reader_factory"] = error_reader_factory
    return TargetWorker(
        selected,
        groups,
        registry=registry,
        resolver=SecretResolver(environ=values),
        sink_writer=SinkWriter([sink]),
        stop_event=threading.Event(),
        cursor_dir="cursors",
        collector_id="collector-1",
        connector=connector,
        tls_checker=lambda _connection: "TLS_AES_256_GCM_SHA384",
        identity_reader=lambda _connection: {"version": "8.4.4"},
        initial_backoff=initial_backoff,
        max_backoff=max_backoff,
        **kwargs,
    )


class RuntimeTests(unittest.TestCase):
    def test_config_argument_is_opt_in_for_legacy_compatibility(self) -> None:
        self.assertIsNone(parse_args([]).config)
        self.assertEqual(parse_args(["--config", "monitoring.yaml"]).config, "monitoring.yaml")

    def test_points_are_catalog_validated_before_sink_write(self) -> None:
        sink = FakeSink()
        invalid = TelemetryPoint(
            observed_at=TS,
            context=TelemetryContext(
                run_id="run",
                target_id="target",
                host="db",
                tier="premium-ssd-v2",
            ),
            measurement="not.in.catalog",
            fields={"value": 1.0},
        )

        with self.assertRaises(ContractError):
            SinkWriter([sink]).write_points([invalid])
        self.assertEqual(sink.points, [])

    def test_groups_run_on_independent_cadences(self) -> None:
        sink = FakeSink()
        calls: list[float] = []
        groups = {
            "collector-health": GroupPlan("collector-health", timedelta(seconds=5)),
            "global-status": GroupPlan("global-status", timedelta(seconds=10)),
        }

        def collect(_conn, context, _group, ts):
            calls.append(ts.timestamp())
            return [point(context, ts)]

        target_worker = worker(
            groups,
            {"global-status": collect},
            sink=sink,
            connector=lambda _cfg: FakeConnection(),
        )
        target_worker.run_cycle(now=0, ts=TS)
        target_worker.run_cycle(now=5, ts=TS + timedelta(seconds=5))
        target_worker.run_cycle(now=10, ts=TS + timedelta(seconds=10))

        self.assertEqual(len(calls), 2)
        self.assertEqual(
            [item.measurement for item in sink.points],
            [
                "collector.health",
                "mysql.global_status",
                "collector.health",
                "collector.health",
                "mysql.global_status",
            ],
        )
        self.assertEqual(sink.points[0].fields["mysql_reachable"], 0.0)
        self.assertEqual(sink.points[2].fields["mysql_reachable"], 1.0)

    def test_heartbeat_is_written_before_failed_connect_and_backoff_is_bounded(self) -> None:
        sink = FakeSink()
        attempts = 0

        def fail_connect(_cfg):
            nonlocal attempts
            self.assertTrue(sink.points)
            attempts += 1
            raise OSError("server unavailable")

        groups = {
            "collector-health": GroupPlan("collector-health", timedelta(seconds=5)),
            "global-status": GroupPlan("global-status", timedelta(seconds=10)),
        }
        target_worker = worker(
            groups,
            {"global-status": lambda *_args: []},
            sink=sink,
            connector=fail_connect,
            max_backoff=2,
        )

        target_worker.run_cycle(now=0, ts=TS)
        target_worker.run_cycle(now=0.5, ts=TS)
        target_worker.run_cycle(now=1, ts=TS + timedelta(seconds=1))
        target_worker.run_cycle(now=3, ts=TS + timedelta(seconds=3))

        self.assertEqual(attempts, 3)
        self.assertEqual(target_worker.reconnect_delay, 2)
        self.assertEqual(sink.points[0].measurement, "collector.health")
        self.assertEqual(sink.points[0].fields["mysql_reachable"], 0.0)

    def test_error_log_receives_target_specific_cursor(self) -> None:
        sink = FakeSink()
        seen = []
        groups = {"error-log": GroupPlan("error-log", timedelta(seconds=10))}

        class FakeReader:
            def __init__(self, _env, cursor_path):
                seen.append(cursor_path)
                self.loaded = False

            def load_cursor(self, _conn):
                self.loaded = True

            def poll(self, _conn):
                self.assert_loaded()
                return iter([{"source": "error_log", "message": "event"}])

            def assert_loaded(self):
                if not self.loaded:
                    raise AssertionError("cursor was not loaded")

        target_worker = worker(
            groups,
            {},
            sink=sink,
            connector=lambda _cfg: FakeConnection(),
            error_reader_factory=FakeReader,
        )
        target_worker.run_cycle(now=0, ts=TS)

        expected = cursor_path_for_target("cursors", "orders")
        self.assertEqual(seen, [expected])
        self.assertEqual(sink.events, [{"source": "error_log", "message": "event"}])

    def test_target_failure_does_not_prevent_sibling_collection(self) -> None:
        sink = FakeSink()
        group = GroupPlan("global-status", timedelta(seconds=10))
        targets = (
            target("bad", run_env="BAD_RUN", host="bad-db"),
            target("good", run_env="GOOD_RUN", host="good-db"),
        )
        plan = CollectionPlan(
            profiles={"standard": Profile("standard", {"global-status": group})},
            targets=targets,
        )
        resolver = SecretResolver(
            environ={
                "BAD_RUN": "run-bad",
                "BAD_USER": "monitor",
                "BAD_PASSWORD": "bad",
                "GOOD_RUN": "run-good",
                "GOOD_USER": "monitor",
                "GOOD_PASSWORD": "good",
            }
        )
        good_connections: list[FakeConnection] = []

        def connect_target(cfg):
            if cfg.host == "bad-db":
                raise OSError("offline")
            connection = FakeConnection()
            good_connections.append(connection)
            return connection

        def collect(_conn, context, _group, ts):
            return [point(context, ts)]

        runtime = CollectionPlanRuntime(
            plan,
            [sink],
            resolver=resolver,
            registry={"global-status": collect},
            connector=connect_target,
            tls_checker=lambda _conn: "TLS",
            identity_reader=lambda _conn: {"version": "8.4.4"},
            collector_id="collector-1",
        )
        self.assertEqual(runtime.run(max_cycles=1), 0)

        collected = [p for p in sink.points if p.measurement == "mysql.global_status"]
        self.assertEqual([p.context.target_id for p in collected], ["good"])
        self.assertEqual(collected[0].context.run_id, "run-good")
        heartbeats = [p for p in sink.points if p.measurement == "collector.health"]
        self.assertEqual({p.context.target_id for p in heartbeats}, {"bad", "good"})
        self.assertEqual(
            {p.context.run_id for p in heartbeats},
            {"run-bad", "run-good"},
        )
        self.assertTrue(good_connections[0].closed)

    def test_cursor_paths_are_safe_and_unique(self) -> None:
        first = cursor_path_for_target("state", "../orders")
        second = cursor_path_for_target("state", "orders")

        self.assertEqual(first.parent, Path("state"))
        self.assertNotEqual(first, second)
        self.assertNotIn("..", first.name)


if __name__ == "__main__":
    unittest.main()
