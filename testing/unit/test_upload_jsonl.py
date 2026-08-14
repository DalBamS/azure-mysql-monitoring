from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE = (
    Path(__file__).parents[2] / "benchmark-integration" / "upload_jsonl.py"
)
SPEC = importlib.util.spec_from_file_location("upload_jsonl", MODULE)
assert SPEC and SPEC.loader
upload_jsonl = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = upload_jsonl
SPEC.loader.exec_module(upload_jsonl)


class UploadJsonlTests(unittest.TestCase):
    def test_reconstructs_a_catalog_valid_point(self) -> None:
        row = {
            "ts": "2026-08-11T10:00:00.000000Z",
            "run_id": "v2-r1",
            "target_id": "orders-v2",
            "host": "orders-v2.mysql.database.azure.com",
            "tier": "premium-ssd-v2",
            "azure_resource_id": "/subscriptions/test/servers/orders-v2",
            "collector_id": "collector-1",
            "measurement": "mysql.global_status",
            "tags": {},
            "fields": {"Queries": 100},
        }

        point = upload_jsonl.point_from_row(row)
        validated = upload_jsonl.CATALOG.validate(point)
        series = upload_jsonl.CATALOG.series_rows(validated)

        self.assertEqual(validated.context.target_id, "orders-v2")
        self.assertEqual(series[0]["field"], "Queries")
        self.assertEqual(series[0]["value"], 100.0)


if __name__ == "__main__":
    unittest.main()
