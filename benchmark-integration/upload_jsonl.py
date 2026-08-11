#!/usr/bin/env python3
"""Bulk-upload a collector JSONL archive to both ADX v2 tables."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

COLLECTOR_DIR = Path(__file__).resolve().parents[1] / "mysql-internal" / "collector"
sys.path.insert(0, str(COLLECTOR_DIR))

from catalog import CATALOG  # noqa: E402
from runtime import PlanSinkConfig  # noqa: E402
from sinks.adx import (  # noqa: E402
    SERIES_MAPPING,
    SERIES_TABLE,
    TELEMETRY_MAPPING,
    TELEMETRY_TABLE,
    AdxSink,
)
from telemetry import TelemetryContext, TelemetryPoint  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Upload even if the local success marker exists.",
    )
    return parser.parse_args(argv)


def point_from_row(row: dict[str, Any]) -> TelemetryPoint:
    observed_at = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
    return TelemetryPoint(
        observed_at=observed_at,
        context=TelemetryContext(
            run_id=str(row["run_id"]),
            target_id=str(row["target_id"]),
            host=str(row["host"]),
            tier=str(row["tier"]),
            azure_resource_id=str(row.get("azure_resource_id", "")),
            collector_id=str(row.get("collector_id", "")),
        ),
        measurement=str(row["measurement"]),
        tags={str(key): str(value) for key, value in dict(row["tags"]).items()},
        fields=dict(row["fields"]),
    )


def load_rows(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    packed = []
    series = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                point = CATALOG.validate(point_from_row(row))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"{path}:{line_number}: invalid telemetry row: {exc}") from exc
            packed.append(row)
            series.extend(CATALOG.series_rows(point))
    if not packed:
        raise ValueError(f"{path} contains no telemetry rows")
    return packed, series


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    marker = args.input.with_suffix(args.input.suffix + ".uploaded")
    if marker.exists() and not args.force:
        print(f"Already uploaded: {args.input}")
        return 0

    cfg = PlanSinkConfig.from_env()
    cfg.require_adx()
    packed, series = load_rows(args.input)
    sink = AdxSink(cfg, streaming=True)
    try:
        sink.write_raw_rows(packed, TELEMETRY_TABLE, TELEMETRY_MAPPING)
        if sink.last_error:
            raise RuntimeError(sink.last_error)
        sink.write_raw_rows(series, SERIES_TABLE, SERIES_MAPPING)
        if sink.last_error:
            raise RuntimeError(sink.last_error)
    finally:
        sink.close()

    marker.write_text(
        json.dumps(
            {
                "input": str(args.input),
                "packed_rows": len(packed),
                "series_rows": len(series),
                "database": os.environ.get("ADX_DATABASE", ""),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Uploaded {len(packed)} telemetry rows and {len(series)} series rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
