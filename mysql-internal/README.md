# mysql-internal/ — Layer 2: in-server telemetry

Telemetry collected by connecting **directly to MySQL 8.4** on Azure Database for MySQL Flexible
Server, independent of Azure Monitor.

## What lives here

| Directory | Purpose |
|---|---|
| [`collector/`](collector/) | Python 3.11+ collector that polls the server on an interval |
| [`sql/`](sql/) | `SHOW GLOBAL STATUS` and `performance_schema` query definitions |

Collected rows land in [`../adx/`](../adx/), the unified store that
[`../grafana/`](../grafana/) reads.

## Why this layer is primary during benchmarks

**Premium SSD v2 servers are in preview**, so Azure platform metrics and diagnostic logs may be
incomplete for them. Layer 2 talks to the engine directly, so it produces a consistent dataset for
both Premium SSD v1 and v2. When Layer 1 and Layer 2 disagree during a benchmark run, Layer 2 wins
and the discrepancy is recorded.

It is also the **only** source of MySQL error-log data: Flexible Server's diagnostic settings expose
just `MySQL Audit Logs` and `MySQL Slow Logs`, with no error-log category. And it is the **real-time
path** — seconds of latency versus the 2–5 minutes of Azure Monitor platform alerts.

## Data flow

```mermaid
flowchart LR
    ENV["Env vars<br/>MYSQL_* / ADX_* / RUN_ID"] --> COL
    SQL["sql/ query definitions"] --> COL["collector/ (TLS-only)<br/>prod 10s / bench 1-5s"]
    SRV["MySQL 8.4 Flexible Server"] --> COL

    COL ==> |"hot path — streaming (~seconds)"| ADX["../adx/ — unified store"]
    COL --> |"cold path — JSONL, queued"| ADX

    ADX --> GF["../grafana/ (final view)"]
    ADX --> BI["../benchmark-integration/"]
```

## MySQL 8.4 requirements

- `caching_sha2_password` is the default auth plugin; `mysql_native_password` is **disabled by
  default**. The driver must support `caching_sha2_password`.
- `innodb_redo_log_capacity` replaces `innodb_log_file_size` — query and report the new variable.
- Azure enforces `require_secure_transport=ON`, so **the connection must use SSL/TLS**. A
  non-TLS connection will be rejected by the server, and this repo never disables TLS.

## Configuration

Nothing is hardcoded. All connection info comes from environment variables:

| Variable | Description |
|---|---|
| `MYSQL_HOST` | Flexible Server FQDN, e.g. `<name>.mysql.database.azure.com` |
| `MYSQL_USER` | MySQL user with `SELECT` on `performance_schema` and `PROCESS` |
| `MYSQL_PASSWORD` | Password (never logged, never committed) |
| `MYSQL_DB` | Default database/schema |
| `MYSQL_TIER` | `premium-ssd-v1` / `premium-ssd-v2`, stamped on every row |
| `RUN_ID` | Tagged onto every emitted row; use a sentinel such as `prod` outside benchmarks |
| `ADX_INGEST_URI` | ADX ingestion endpoint (ADX sink only) |
| `ADX_DATABASE` | Target ADX database (ADX sink only) |

The ADX sink authenticates with a **managed identity**, so `MYSQL_PASSWORD` is the only credential.

## Conventions

- **Python 3.11+**, core dependencies limited to `mysql-connector-python` (or `PyMySQL`). The ADX
  sink is the single sanctioned extra, isolated in `requirements-adx.txt`. **No ORM.**
- **All timestamps are UTC ISO-8601.**
- **Every row carries `RUN_ID`**, so collector output joins with benchmark results on the time axis.
- Emit `collector_heartbeat` every cycle — a dead collector produces a flatline that otherwise reads
  as healthy.

## Required grants

```sql
-- Read-only monitoring user (8.4: caching_sha2_password is the default plugin)
CREATE USER 'monitor'@'%' IDENTIFIED BY '<supplied-at-runtime>';
GRANT PROCESS, REPLICATION CLIENT ON *.* TO 'monitor'@'%';
GRANT SELECT ON performance_schema.* TO 'monitor'@'%';
```

Create this user out-of-band; never commit a real password.
