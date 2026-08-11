from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

COLLECTOR = Path(__file__).parents[2] / "mysql-internal" / "collector"
sys.path.insert(0, str(COLLECTOR))

from catalog import CATALOG  # noqa: E402
from groups import (  # noqa: E402
    GROUP_COLLECTORS,
    collect_file_io,
    collect_global_status,
    collect_global_variables,
    collect_index_io,
    collect_innodb_metrics,
    collect_process_states,
    collect_schema_size,
    collect_statement_digests,
    collect_table_io,
)
from plan import GroupPlan  # noqa: E402
from telemetry import TelemetryContext  # noqa: E402
from telemetry import FieldKind  # noqa: E402


class Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.params = ()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, _sql, params=()):
        self.params = params

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, rows):
        self.cursor_instance = Cursor(rows)

    def cursor(self):
        return self.cursor_instance


class CollectionGroupTests(unittest.TestCase):
    def setUp(self):
        self.context = TelemetryContext(
            run_id="run-1",
            target_id="db-1",
            host="db.mysql.database.azure.com",
            tier="premium-ssd-v2",
        )
        self.ts = datetime(2026, 8, 11, tzinfo=timezone.utc)

    def plan(self, name, top_k=None):
        return GroupPlan(name, timedelta(seconds=30), top_k)

    def test_registry_covers_every_point_collection_group(self):
        self.assertEqual(
            set(GROUP_COLLECTORS),
            {
                "global-status",
                "innodb-metrics",
                "global-variables",
                "file-io",
                "process-states",
                "statement-digests",
                "schema-size",
                "table-io",
                "index-io",
            },
        )

    def test_global_status_uses_catalog_as_query_allow_list(self):
        conn = Connection([("Queries", "42"), ("Threads_running", "3")])
        point = collect_global_status(
            conn, self.context, self.plan("global-status"), self.ts
        )[0]
        self.assertEqual(point.fields["Queries"], 42.0)
        self.assertGreater(len(conn.cursor_instance.params), 50)

    def test_average_row_lock_time_is_a_gauge(self):
        field = CATALOG.measurement("mysql.global_status").fields[
            "Innodb_row_lock_time_avg"
        ]
        self.assertEqual(field.kind, FieldKind.GAUGE)

    def test_file_and_statement_latency_are_converted_from_picoseconds(self):
        file_points = collect_file_io(
            Connection([("wait/io/file/innodb/innodb_data_file", 2, 4096, 2_000_000_000, 1, 512, 500_000_000, 0, 0)]),
            self.context,
            self.plan("file-io", 10),
            self.ts,
        )
        read = next(point for point in file_points if point.tags["mode"] == "read")
        self.assertEqual(read.fields["wait_ms_total"], 2.0)
        self.assertEqual(read.fields["bytes_total"], 4096)

        digest = collect_statement_digests(
            Connection([("app", "abc", 3, 4_000_000_000, 1_000_000_000, 30, 2, 1, 2, 3, 1, 1, 2_000_000_000, 3_000_000_000)]),
            self.context,
            self.plan("statement-digests", 10),
            self.ts,
        )[0]
        self.assertEqual(digest.fields["latency_ms_total"], 4.0)
        self.assertEqual(digest.fields["latency_p99_ms"], 3.0)
        self.assertNotIn("digest_text", digest.fields)

    def test_innodb_type_selects_counter_or_gauge_semantics(self):
        points = collect_innodb_metrics(
            Connection(
                [
                    ("buffer_pool_reads", "buffer", "status_counter", 10),
                    ("buffer_pool_size", "buffer", "value", 100.5),
                ]
            ),
            self.context,
            self.plan("innodb-metrics"),
            self.ts,
        )
        self.assertEqual(points[0].fields, {"counter_value": 10})
        self.assertEqual(points[1].fields, {"gauge_value": 100.5})

    def test_dimension_groups_preserve_dimensions_and_bounds(self):
        variables = collect_global_variables(
            Connection([("max_connections", "151"), ("version", "8.4.4")]),
            self.context,
            self.plan("global-variables"),
            self.ts,
        )
        self.assertEqual(variables[0].fields, {"numeric_value": 151.0})
        self.assertEqual(variables[1].fields, {"text_value": "8.4.4"})

        process = collect_process_states(
            Connection([("Sleep", None, 4, 20)]),
            self.context,
            self.plan("process-states"),
            self.ts,
        )[0]
        self.assertEqual(process.tags["state"], "(none)")

        schema = collect_schema_size(
            Connection([("app", "orders", "InnoDB", 10, 100, 20, 5)]),
            self.context,
            self.plan("schema-size", 7),
            self.ts,
        )[0]
        self.assertEqual(schema.fields["index_bytes"], 20)

        table = collect_table_io(
            Connection([("app", "orders", 2, 2_000_000_000, 1, 1_000_000_000, 0, 0, 0, 0)]),
            self.context,
            self.plan("table-io", 8),
            self.ts,
        )[0]
        self.assertEqual(table.fields["fetch_wait_ms_total"], 2.0)

        index = collect_index_io(
            Connection([("app", "orders", "PRIMARY", 2, 2_000_000_000)]),
            self.context,
            self.plan("index-io", 9),
            self.ts,
        )[0]
        self.assertEqual(index.tags["index"], "PRIMARY")
        self.assertEqual(index.fields["fetch_wait_ms_total"], 2.0)


if __name__ == "__main__":
    unittest.main()
