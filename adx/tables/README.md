# adx/tables/ — table DDL, mappings and materialized views

KQL control commands that define the schema of the unified store. Committed here and applied via
CI, never typed ad hoc into the Kusto web UI.

## Expected contents

| File | Purpose |
|---|---|
| `MysqlMetrics.kql` | Numeric samples from `SHOW GLOBAL STATUS` / `performance_schema` |
| `MysqlEvents.kql` | Text events from `performance_schema.error_log` |
| `BenchmarkRuns.kql` | One row per benchmark run: tier, storage config, start/end |
| `mappings.kql` | JSON ingestion mappings for the JSONL the collector emits |
| `materialized-views.kql` | 1-minute and 5-minute rollups |

## Core tables

```kusto
.create table MysqlMetrics (
    Timestamp: datetime,      // UTC — Kusto datetime is always UTC
    RunId:     string,        // from the RUN_ID env var, on every row
    Host:      string,
    Tier:      string,        // premium-ssd-v1 | premium-ssd-v2
    Source:    string,        // global_status | ps_file_io | ps_digest
    Metric:    string,
    Value:     real
)

.create table MysqlEvents (
    Timestamp: datetime,      // UTC
    RunId:     string,
    Host:      string,
    Tier:      string,
    Source:    string,        // error_log | stmt_digest
    Level:     string,        // System | Warning | Error
    ErrorCode: string,
    Subsystem: string,
    Message:   string
)
```

Keeping metrics and events in separate tables is deliberate: their schemas, retention, and query
patterns differ, and mixing them forces every numeric query to filter out text rows.

## Ingestion mappings

The collector emits **JSON Lines**, so both ingestion paths use a `multijson` mapping:

```kusto
.create table MysqlMetrics ingestion json mapping "MysqlMetricsMapping"
'['
'  {"column":"Timestamp","Properties":{"Path":"$.ts"}},'
'  {"column":"RunId",    "Properties":{"Path":"$.run_id"}},'
'  {"column":"Host",     "Properties":{"Path":"$.host"}},'
'  {"column":"Tier",     "Properties":{"Path":"$.tier"}},'
'  {"column":"Source",   "Properties":{"Path":"$.source"}},'
'  {"column":"Metric",   "Properties":{"Path":"$.metric"}},'
'  {"column":"Value",    "Properties":{"Path":"$.value"}}'
']'
```

The same mapping serves the streaming (hot) and queued (cold) paths, so a JSONL file replayed later
produces rows identical to the ones ingested live.

## Materialized views for rollups

Long-range dashboards must not scan raw data. A materialized view keeps 1-minute rollups current
without a scheduled job:

```kusto
.create materialized-view with (backfill=true) MysqlMetrics1m on table MysqlMetrics
{
    MysqlMetrics
    | summarize Avg = avg(Value), Max = max(Value), Min = min(Value), Samples = count()
      by bin(Timestamp, 1m), RunId, Host, Tier, Metric
}
```

Raw `MysqlMetrics` is then free to expire after 30 days while trends survive for a year — see
[`../policies/`](../policies/).

## Counters vs gauges

`SHOW GLOBAL STATUS` values are mostly **cumulative counters**. Store the raw counter and derive
rates at query time, so a collector restart or a counter reset stays visible in the data:

```kusto
MysqlMetrics
| where Metric == "Innodb_data_reads" and RunId == "<run>"
| order by Timestamp asc
| extend Delta = Value - prev(Value)
| where Delta >= 0        // discard the reset boundary rather than plotting a negative spike
```

## Conventions

- **UTC only.** Never ingest naive or local timestamps; Kusto will treat them as UTC and silently
  shift every chart.
- **`RunId` on every row**, including production rows (use a stable sentinel such as `prod` when no
  benchmark is running) so no query needs a special case for missing values.
- MySQL 8.4 only: metric names follow 8.4 (`innodb_redo_log_capacity`, not `innodb_log_file_size`).
- Table and mapping names are stable — [`../../grafana/dashboards/`](../../grafana/dashboards/)
  references them directly.
- These files contain **no credentials and no real hostnames**.
