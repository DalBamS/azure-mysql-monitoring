# mysql-internal/collector/ — Python metrics collector

A small **Python 3.11+** program that connects to Azure Database for MySQL Flexible Server
(**MySQL 8.4**) over TLS, polls it on a fixed interval, and emits one tagged metric row per sample.

This is the **primary data source during benchmark runs**, because Premium SSD v2 servers are in
preview and Azure platform metrics may be incomplete for them.

## Expected contents

| File | Purpose |
|---|---|
| `collector.py` | Entry point: poll loop, interval, graceful shutdown, heartbeat |
| `connection.py` | TLS-enforced connection factory built from environment variables |
| `metrics.py` | Runs the queries in [`../sql/`](../sql/) and normalises results |
| `events.py` | Cursor-based incremental read of `performance_schema.error_log` |
| `sinks/jsonl.py` | Append-only JSON Lines writer (raw archive, cold-path source) |
| `sinks/adx.py` | ADX streaming (hot) and queued (cold) ingestion — **optional extra** |
| `requirements.txt` | Core: `mysql-connector-python` (or `PyMySQL`) — nothing else |
| `requirements-adx.txt` | Extra: `azure-kusto-ingest`, `azure-identity` |

## Configuration

**Never hardcode credentials.** All connection info comes from environment variables:

| Variable | Required | Description |
|---|---|---|
| `MYSQL_HOST` | yes | Flexible Server FQDN, e.g. `<name>.mysql.database.azure.com` |
| `MYSQL_USER` | yes | Monitoring user (`PROCESS`, `SELECT` on `performance_schema`) |
| `MYSQL_PASSWORD` | yes | Password — never logged, never committed |
| `MYSQL_DB` | yes | Default database/schema |
| `RUN_ID` | yes | Tagged onto **every** row; use a sentinel such as `prod` outside benchmarks |
| `ADX_INGEST_URI` | ADX sink | `https://ingest-<cluster>.<region>.kusto.windows.net` |
| `ADX_CLUSTER_URI` | ADX sink | `https://<cluster>.<region>.kusto.windows.net` |
| `ADX_DATABASE` | ADX sink | Target database |
| `MYSQL_TIER` | yes | `premium-ssd-v1` / `premium-ssd-v2`, stamped on every row |

The ADX sink authenticates with a **managed identity** — there is no ADX secret to configure.

```bash
export MYSQL_HOST="<server>.mysql.database.azure.com"
export MYSQL_USER="<user>"
export MYSQL_PASSWORD="<password>"
export MYSQL_DB="<database>"
export MYSQL_TIER="premium-ssd-v2"
export RUN_ID="ssdv2-2026-08-10-01"

# Benchmark: high resolution, file only — no extra dependencies needed
python collector.py --interval 5 --sink jsonl \
  --out ../../benchmark-integration/runs/$RUN_ID.jsonl

# Production: real-time hot path
export ADX_INGEST_URI="https://ingest-<cluster>.<region>.kusto.windows.net"
export ADX_DATABASE="<database>"
python collector.py --interval 10 --sink adx-streaming --sink jsonl
```

PowerShell:

```powershell
$env:MYSQL_HOST = "<server>.mysql.database.azure.com"
$env:MYSQL_USER = "<user>"
$env:MYSQL_PASSWORD = "<password>"
$env:MYSQL_DB     = "<database>"
$env:MYSQL_TIER   = "premium-ssd-v2"
$env:RUN_ID       = "ssdv2-2026-08-10-01"

python collector.py --interval 5 --sink jsonl --out "..\..\benchmark-integration\runs\$env:RUN_ID.jsonl"
```

## Rules

- **Python 3.11+.** The **core** collector's runtime dependencies are limited to
  `mysql-connector-python` (or `PyMySQL`); everything else must come from the standard library.
  **No ORM.**
- The **ADX sink is the one sanctioned exception**, isolated in `sinks/adx.py` and
  `requirements-adx.txt` (`azure-kusto-ingest`, `azure-identity`). The core must import it lazily so
  a benchmark run works with the core requirements alone.
- **TLS is mandatory.** Azure sets `require_secure_transport=ON`; never disable SSL, and never
  fall back to an unencrypted connection.
- MySQL 8.4 uses **`caching_sha2_password`** by default (`mysql_native_password` is disabled);
  the driver must support it.
- Read `innodb_redo_log_capacity`, not the deprecated `innodb_log_file_size` (still exposed on
  8.4, but ignored once capacity is set).
- **All timestamps are UTC ISO-8601** — emit timezone-aware values, never naive or local time.
- **Every row includes `RUN_ID`** plus the sampled server identity and tier, so v1 and v2 runs stay
  distinguishable.
- Output is append-only and machine-readable so a run can be replayed and re-analysed.
- Redact `MYSQL_PASSWORD` from any config dump or log line.
- A sink failure must **never** kill the poll loop — buffer, retry with backoff, and keep sampling.

## Output shape

One JSON Lines record per sample. This is the wire format for **both** sinks, so a file replayed
into ADX later produces rows identical to the ones ingested live:

```json
{"ts":"2026-08-10T00:53:04Z","run_id":"ssdv2-2026-08-10-01","host":"<server>","tier":"premium-ssd-v2","source":"global_status","metric":"Innodb_data_reads","value":184392}
```

Event rows use the same envelope with a text payload:

```json
{"ts":"2026-08-10T00:53:07.412Z","run_id":"ssdv2-2026-08-10-01","host":"<server>","tier":"premium-ssd-v2","source":"error_log","level":"Error","error_code":"MY-012345","subsystem":"InnoDB","message":"<text>"}
```

Field names map to the ADX columns via the ingestion mappings in
[`../../adx/tables/`](../../adx/tables/).

## Sinks

| Sink | Path | Latency | Used for |
|---|---|---|---|
| `jsonl` | local file per `RUN_ID` | n/a | Raw archive, benchmark artifact, cold-path source |
| `adx-streaming` | ADX streaming ingestion | seconds | **Production real-time monitoring** |
| `adx-queued` | ADX queued ingestion | batching window | Bulk replay, backfill, benchmark upload |

Run `jsonl` alongside an ADX sink in production: the file is the local buffer that lets you replay
a window if ingestion was rejected.

Queued ingestion batches for up to **5 minutes** by default, so it cannot carry real-time
monitoring — that is what `adx-streaming` is for. Streaming has a **~4 MB per-request limit**, so
large backfills must go through `adx-queued`.

## Heartbeat — required

A self-built collector adds a failure mode the platform does not have: **when the collector dies,
dashboards flatline and look healthy.** Silence must be indistinguishable from an outage only to a
naive reader, never to the alerting.

Emit `collector_heartbeat` on **every** cycle, before any query work that could fail:

```json
{"ts":"2026-08-10T00:53:04Z","run_id":"prod","host":"<server>","tier":"premium-ssd-v2","source":"collector","metric":"collector_heartbeat","value":1}
```

The alert that consumes it is defined in [`../../adx/policies/`](../../adx/policies/).

## Collecting events vs metrics

The two data shapes need different read strategies:

| Data | Source | Shape | Strategy |
|---|---|---|---|
| Metrics | `SHOW GLOBAL STATUS` | Cumulative counters | Sample each interval; store the raw counter and derive rates at query time |
| Error events | `performance_schema.error_log` | **Ring buffer** | Incremental read using the last `LOGGED` value as a cursor |
| Query stats | `events_statements_summary_by_digest` | Cumulative aggregate | Snapshot and diff against the previous snapshot |

`performance_schema.error_log` is a **ring buffer**: old entries are evicted as new ones arrive.
Poll it often enough that eviction never outruns collection, and persist the cursor so a collector
restart does not re-ingest or skip entries.

Azure Flexible Server exposes **no error-log category in diagnostic settings** — only
`MySQL Audit Logs` and `MySQL Slow Logs`. Reading `performance_schema.error_log` here is what closes
that gap, and it is a capability Layer 1 simply does not have.

## Volume control

The single biggest cost lever is **how many status variables you keep**. MySQL 8.4 exposes 400+;
retaining a curated ~80 instead of all of them cuts ingestion volume roughly fivefold. Define the
allow-list in [`../sql/`](../sql/) rather than filtering downstream, so the data is never paid for.

Suggested intervals:

| Context | Interval | Rationale |
|---|---|---|
| Benchmark | 1–5s | Short runs; resolution matters more than volume |
| Production | 10–15s | Keeps detection under a minute at sustainable cost |
