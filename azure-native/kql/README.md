# azure-native/kql/ — Log Analytics queries

Reusable **KQL** queries run against the Log Analytics workspace that receives diagnostic settings
and platform metrics from Azure Database for MySQL Flexible Server (MySQL 8.4).

## Expected contents

| File | Purpose |
|---|---|
| `slow-queries.kql` | Slow query log analysis (`AzureDiagnostics`, `MySqlSlowLogs`) |
| `audit-events.kql` | Audit log analysis (`AzureDiagnostics`, `MySqlAuditLogs`) |
| `connections.kql` | Connection counts, aborted connects, TLS handshake failures |
| `storage-latency.kql` | Read/write latency and IOPS, used for SSD v1 vs v2 comparison |
| `cpu-memory.kql` | Resource utilisation (`AzureMetrics`) |
| `platform-events.kql` | Failover, restart and maintenance events (`AzureActivity`) |

One query per file, with a header comment describing its inputs, expected columns, and which
workbook or alert consumes it.

## Available tables

Only three tables carry this service's telemetry:

| Table | Contents |
|---|---|
| `AzureMetrics` | Platform metrics (CPU, memory, storage, IOPS, connections) |
| `AzureDiagnostics` | Resource logs — **`MySqlSlowLogs` and `MySqlAuditLogs` only** |
| `AzureActivity` | Control-plane operations on the server resource |

There is **no error-log category** for Flexible Server. Do not write a query that expects one;
MySQL error-log content comes from `performance_schema.error_log` in Layer 2 and is queried in
[`../../adx/`](../../adx/) instead.

## Conventions

- Every query header documents required parameters (time range, server name, `RUN_ID`).
- Project timestamps as **UTC ISO-8601**; do not convert to local time inside a query.
- For benchmark queries, project a `RunId` column so results can be joined with
  `mysql-internal/` collector output on the time axis.
- MySQL 8.4 only. Where a query touches redo-log configuration, use `innodb_redo_log_capacity`.
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
