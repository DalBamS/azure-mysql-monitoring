from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

COLLECTOR = Path(__file__).parents[2] / "mysql-internal" / "collector"
sys.path.insert(0, str(COLLECTOR))

from telemetry import (  # noqa: E402
    Cardinality,
    ContractError,
    FieldKind,
    FieldSpec,
    MeasurementSpec,
    MetricCatalog,
    TelemetryContext,
    TelemetryPoint,
    legacy_metric_rows,
)


class TelemetryContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = TelemetryContext(
            run_id="prod",
            target_id="orders-db",
            host="orders.mysql.database.azure.com",
            tier="premium-ssd-v2",
            collector_id="collector-01",
        )
        self.catalog = MetricCatalog(
            [
                MeasurementSpec(
                    name="mysql.workload",
                    fields={
                        "questions_total": FieldSpec(
                            kind=FieldKind.COUNTER, unit="queries", value_type=int
                        ),
                        "threads_running": FieldSpec(
                            kind=FieldKind.GAUGE, unit="threads", value_type=int
                        ),
                    },
                ),
                MeasurementSpec(
                    name="mysql.file_io",
                    tags=frozenset({"event", "mode"}),
                    cardinality=Cardinality.BOUNDED,
                    fields={
                        "operations_total": FieldSpec(
                            kind=FieldKind.COUNTER, unit="operations", value_type=int
                        ),
                        "wait_ms_total": FieldSpec(
                            kind=FieldKind.COUNTER, unit="ms", value_type=float
                        ),
                    },
                ),
            ]
        )

    def point(self, **overrides):
        values = {
            "observed_at": datetime(2026, 8, 10, 12, 30, tzinfo=timezone.utc),
            "context": self.context,
            "measurement": "mysql.workload",
            "fields": {"questions_total": 100, "threads_running": 3},
        }
        values.update(overrides)
        return TelemetryPoint(**values)

    def test_rejects_naive_timestamp(self) -> None:
        with self.assertRaisesRegex(ContractError, "timezone-aware"):
            self.point(observed_at=datetime(2026, 8, 10, 12, 30))

    def test_catalog_rejects_unknown_and_missing_fields(self) -> None:
        point = self.point(fields={"questions_total": 100, "surprise": 1})
        with self.assertRaisesRegex(ContractError, "unknown fields: surprise; missing fields"):
            self.catalog.validate(point)

    def test_optional_fields_may_be_absent(self) -> None:
        catalog = MetricCatalog(
            [
                MeasurementSpec(
                    name="optional",
                    fields={
                        "required": FieldSpec(FieldKind.GAUGE, "count", int),
                        "optional": FieldSpec(
                            FieldKind.STATE, "text", str, series=False, required=False
                        ),
                    },
                )
            ]
        )
        point = self.point(measurement="optional", fields={"required": 1})
        self.assertIs(catalog.validate(point), point)

    def test_catalog_rejects_wrong_value_type(self) -> None:
        point = self.point(fields={"questions_total": 100.5, "threads_running": 3})
        with self.assertRaisesRegex(ContractError, "expects int"):
            self.catalog.validate(point)

    def test_series_key_is_independent_of_tag_order(self) -> None:
        fields = {"operations_total": 10, "wait_ms_total": 4.5}
        first = self.point(
            measurement="mysql.file_io",
            fields=fields,
            tags={"event": "innodb_data_file", "mode": "read"},
        )
        second = self.point(
            measurement="mysql.file_io",
            fields=fields,
            tags={"mode": "read", "event": "innodb_data_file"},
        )
        self.assertEqual(first.series_key, second.series_key)

    def test_packed_row_preserves_measurement_tags_and_fields(self) -> None:
        point = self.point(
            measurement="mysql.file_io",
            fields={"operations_total": 10, "wait_ms_total": 4.5},
            tags={"event": "innodb_data_file", "mode": "read"},
        )
        self.catalog.validate(point)
        row = point.packed_row()
        self.assertEqual(row["contract_version"], 2)
        self.assertEqual(row["target_id"], "orders-db")
        self.assertEqual(row["measurement"], "mysql.file_io")
        self.assertEqual(row["tags"]["mode"], "read")
        self.assertEqual(row["fields"]["wait_ms_total"], 4.5)

    def test_series_rows_include_semantics_and_units(self) -> None:
        point = self.point(
            measurement="mysql.file_io",
            fields={"operations_total": 10, "wait_ms_total": 4.5},
            tags={"event": "innodb_data_file", "mode": "read"},
        )
        rows = self.catalog.series_rows(point)
        self.assertEqual(
            {(row["field"], row["kind"], row["unit"]) for row in rows},
            {
                ("operations_total", "counter", "operations"),
                ("wait_ms_total", "counter", "ms"),
            },
        )

    def test_legacy_adapter_keeps_existing_metric_names(self) -> None:
        rows = legacy_metric_rows(
            self.point(),
            self.catalog,
            source="global_status",
            field_names={
                "questions_total": "Questions",
                "threads_running": "Threads_running",
            },
        )
        self.assertEqual(
            [row["metric"] for row in rows], ["Questions", "Threads_running"]
        )

    def test_legacy_adapter_rejects_dimensional_points(self) -> None:
        point = self.point(
            measurement="mysql.file_io",
            fields={"operations_total": 10, "wait_ms_total": 4.5},
            tags={"event": "innodb_data_file", "mode": "read"},
        )
        with self.assertRaisesRegex(ContractError, "cannot be represented"):
            legacy_metric_rows(point, self.catalog, source="file_io")


if __name__ == "__main__":
    unittest.main()
