# testing/ — real-Azure test environment

Deploys a live Azure environment and verifies that the monitoring pipeline in this repository
actually works, end to end. Everything here targets **real resources**; there is no emulator
and no container substitute, because the things most likely to be wrong — enforced TLS,
diagnostic log categories, streaming ingestion latency, managed-identity permissions — only
exist on the real platform.

> **This costs money.** The Azure Data Explorer cluster and the Managed Grafana workspace bill
> continuously while deployed, idle or not. Run [`scripts/teardown.ps1`](scripts/teardown.ps1)
> when you are finished. Deploy with `-SkipAdx -SkipGrafana` for the cheap subset.

## What gets deployed

| Resource | Layer | Purpose |
|---|---|---|
| MySQL Flexible Server 8.4 (`Standard_B1ms`) | target | The server being monitored |
| Log Analytics workspace + diagnostic settings | 1 | Slow logs, audit logs, platform metrics |
| Azure Data Explorer (`Dev(No SLA)_Standard_E2a_v4`) | store | Unified metrics + events store |
| Azure Managed Grafana (**Standard**) | 3 | Final view over both data sources |

Standard tier on Grafana is not a preference: the ADX data source does not exist on the
deprecated Essential tier, so an Essential workspace would deploy successfully and then be
unable to display any collector data.

## Layout

| Path | Purpose |
|---|---|
| `bicep/main.bicep` | Entry point, tiered by `deployAdx` / `deployGrafana` |
| `bicep/modules/` | `mysql`, `monitoring`, `adx`, `grafana`, `roles` |
| `scripts/deploy.ps1` | Creates the resource group, deploys, writes `testing/.env` |
| `scripts/load-env.ps1` | Loads `.env` into the shell (**dot-source it**) |
| `scripts/bootstrap_adx.py` | Applies the committed `.kql` schema to the live cluster |
| `scripts/workload.py` | Generates database load so counters actually move |
| `scripts/teardown.ps1` | Deletes everything; tag-guarded against pointing at production |
| `verify.py` | The end-to-end check — PASS/FAIL per assertion |

## Run order

```powershell
cd testing

# 1. Deploy. ADX provisioning takes 10-15 minutes.
./scripts/deploy.ps1 -ResourceGroup mysql-mon-test -Location koreacentral

# 2. Load the generated environment. The leading dot is required.
. ./scripts/load-env.ps1

# 3. Install dependencies
pip install -r ../mysql-internal/collector/requirements-adx.txt

# 4. Apply the ADX schema from the committed .kql files
python scripts/bootstrap_adx.py

# 5. Generate load in a second shell, so metrics are not flat
python scripts/workload.py --seconds 300

# 6. Collect while the load runs
python ../mysql-internal/collector/collector.py `
    --interval 5 --sink jsonl --sink adx-streaming `
    --out runs/$env:RUN_ID.jsonl --max-cycles 40

# 7. Verify
python verify.py

# 8. Tear down
./scripts/teardown.ps1 -ResourceGroup mysql-mon-test
```

Steps 5 and 6 must overlap. Running the collector against an idle server produces a flat
series, which is indistinguishable from a broken collector — the exact false negative this
environment exists to rule out.

## What each check proves

`verify.py` is not a deployment smoke test. It probes the assumptions the documentation in
this repository is built on, so a wrong assumption fails loudly here instead of silently
producing empty dashboards in production.

| Check | What a failure would mean |
|---|---|
| Connection is encrypted (`Ssl_cipher` non-empty) | `require_secure_transport=ON` is configured but not enforced |
| MySQL version is 8.4 | Metric names and variables in the repo may not match |
| `innodb_redo_log_capacity` exists, `innodb_log_file_size` gone | The 8.4 rename the repo depends on is wrong |
| `performance_schema.error_log` readable | **No error-log source exists at all** — Layer 1 has no such category to fall back on |
| Heartbeat emitted every cycle | A dead collector would flatline and read as healthy |
| Every row tagged `run_id` + `tier` | Benchmark runs could not be joined or told apart |
| Timestamps end in `Z` | Kusto would treat naive values as UTC and shift every chart |
| Streaming ingestion enabled on the table | "Real-time" is silently 5-minute queued batching |
| Probe row queryable within 90s | The documented 25-45s detection budget is not achievable |
| `CollectorHealth()` returns rows | Collector-down alerting has no working signal |
| Diagnostic categories contain no error log | Confirms the documented two-category limitation |
| Grafana is Standard tier | Essential cannot use the ADX data source |

Exit code is `0` when every check passes, `1` when any fails. `--json` writes
`runs/verify-report.json` for CI.

## Configuration

`deploy.ps1` generates `testing/.env`, which is **git-ignored** because it contains the MySQL
password. Nothing here is ever hardcoded:

| Variable | Description |
|---|---|
| `MYSQL_HOST` | Flexible Server FQDN |
| `MYSQL_USER` | Administrator login |
| `MYSQL_PASSWORD` | Generated at deploy time; never logged, never committed |
| `MYSQL_DB` | Test database |
| `MYSQL_TIER` | `premium-ssd-v1` for this environment |
| `RUN_ID` | Stamped on every row |
| `ADX_CLUSTER_URI` | Query endpoint (streaming ingestion and queries) |
| `ADX_INGEST_URI` | Ingestion endpoint (queued path) |
| `ADX_DATABASE` | Target database |
| `LOG_ANALYTICS_WORKSPACE_ID` | Workspace GUID for KQL queries |
| `GRAFANA_ENDPOINT` | Managed Grafana URL |
| `AZURE_RESOURCE_GROUP` | For teardown and Grafana lookups |

ADX and Grafana authenticate with **managed identity** (or your `az login` locally), so
`MYSQL_PASSWORD` is the only secret in the file.

## Scope

This environment uses **Premium SSD v1** only. Premium SSD v2 is in preview and needs regional
enablement, and the purpose here is to prove the monitoring pipeline works — not to benchmark
storage. Once the pipeline is verified, point the same collector at a v2-backed server and use
[`../benchmark-integration/`](../benchmark-integration/) for the comparison itself.

`long_query_time` is set to `0` so the slow query log reliably produces rows within a short
test. No production server should ever be configured that way.
