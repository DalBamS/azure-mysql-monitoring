# mysql-internal/sql/ — collector query definitions

Plain SQL executed by the collector in [`../collector/`](../collector/) against
**MySQL 8.4** on Azure Database for MySQL Flexible Server.

## Expected contents

| File | Purpose |
|---|---|
| `metrics_allowlist.sql` | The curated ~80 status variables actually retained |
| `global_status.sql` | `SHOW GLOBAL STATUS` — counters (InnoDB I/O, threads, handlers, bytes) |
| `global_variables.sql` | Configuration snapshot, incl. `innodb_redo_log_capacity` |
| `error_log.sql` | `performance_schema.error_log` — incremental read by `LOGGED` cursor |
| `ps_file_summary_io.sql` | `performance_schema.file_summary_by_event_name` — file I/O latency |
| `ps_table_io_waits.sql` | `performance_schema.table_io_waits_summary_by_table` |
| `ps_statement_digest.sql` | `performance_schema.events_statements_summary_by_digest` |
| `ps_innodb_buffer_pool.sql` | Buffer pool hit rate and page churn |
| `replication_status.sql` | Replica lag and applier state |

One query per file, with a header comment stating what it measures, its output columns, and the
sampling cadence it is designed for.

## Conventions

- **MySQL 8.4 only.** Do not use syntax, variables, or `performance_schema` tables that were
  removed or renamed in 8.4.
  - Use `innodb_redo_log_capacity`; `innodb_log_file_size` is deprecated and, although still
    returned by `SHOW VARIABLES`, no longer governs redo-log sizing.
  - Assume `caching_sha2_password` is the default auth plugin.
- **No ORM** — these are raw SQL statements executed by the collector.
- Queries are **read-only**. No `SET GLOBAL`, no DDL, no writes.
- Keep queries cheap; they run on every polling interval against servers under benchmark load.
- Use parameter placeholders rather than string interpolation for any variable input.
- The collector adds the UTC ISO-8601 timestamp, `RUN_ID` and tier tags — queries return raw
  metric name/value pairs and do not embed timing or run identity themselves.
- **Filter here, not downstream.** MySQL 8.4 exposes 400+ status variables; the allow-list in this
  directory is what keeps ingestion volume (and ADX cost) roughly fivefold lower.

## `performance_schema.error_log` is a ring buffer

This table is the only route to MySQL error-log data on Flexible Server — Azure's diagnostic
settings offer no error-log category. It behaves differently from the summary tables:

- Entries are **evicted** as new ones arrive, so a slow poll loses data permanently.
- Read incrementally using the last seen `LOGGED` value as a cursor, and persist that cursor so a
  collector restart neither skips nor duplicates entries.
- `LOGGED` is a `TIMESTAMP(6)`; compare with microsecond precision or risk re-reading a boundary row.

## Configuration

These files contain no connection details. The collector supplies them from environment variables,
and nothing is hardcoded:

| Variable | Description |
|---|---|
| `MYSQL_HOST` | Flexible Server FQDN |
| `MYSQL_USER` | Monitoring user with `PROCESS` and `SELECT` on `performance_schema` |
| `MYSQL_PASSWORD` | Password (never logged, never committed) |
| `MYSQL_DB` | Default database/schema |
| `RUN_ID` | Tagged onto every metric row by the collector |

Connections are made over TLS, since Azure enforces `require_secure_transport=ON`.
