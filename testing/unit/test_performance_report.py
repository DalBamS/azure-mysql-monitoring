from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPORT = Path(__file__).parents[2] / "benchmark-integration" / "performance_report.py"
SPEC = importlib.util.spec_from_file_location("performance_report", REPORT)
assert SPEC and SPEC.loader
performance_report = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = performance_report
SPEC.loader.exec_module(performance_report)


class PerformanceReportTests(unittest.TestCase):
    def test_direction_aware_metric_verdicts(self) -> None:
        qps = performance_report.compare_metric("QPS", 100, 110, 5)
        latency = performance_report.compare_metric("Read latency ms", 10, 8, 5)
        regression = performance_report.compare_metric("Query p99 ms", 10, 12, 5)
        errors = performance_report.compare_metric("Query errors/s", 2, 3, 5)
        throughput = performance_report.compare_metric(
            "Read throughput MiB/s", 100, 110, 5
        )
        zero_errors = performance_report.compare_metric("Query errors/s", 0, 0, 5)
        new_errors = performance_report.compare_metric("Query errors/s", 0, 1, 5)

        self.assertEqual(qps.verdict, "IMPROVEMENT")
        self.assertEqual(latency.verdict, "IMPROVEMENT")
        self.assertEqual(regression.verdict, "REGRESSION")
        self.assertEqual(errors.verdict, "REGRESSION")
        self.assertEqual(throughput.verdict, "IMPROVEMENT")
        self.assertEqual(zero_errors.verdict, "NO MATERIAL CHANGE")
        self.assertEqual(new_errors.verdict, "REGRESSION")

    def test_report_is_inconclusive_when_runs_are_not_comparable(self) -> None:
        baseline = performance_report.RunSelection("v1", "orders-v1")
        candidate = performance_report.RunSelection("v2", "orders-v2")
        inventory = [
            {
                "RunId": "v1",
                "TargetId": "orders-v1",
                "Host": "v1.mysql.database.azure.com",
                "Tier": "premium-ssd-v1",
                "Start": "2026-08-11T00:00:00Z",
                "DurationSeconds": 600,
                "Points": 100,
                "Measurements": 5,
            },
            {
                "RunId": "v2",
                "TargetId": "orders-v2",
                "Host": "v2.mysql.database.azure.com",
                "Tier": "premium-ssd-v2",
                "Start": "2026-08-11T00:00:00Z",
                "DurationSeconds": 300,
                "Points": 50,
                "Measurements": 5,
            },
        ]
        comparison = performance_report.compare_metric("QPS", 100, 120, 5)

        report = performance_report.render_report(
            baseline=baseline,
            candidate=candidate,
            start=datetime(2026, 8, 11, tzinfo=timezone.utc),
            end=datetime(2026, 8, 12, tzinfo=timezone.utc),
            inventory=inventory,
            comparisons=[comparison],
            sample_differences={"QPS": 50},
            max_duration_difference_pct=10,
            max_sample_difference_pct=20,
            material_change_pct=5,
            dashboard_url=None,
        )

        self.assertIn("**Overall result:** INCONCLUSIVE", report)
        self.assertIn("Run durations differ", report)
        self.assertIn("Sample counts differ", report)

    def test_kql_literals_escape_user_selected_ids(self) -> None:
        baseline = performance_report.RunSelection("v1'bad", "target")
        candidate = performance_report.RunSelection("v2", "target")
        query = performance_report.summary_query(
            baseline,
            candidate,
            datetime(2026, 8, 11, tzinfo=timezone.utc),
            datetime(2026, 8, 12, tzinfo=timezone.utc),
        )

        self.assertIn("'v1''bad'", query)


if __name__ == "__main__":
    unittest.main()
