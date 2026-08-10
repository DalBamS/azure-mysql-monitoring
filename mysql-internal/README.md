# mysql-internal/ — Layer 2: general-purpose in-server telemetry

Telemetry collected by a monitoring VM that connects **directly to multiple MySQL 8.4 Targets** on
Azure Database for MySQL Flexible Server, independent of Azure Monitor.

## What lives here

| Directory | Purpose |
|---|---|
| [`collector/`](collector/) | Python 3.11+ multi-target collector, telemetry contract and YAML plan |
| [`sql/`](sql/) | `performance_schema` / `information_schema` Collection Group queries |

Collected rows land in [`../adx/`](../adx/), the unified store that
[`../grafana/`](../grafana/) reads.

## Why production and benchmark use the same layer

**Premium SSD v2 servers are in preview**, so Azure platform metrics and diagnostic logs may be
incomplete for them. Layer 2 talks to the engine directly, so it produces a consistent dataset for
both Premium SSD v1 and v2. The benchmark Profile changes cadence and `RUN_ID`, not metric meaning.
When Layer 1 and Layer 2 disagree during a benchmark run, Layer 2 wins and the discrepancy is
recorded.

It is also the **only** source of MySQL error-log data: Flexible Server's diagnostic settings expose
just `MySQL Audit Logs` and `MySQL Slow Logs`, with no error-log category. And it is the **real-time
path** — seconds of latency versus the 2–5 minutes of Azure Monitor platform alerts.

## Data flow

```mermaid
flowchart LR
    PLAN["YAML Targets + Profiles<br/>env / Key Vault references"] --> COL
    SQL["Collection Group queries"] --> COL["collector VM (TLS-only)<br/>multi-target scheduler"]
    SRV["Multiple MySQL 8.4 Targets"] --> COL

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

Production uses a YAML Collection Plan with references to environment variables or Azure Key Vault
secrets; see [`collector/monitoring.example.yaml`](collector/monitoring.example.yaml). Literal
credentials in YAML are invalid.

The existing single-target compatibility runtime reads:

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

- **Python 3.11+**, with `mysql-connector-python` and PyYAML in the core runtime. Azure SDKs for the
  ADX sink remain isolated in `requirements-adx.txt`. **No ORM.**
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
