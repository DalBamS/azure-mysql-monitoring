# benchmark-integration/

Glue between **benchmark runs** and **monitoring data**. This is where a Premium SSD v1 vs
Premium SSD v2 comparison is actually produced.

## Purpose

A benchmark tool (sysbench, an application-level load generator, etc.) produces throughput and
latency results. Separately, the collector in [`../mysql-internal/collector/`](../mysql-internal/collector/)
produces engine-internal metrics, and [`../azure-native/`](../azure-native/) produces platform
telemetry. This directory **joins them on the time axis using `RUN_ID`** and renders the comparison.

```mermaid
flowchart LR
    BM["Benchmark results<br/>(RUN_ID + UTC timestamps)"] --> J["Join on RUN_ID<br/>+ time axis"]
    COL["mysql-internal collector output<br/>(primary)"] --> J
    LAW["azure-native / Log Analytics<br/>(supplementary)"] --> J
    J --> R["v1 vs v2 comparison report"]
```

## Expected contents

| Path | Purpose |
|---|---|
| `runs/` | Per-run raw output, one file per `RUN_ID` (git-ignored) |
| `join.py` | Aligns benchmark rows and collector rows on `run_id` + timestamp |
| `compare.py` | Produces the Premium SSD v1 vs v2 comparison |
| `report/` | Generated summaries and charts |

## Why the collector is the primary source

**Premium SSD v2 servers are in preview**, so Azure Monitor metrics and diagnostic logs may be
incomplete for them. Comparisons are therefore built from `mysql-internal/` collector data, with
`azure-native/` used as supplementary context. Any disagreement between the two layers is recorded
in the report rather than silently resolved.

## Run correlation contract

Both sides of the join must satisfy:

- **`RUN_ID`** — read from the `RUN_ID` environment variable and present on **every** row.
  A single benchmark execution uses one `RUN_ID` for the load generator and the collector.
- **UTC ISO-8601 timestamps** — timezone-aware, never local time. This is the join key alongside
  `RUN_ID`.
- A storage-tier label (`premium-ssd-v1` / `premium-ssd-v2`) so runs are attributable.

Suggested `RUN_ID` convention: `<tier>-<yyyy-mm-dd>-<seq>`, e.g. `ssdv2-2026-08-10-01`.

## Configuration

Nothing is hardcoded. All connection info comes from environment variables:

| Variable | Description |
|---|---|
| `MYSQL_HOST` | Flexible Server FQDN |
| `MYSQL_USER` | MySQL user |
| `MYSQL_PASSWORD` | Password (never logged, never committed) |
| `MYSQL_DB` | Default database/schema |
| `RUN_ID` | Identifier shared by the benchmark and the collector for one run |

```bash
export RUN_ID="ssdv2-2026-08-10-01"

# 1. start the collector (separate shell), then run the benchmark with the same RUN_ID
# 2. join and compare
python join.py --run-id "$RUN_ID"
python compare.py --baseline ssdv1-2026-08-10-01 --candidate "$RUN_ID"
```

## Conventions

- **Python 3.11+**, dependencies limited to `mysql-connector-python` (or `PyMySQL`). **No ORM.**
- All timestamps stay **UTC ISO-8601** end to end; no timezone conversion during the join.
- Raw run data under `runs/` is not committed — it can be large and may contain customer-shaped
  workload details.
