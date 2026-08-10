# mysql-internal/collector/ — Python metrics collector

A small **Python 3.11+** program that connects to Azure Database for MySQL Flexible Server
(**MySQL 8.4**) over TLS, polls it on a fixed interval, and emits one tagged metric row per sample.

This is the **primary data source during benchmark runs**, because Premium SSD v2 servers are in
preview and Azure platform metrics may be incomplete for them.

## Expected contents

| File | Purpose |
|---|---|
| `collector.py` | Entry point: poll loop, interval, graceful shutdown |
| `connection.py` | TLS-enforced connection factory built from environment variables |
| `metrics.py` | Runs the queries in [`../sql/`](../sql/) and normalises results |
| `output.py` | Append-only CSV / JSON Lines writer, plus the MySQL sink |
| `requirements.txt` | `mysql-connector-python` (or `PyMySQL`) — nothing else |

## Configuration

**Never hardcode credentials.** All connection info comes from environment variables:

| Variable | Required | Description |
|---|---|---|
| `MYSQL_HOST` | yes | Flexible Server FQDN, e.g. `<name>.mysql.database.azure.com` |
| `MYSQL_USER` | yes | Monitoring user (`PROCESS`, `SELECT` on `performance_schema`) |
| `MYSQL_PASSWORD` | yes | Password — never logged, never committed |
| `MYSQL_DB` | yes | Default database/schema |
| `RUN_ID` | yes | Tagged onto **every** metric row for benchmark correlation |

```bash
export MYSQL_HOST="<server>.mysql.database.azure.com"
export MYSQL_USER="<user>"
export MYSQL_PASSWORD="<password>"
export MYSQL_DB="<database>"
export RUN_ID="ssdv2-2026-08-10-01"

python collector.py --interval 10 --out ../../benchmark-integration/runs/$RUN_ID.jsonl
```

PowerShell:

```powershell
$env:MYSQL_HOST = "<server>.mysql.database.azure.com"
$env:MYSQL_USER = "<user>"
$env:MYSQL_PASSWORD = "<password>"
$env:MYSQL_DB     = "<database>"
$env:RUN_ID       = "ssdv2-2026-08-10-01"

python collector.py --interval 10 --out "..\..\benchmark-integration\runs\$env:RUN_ID.jsonl"
```

## Rules

- **Python 3.11+.** Runtime dependencies limited to `mysql-connector-python` (or `PyMySQL`);
  everything else must come from the standard library. **No ORM.**
- **TLS is mandatory.** Azure sets `require_secure_transport=ON`; never disable SSL, and never
  fall back to an unencrypted connection.
- MySQL 8.4 uses **`caching_sha2_password`** by default (`mysql_native_password` is disabled);
  the driver must support it.
- Read `innodb_redo_log_capacity`, not the removed `innodb_log_file_size`.
- **All timestamps are UTC ISO-8601** — emit timezone-aware values, never naive or local time.
- **Every row includes `RUN_ID`** plus the sampled server identity, so v1 and v2 runs stay
  distinguishable.
- Output is append-only and machine-readable so a run can be replayed and re-analysed.
- Redact `MYSQL_PASSWORD` from any config dump or log line.

## Output shape

One record per sample, for example:

```json
{"ts":"2026-08-10T00:53:04Z","run_id":"ssdv2-2026-08-10-01","host":"<server>","metric":"Innodb_data_reads","value":184392}
```

Downstream, [`../../benchmark-integration/`](../../benchmark-integration/) joins these rows with
benchmark results on `run_id` and the time axis.

## MySQL sink (required by Grafana)

Besides the JSON Lines file, the collector persists samples to a MySQL table so
[`../../grafana/`](../../grafana/) can graph them. Grafana cannot chart `SHOW GLOBAL STATUS`
directly — a live `SHOW` returns an instantaneous snapshot with no time axis.

```sql
CREATE TABLE IF NOT EXISTS monitoring_metrics (
  id      BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  ts      DATETIME(3)     NOT NULL,   -- UTC, always
  run_id  VARCHAR(64)     NOT NULL,
  host    VARCHAR(255)    NOT NULL,
  tier    VARCHAR(32)     NOT NULL,   -- premium-ssd-v1 | premium-ssd-v2
  metric  VARCHAR(128)    NOT NULL,
  value   DOUBLE          NOT NULL,
  PRIMARY KEY (id),
  KEY idx_run_ts (run_id, ts),
  KEY idx_metric_ts (metric, ts)
) ENGINE=InnoDB;
```

- `ts` is stored in **UTC**. Grafana's MySQL data source is configured with `timezone: UTC` to
  match; a mismatch shifts every panel and silently invalidates a v1 vs v2 comparison.
- `run_id` is on every row so the `$run_id` dashboard variable can isolate one benchmark run.
- Write this table to a **separate database or server** from the one under benchmark, so collection
  writes do not perturb the measurement.
