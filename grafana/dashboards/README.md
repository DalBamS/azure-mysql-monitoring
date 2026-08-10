# grafana/dashboards/ — dashboard JSON models

Committed Grafana dashboard definitions. These are the **final monitoring view** for Azure Database
for MySQL Flexible Server (MySQL 8.4).

## Expected contents

| File | Purpose |
|---|---|
| `benchmark-ssd-v1-vs-v2.json` | Premium SSD v1 vs v2 comparison, driven by `$run_id` |
| `production-overview.json` | Ongoing health view for gaming customer workloads, 10s refresh |
| `collector-health.json` | Heartbeat, ingestion lag, sink failures |
| `storage-io.json` | IOPS, throughput, read/write latency, redo-log pressure |
| `connections-and-threads.json` | Connections, aborted connects, thread activity |
| `query-performance.json` | `performance_schema` statement digests and slow queries |
| `error-log.json` | `performance_schema.error_log` events — ADX only |

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

- **UTC everywhere.** Kusto `datetime` is always UTC; do not pin a dashboard to a local timezone,
  since a shifted axis silently invalidates a v1 vs v2 comparison.
- **ADX panels are authoritative during benchmarks**; Azure Monitor panels are supplementary, since
  Premium SSD v2 is in preview and its platform telemetry may have gaps. Annotate mixed panels so a
  gap is not misread as a healthy flat line.
- **Match the table to the time range**: raw `MysqlMetrics` for live/short ranges, the
  `MysqlMetrics1m` rollup for long ranges. Querying raw over a year is slow and expensive.
- Production dashboards refresh at **10s** to stay inside the ~25–45s detection budget.
- MySQL 8.4 only: redo-log panels use `innodb_redo_log_capacity`, not the removed
  `innodb_log_file_size`.
- Counter metrics from `SHOW GLOBAL STATUS` are cumulative — apply a rate/delta transform rather
  than plotting the raw value.

## Query shape

Panels backed by the ADX data source query the collector's ingested rows with KQL. Short/live
ranges read the raw table:

```kusto
MysqlMetrics
| where $__timeFilter(Timestamp)
| where RunId == '$run_id' and Metric in ($metric)
| order by Timestamp asc
| extend Delta = Value - prev(Value)     // counters are cumulative
| where Delta >= 0
| project Timestamp, Metric, Delta
```

Long ranges must read the rollup view instead, so a year-long panel does not scan raw data:

```kusto
MysqlMetrics1m
| where $__timeFilter(Timestamp)
| where Metric == 'Innodb_data_reads'
| project Timestamp, Avg, Max
```

Error-log panels read the events table — this data has no Azure Monitor equivalent:

```kusto
MysqlEvents
| where $__timeFilter(Timestamp) and Source == 'error_log' and Level == 'Error'
| project Timestamp, Host, ErrorCode, Subsystem, Message
```

## Configuration

Dashboards hold no connection details; data sources supply them via managed identity and nothing is
hardcoded:

| Variable | Description |
|---|---|
| `ADX_CLUSTER_URI` | `https://<cluster>.<region>.kusto.windows.net` |
| `ADX_DATABASE` | Database holding `MysqlMetrics` / `MysqlEvents` |
| `RUN_ID` | Benchmark run identifier, surfaced as `$run_id` |
