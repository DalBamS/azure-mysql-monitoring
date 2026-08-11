# mysql-internal/collector/ — Python metrics collector

A **Python 3.11+** collector for Azure Database for MySQL Flexible Server (**MySQL 8.4**). The
production design runs on a monitoring VM, connects to multiple Targets over TLS, and executes a
validated YAML Collection Plan.

Production monitoring is the primary mode. Benchmark Profiles reuse the same measurements at a
shorter cadence and stamp a benchmark `RUN_ID`; Layer 2 remains authoritative for Premium SSD v2
because its Azure platform metrics may be incomplete while the tier is in preview.

## Expected contents

| File | Purpose |
|---|---|
| `collector.py` | Entry point for Collection Plan and single-target compatibility modes |
| `telemetry.py` | Versioned `measurement` / `tags` / `fields` contract and catalog validation |
| `catalog.py` | Repository-owned field semantics, units and cardinality |
| `plan.py` | Multi-target YAML Profile validation and Collection Job compilation |
| `monitoring.example.yaml` | Safe example Profiles and Targets; contains secret references only |
| `runtime.py` | Per-Target workers, independent cadence, reconnect/backoff and heartbeat |
| `groups.py` | Collection Group registry and MySQL-to-telemetry conversion |
| `secrets.py` | Environment and managed-identity Key Vault reference resolution |
| `connection.py` | TLS-enforced connection factory built from environment variables |
| `metrics.py` | Runs the queries in [`../sql/`](../sql/) and normalises results |
| `events.py` | Cursor-based incremental read of `performance_schema.error_log` |
| `sinks/jsonl.py` | Append-only JSON Lines writer (raw archive, cold-path source) |
| `sinks/adx.py` | ADX streaming (hot) and queued (cold) ingestion — **optional extra** |
| `requirements.txt` | Core: `mysql-connector-python` and PyYAML |
| `requirements-adx.txt` | Azure extra: ADX clients, managed identity, and Key Vault secrets |

## Collection Plan (v2)

Copy the example outside source control, edit the Targets and validate it:

```bash
cp monitoring.example.yaml monitoring.yaml
python plan.py monitoring.yaml
```

Expected result:

```text
VALID: 2 Targets, 4 Profiles, 14 Collection Jobs
```

The validator rejects:

- literal usernames/passwords instead of environment-variable or Key Vault references;
- unknown Collection Groups or options;
- high-cardinality groups without explicit Profile opt-in;
- intervals below each group's safety floor;
- duplicate Target IDs and inheritance cycles.

Profiles separate monitoring depth from code:

| Profile | Intent |
|---|---|
| `standard` | General production health at low cardinality |
| `extended` | File IO latency, process state, statement digest and capacity diagnosis |
| `deep-dive` | Opt-in table/index dimensions with top-K bounds |
| `benchmark` | Short cadence for QPS, IO and latency, compared by `RUN_ID` |

Set the environment variables referenced by each Target, plus the ADX settings when an ADX sink is
selected, then start the multi-target runtime:

```bash
export SERVER_A_RUN_ID="prod"
export SERVER_A_MYSQL_USER="monitor"
export ADX_CLUSTER_URI="https://<cluster>.<region>.kusto.windows.net"
export ADX_INGEST_URI="https://ingest-<cluster>.<region>.kusto.windows.net"
export ADX_DATABASE="<database>"

python collector.py --config monitoring.yaml \
  --sink adx-streaming --sink jsonl --out telemetry.jsonl
```

Each Target owns its MySQL connection, schedule, reconnect backoff and error-log cursor. One failed
server therefore keeps emitting an unreachable heartbeat without blocking sibling Targets.
Key Vault references use `DefaultAzureCredential`; assign the collector VM's managed identity
permission to read only the named secrets.

## Single-target compatibility configuration

**Never hardcode credentials.** Compatibility mode reads connection info from environment
variables:

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

- **Python 3.11+.** Core dependencies are `mysql-connector-python` and PyYAML. **No ORM.**
- **Azure integration is the sanctioned dependency exception**, isolated behind lazy imports and
  `requirements-adx.txt` (`azure-kusto-ingest`, `azure-identity`, `azure-keyvault-secrets`). An
  environment-only JSONL benchmark works with the core requirements alone.
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

## Telemetry contract v2

New Collection Groups emit a canonical Telemetry Point rather than encoding dimensions in a metric
name:

```json
{
  "ts": "2026-08-10T00:53:04.000000Z",
  "contract_version": 2,
  "run_id": "prod",
  "target_id": "orders-db",
  "host": "<server>",
  "tier": "premium-ssd-v2",
  "measurement": "mysql.file_io",
  "series_key": "<stable-hash>",
  "tags": {"event": "innodb_data_file", "mode": "read"},
  "fields": {"operations_total": 1557, "wait_ms_total": 3718.8}
}
```

The Metric Catalog validates field kind (`counter`, `gauge`, `state`), unit and allowed dimensions.
Dashboard-critical numeric fields can be projected to narrow time-series rows. A compatibility
adapter keeps existing `MysqlMetrics` dashboards working while ADX migrates.

## Legacy output shape

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
