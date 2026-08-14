from __future__ import annotations

import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

COLLECTOR = Path(__file__).parents[2] / "mysql-internal" / "collector"
sys.path.insert(0, str(COLLECTOR))

from catalog import CATALOG  # noqa: E402
from sinks.adx import SERIES_TABLE, TELEMETRY_TABLE  # noqa: E402
from sinks.spool import DurableSpoolSink, SpoolConfig  # noqa: E402
from telemetry import TelemetryContext, TelemetryPoint  # noqa: E402


class FakeAdxSink:
    def __init__(self, *, error: str | None = None) -> None:
        self.error = error
        self.last_error: str | None = None
        self.calls: list[tuple[str, str, list[dict]]] = []
        self.closed = False
        self.statuses: list[tuple[str, object]] = []

    def write_raw_rows(self, rows, table, mapping, ingestion_tag=None) -> list[str]:
        self.calls.append((table, mapping, list(rows)))
        self.last_error = self.error
        if self.error:
            return []
        source_id = str(uuid.uuid4())
        if ingestion_tag:
            self.statuses.append(
                ("success", SimpleNamespace(IngestionSourceId=source_id.upper()))
            )
        return [source_id]

    def pop_ingestion_statuses(self):
        statuses, self.statuses = self.statuses, []
        return statuses

    def close(self) -> None:
        self.closed = True


def sample_point(target_id: str = "orders") -> TelemetryPoint:
    return TelemetryPoint(
        observed_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
        context=TelemetryContext(
            run_id="prod",
            target_id=target_id,
            host=f"{target_id}.mysql.database.azure.com",
            tier="premium-ssd-v2",
        ),
        measurement="mysql.global_status",
        fields={"Threads_connected": 3.0},
    )


class DurableSpoolTests(unittest.TestCase):
    def build(self, root: Path, live: FakeAdxSink, queued: FakeAdxSink, limit=100_000):
        return DurableSpoolSink(
            live,
            queued,
            SpoolConfig(root, max_bytes=limit, replay_seconds=60),
            start_replay=False,
        )

    def test_successful_streaming_removes_persisted_segments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            live, queued = FakeAdxSink(), FakeAdxSink()
            sink = self.build(Path(directory), live, queued)

            sink.write_points([sample_point()], CATALOG)

            self.assertEqual(
                [call[0] for call in live.calls],
                [TELEMETRY_TABLE, SERIES_TABLE],
            )
            self.assertEqual(sink.pending_segments(), [])
            sink.close()

    def test_replay_ignores_batch_while_streaming_is_in_flight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queued = FakeAdxSink()

            class RacingLiveSink(FakeAdxSink):
                owner: DurableSpoolSink

                def write_raw_rows(
                    self, rows, table, mapping, ingestion_tag=None
                ) -> list[str]:
                    self.owner.replay_once()
                    return super().write_raw_rows(
                        rows, table, mapping, ingestion_tag
                    )

            live = RacingLiveSink()
            sink = self.build(Path(directory), live, queued)
            live.owner = sink

            sink.write_points([sample_point()], CATALOG)

            self.assertEqual(queued.calls, [])
            self.assertEqual(sink.pending_segments(), [])
            sink.close()

    def test_failed_streaming_is_replayed_through_queued_ingestion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            live, queued = FakeAdxSink(error="ADX unavailable"), FakeAdxSink()
            sink = self.build(Path(directory), live, queued)

            sink.write_points([sample_point()], CATALOG)
            self.assertEqual(len(sink.pending_segments()), 2)

            self.assertEqual(sink.replay_once(), 2)
            self.assertEqual(len(sink.pending_segments()), 2)
            self.assertEqual(sink.replay_once(), 0)
            self.assertEqual(sink.pending_segments(), [])
            self.assertEqual(
                [call[0] for call in queued.calls],
                [TELEMETRY_TABLE, SERIES_TABLE],
            )
            sink.close()

    def test_segments_are_partitioned_by_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            live = FakeAdxSink(error="ADX unavailable")
            sink = self.build(Path(directory), live, FakeAdxSink())

            sink.write_points([sample_point("orders"), sample_point("sessions")], CATALOG)

            parents = {path.parent.name for path in sink.pending_segments()}
            self.assertEqual(len(parents), 2)
            self.assertTrue(any(parent.startswith("orders-") for parent in parents))
            self.assertTrue(any(parent.startswith("sessions-") for parent in parents))
            sink.close()

    def test_full_spool_retains_existing_data_and_reports_dropped_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            live = FakeAdxSink(error="ADX unavailable")
            sink = self.build(Path(directory), live, FakeAdxSink(), limit=700)

            sink.write_points([sample_point()], CATALOG)

            self.assertIsNotNone(sink.last_error)
            self.assertIn("spool limit", sink.last_error)
            self.assertGreaterEqual(len(sink.pending_segments()), 1)
            self.assertTrue((Path(directory) / ".overflow").is_file())
            sink.close()


if __name__ == "__main__":
    unittest.main()
