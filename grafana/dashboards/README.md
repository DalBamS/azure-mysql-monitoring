# grafana/dashboards/ — dashboard JSON models

Committed Grafana dashboard definitions. These are the **final monitoring view** for Azure Database
for MySQL Flexible Server (MySQL 8.4).

## Contents

| File | Status | Purpose |
|---|---|---|
| `production-overview.json` | **built** | Ongoing health view for gaming customer workloads |
| `benchmark-ssd-v1-vs-v2.json` | **built** | Premium SSD v1 vs v2 comparison, driven by `$baseline` / `$candidate` |
| `collector-health.json` | **built** | Heartbeat, sample arrival rate, cycle duration, ingestion lag |
| `check-dashboard-queries.py` | **built** | Runs every panel's KQL against the real cluster |
| `storage-io.json` | planned | IOPS, throughput, read/write latency, redo-log pressure |
| `connections-and-threads.json` | planned | Folded into `production-overview.json` for now |
| `query-performance.json` | planned | `performance_schema` statement digests and slow queries |
| `error-log.json` | planned | Folded into `production-overview.json` for now |

Deploy them with [`../provisioning/deploy.ps1`](../provisioning/deploy.ps1).

## Verifying a dashboard actually works

Importing a dashboard only proves its JSON parsed. A panel whose KQL names a column that does not
exist imports perfectly and then draws an empty graph — which looks exactly like a healthy idle
server. So panels are verified by executing their queries:

```bash
. ./testing/scripts/load-env.ps1
python grafana/dashboards/check-dashboard-queries.py
```

The script substitutes the Grafana macros (`$__timeFilter`) and template variables that ADX would
otherwise reject, then runs each query and reports its row count. A panel returning zero rows is
reported separately from one that failed, because an empty error-log panel is correct on a healthy
server while an empty throughput panel is not.

Last verified against the live test environment: **24 panels returning data, 1 valid but empty
(error log), 0 failed.**

## Rules

- Dashboards are **source-controlled here and provisioned**, never left as UI-only edits. Export
  the JSON from Grafana and commit it so every change is reviewable in a PR.
- **No credentials, no real hostnames, no customer identifiers** in dashboard JSON. Reference data
  sources by their provisioned UID from [`../datasources/`](../datasources/).
- Strip volatile export noise (`id`, `version`, `iteration`) before committing to keep diffs clean.
- A new panel needs a passing run of `check-dashboard-queries.py` before it is committed.

## Template variables

| Variable | Dashboard | Purpose |
|---|---|---|
| `$run_id` | overview, collector health | Selects one run; matches the `RUN_ID` env var tagged onto every metric row |
| `$host` | overview | Which Flexible Server instance to display |
| `$baseline` / `$candidate` | benchmark | The two runs being compared |

Because both the benchmark output and the collector output carry the same `RUN_ID`, selecting
`$run_id` lines up load-generator results and engine-internal metrics on one time axis.

The benchmark dashboard selects **two runs** rather than filtering by time, because the v1 and v2
runs happen at different wall-clock times — a shared time axis would show one run or the other,
never both. Its rate panels plot *elapsed seconds since each run started* so the two overlay.

## Panel conventions

- **UTC everywhere.** Kusto `datetime` is always UTC; do not pin a dashboard to a local timezone,
  since a shifted axis silently invalidates a v1 vs v2 comparison.
- **ADX panels are authoritative during benchmarks**; Azure Monitor panels are supplementary, since
  Premium SSD v2 is in preview and its platform telemetry may have gaps. Annotate mixed panels so a
  gap is not misread as a healthy flat line.
- **Match the table to the time range**: raw `MysqlMetrics` for live/short ranges, the
  `MysqlMetrics1m` rollup for longer ranges inside the 90-day lifecycle.
- Production dashboards refresh at **30s**; the streaming path was measured at 6.5s end to end on
  the test environment, so a faster refresh mostly re-queries the same rows.
- MySQL 8.4 only: redo-log panels use `innodb_redo_log_capacity`. `innodb_log_file_size` is
  deprecated but still *readable* — it reports a stale value that governs nothing once
  `innodb_redo_log_capacity` is set, so never plot it.
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

Long ranges must read the rollup view instead, so a 90-day panel does not scan every raw point:

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
