from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

MODULE = (
    Path(__file__).parents[2]
    / "benchmark-integration"
    / "open_loop_workload.py"
)
SPEC = importlib.util.spec_from_file_location("open_loop_workload", MODULE)
assert SPEC and SPEC.loader
open_loop_workload = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = open_loop_workload
SPEC.loader.exec_module(open_loop_workload)


class OpenLoopWorkloadTests(unittest.TestCase):
    def test_operation_stream_is_deterministic_and_bounded(self) -> None:
        kwargs = {
            "seed": 42,
            "count": 100,
            "row_count": 25,
            "read_percent": 80,
            "started_at": 10.0,
            "rate": 20,
        }

        first = list(open_loop_workload.operation_stream(**kwargs))
        second = list(open_loop_workload.operation_stream(**kwargs))

        self.assertEqual(first, second)
        self.assertTrue(all(1 <= operation.row_id <= 25 for operation in first))
        self.assertEqual(first[-1].scheduled_at, 10.0 + 99 / 20)

    def test_operation_stream_honors_extreme_read_mix(self) -> None:
        common = {
            "seed": 7,
            "count": 20,
            "row_count": 10,
            "started_at": 0.0,
            "rate": 10,
        }

        reads = list(
            open_loop_workload.operation_stream(read_percent=100, **common)
        )
        writes = list(
            open_loop_workload.operation_stream(read_percent=0, **common)
        )

        self.assertTrue(all(operation.kind == "read" for operation in reads))
        self.assertTrue(all(operation.kind == "write" for operation in writes))

    def test_percentile_interpolates(self) -> None:
        self.assertEqual(open_loop_workload.percentile([], 0.95), 0.0)
        self.assertEqual(open_loop_workload.percentile([1.0], 0.95), 1.0)
        self.assertAlmostEqual(
            open_loop_workload.percentile([1.0, 2.0, 3.0, 4.0], 0.50),
            2.5,
        )

    def test_wait_for_buffer_pool_accepts_exact_size_with_empty_status(self) -> None:
        cursor = MagicMock()
        cursor.fetchone.side_effect = [(1073741824,), ("status", "")]
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        args = SimpleNamespace(
            expected_bytes=1073741824,
            timeout_seconds=1,
            poll_seconds=0,
        )

        with (
            patch.object(open_loop_workload, "connect", return_value=connection),
            patch.object(open_loop_workload.time, "monotonic", side_effect=[0, 0]),
        ):
            result = open_loop_workload.wait_for_buffer_pool(MagicMock(), args)

        self.assertEqual(result, 0)
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
