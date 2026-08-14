# azure-native/kql/ — Log Analytics queries

Reusable **KQL** queries run against the Log Analytics workspace that receives diagnostic settings
and platform metrics from Azure Database for MySQL Flexible Server (MySQL 8.4).

## Contents

| File | Purpose |
|---|---|
| `slow-queries.kql` | Slow query log analysis (`AzureDiagnostics`, `MySqlSlowLogs`) |
| `audit-events.kql` | Audit log analysis (`AzureDiagnostics`, `MySqlAuditLogs`) |
| `connections.kql` | Connection counts, aborted connects, TLS handshake failures |
| `storage-latency.kql` | Storage utilisation and IO pressure, used for SSD v1 vs v2 comparison |
| `cpu-memory.kql` | Resource utilisation (`AzureMetrics`) |
| `platform-events.kql` | Failover, restart and maintenance events (`AzureActivity`) |
| `check-kql.py` | Runs every file above against a real workspace |

Each file holds **one executable query**, then commented variants below it. The variants are
documentation — `check-kql.py` executes only the active query and reports the rest as skipped.

## Verifying these queries

A query naming a column Azure does not emit fails quietly: the tile errors where nobody looks, or
returns nothing and reads as "quiet". So they are executed rather than reviewed:

```bash
. ./testing/scripts/load-env.ps1
python azure-native/kql/check-kql.py
```

Last verified against the live test workspace: **6 files, 0 failed.** `platform-events.kql`
returned 0 rows, which is correct — no control-plane operation had occurred in the window.

> A zero-row result is not proof of health. `slow-queries.kql` returns nothing both when no query
> was slow and when `slow_query_log` is `OFF`. Check the server parameter before concluding the
> former.

## Available tables

Only three tables carry this service's telemetry:

| Table | Contents |
|---|---|
| `AzureMetrics` | Platform metrics (CPU, memory, storage, IOPS, connections) |
| `AzureDiagnostics` | Resource logs — **`MySqlSlowLogs` and `MySqlAuditLogs` only** |
| `AzureActivity` | Control-plane operations on the server resource |

Confirmed on a live server: `AzureDiagnostics` contained exactly those two categories and nothing
else. There is **no error-log category** for Flexible Server. Do not write a query that expects one;
MySQL error-log content comes from `performance_schema.error_log` in Layer 2 and is queried in
[`../../adx/`](../../adx/) instead.

## Metric names are case-sensitive and mixed-convention

Flexible Server emits platform metrics in `snake_case` (`cpu_percent`, `memory_percent`,
`storage_percent`, `aborted_connections`, `io_consumption_percent`) alongside engine counters that
keep MySQL's own capitalisation (`Queries`, `Slow_queries`, `Threads_running`,
`Innodb_buffer_pool_reads`). Mixing the two up returns an **empty result, not an error** — the
failure mode that looks like healthy silence.

## Conventions

- Every query header documents required parameters (time range, server name, `RUN_ID`).
- Project timestamps as **UTC ISO-8601**; do not convert to local time inside a query.
- For benchmark queries, project a `RunId` column so results can be joined with
  `mysql-internal/` collector output on the time axis.
- MySQL 8.4 only. Where a query touches redo-log configuration, use `innodb_redo_log_capacity`.
  `innodb_log_file_size` is deprecated but still readable, returning a stale value that governs
  nothing — so it must never be reported as if it were in effect.
- Queries must not contain credentials, real hostnames, or customer identifiers — parameterise them.

## Configuration

These queries run in Log Analytics and require no database credentials. Repo-wide tooling that
does connect to MySQL reads only environment variables — nothing is hardcoded:

| Variable | Description |
|---|---|
| `MYSQL_HOST` | Flexible Server FQDN |
| `MYSQL_USER` | MySQL user |
| `MYSQL_PASSWORD` | Password (never logged, never committed) |
| `MYSQL_DB` | Default database/schema |
| `RUN_ID` | Benchmark run identifier, projected as `RunId` |

## Premium SSD v2 caveat

Premium SSD v2 is in **preview**; some metric or log categories may be absent for v2-backed
servers, producing empty results. Treat gaps as missing telemetry, not as healthy behaviour, and
confirm against Layer 2 collector data.

## KQL is shared with ADX

The query language here is the same one used in [`../../adx/`](../../adx/) and in
[`../../grafana/dashboards/`](../../grafana/dashboards/). Table names and schemas differ, but
operators, functions and time-series idioms carry over — reuse patterns rather than reinventing
them per layer.
