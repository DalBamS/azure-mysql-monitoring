# grafana/dashboards/ — dashboard JSON models

Committed Grafana dashboard definitions. These are the **final monitoring view** for Azure Database
for MySQL Flexible Server (MySQL 8.4).

## Expected contents

| File | Purpose |
|---|---|
| `benchmark-ssd-v1-vs-v2.json` | Premium SSD v1 vs v2 comparison, driven by `$run_id` |
| `production-overview.json` | Ongoing health view for gaming customer workloads |
| `storage-io.json` | IOPS, throughput, read/write latency, redo-log pressure |
| `connections-and-threads.json` | Connections, aborted connects, thread activity |
| `query-performance.json` | `performance_schema` statement digests and slow queries |

## Rules

- Dashboards are **source-controlled here and provisioned**, never left as UI-only edits. Export
  the JSON from Grafana and commit it so every change is reviewable in a PR.
- **No credentials, no real hostnames, no customer identifiers** in dashboard JSON. Reference data
  sources by their provisioned UID from [`../datasources/`](../datasources/).
- Strip volatile export noise (`id`, `version`, `iteration`) before committing to keep diffs clean.

## Required template variables

| Variable | Purpose |
|---|---|
| `$run_id` | Selects one benchmark run; matches the `RUN_ID` env var tagged onto every metric row |
| `$tier` | `premium-ssd-v1` / `premium-ssd-v2`, for labelling comparisons |
| `$server` | Which Flexible Server instance to display |

Because both the benchmark output and the collector output carry the same `RUN_ID`, selecting
`$run_id` lines up load-generator results and engine-internal metrics on one time axis.

## Panel conventions

- **UTC ISO-8601 everywhere.** Do not pin a dashboard to a local timezone; a shifted axis silently
  invalidates a v1 vs v2 comparison.
- Layer 2 (MySQL data source) panels are **authoritative during benchmarks**; Layer 1
  (Azure Monitor) panels are supplementary, since Premium SSD v2 is in preview and its platform
  telemetry may have gaps. Annotate mixed panels so a gap is not misread as a healthy flat line.
- MySQL 8.4 only: redo-log panels use `innodb_redo_log_capacity`, not the removed
  `innodb_log_file_size`.
- Counter metrics from `SHOW GLOBAL STATUS` are cumulative — apply a rate/delta transform rather
  than plotting the raw value.

## Query shape

Panels backed by the MySQL data source read the collector's persisted metrics table, not
`SHOW GLOBAL STATUS` directly (a live `SHOW` returns a snapshot that cannot be graphed):

```sql
SELECT
  ts     AS time,   -- DATETIME(3) stored in UTC
  metric,
  value
FROM monitoring_metrics
WHERE run_id = '$run_id'
  AND metric IN ($metric)
  AND $__timeFilter(ts)
ORDER BY ts;
```

## Configuration

Dashboards hold no connection details; data sources supply them from environment variables and
nothing is hardcoded:

| Variable | Description |
|---|---|
| `MYSQL_HOST` | Flexible Server FQDN |
| `MYSQL_USER` | Read-only monitoring user |
| `MYSQL_PASSWORD` | Password (never logged, never committed) |
| `MYSQL_DB` | Database holding the metrics table |
| `RUN_ID` | Benchmark run identifier, surfaced as `$run_id` |
