# grafana/datasources/ — data source provisioning

Data source definitions for **Azure Managed Grafana**. Two data sources back every dashboard.

> **Tier requirement:** the Azure Data Explorer data source is available on the Managed Grafana
> **Standard** tier. It is not offered on the deprecated Essential tier.

## Contents

| File | uid | Purpose |
|---|---|---|
| `adx.json` | `mysqlmon-adx` | **Primary** — the unified metrics + logs store |
| `azure-monitor.json` | `azure-monitor-oob` | Layer 1 — platform metrics, plus Slow/Audit logs in Log Analytics |

Apply them with [`../provisioning/deploy.ps1`](../provisioning/deploy.ps1), which substitutes the
`${ENV_VAR}` placeholders and creates or updates each data source in place.

These are **Grafana HTTP API payloads**, not `provisioning/datasources/*.yaml` files. Azure Managed
Grafana does not mount a provisioning directory — it is configured through the API — so the API
shape is the one that actually works against the deployment target.

## 1. Azure Data Explorer (primary)

Reads everything the collector produces: numeric metrics, `performance_schema.error_log` events,
and benchmark run metadata. This is the real-time path and the long-term store.

Authentication is `azureCredentials.authType: msi` — the workspace's own managed identity, so no
secret exists to leak or rotate. Grant that identity the **`Viewer`** role on the ADX database,
assigned in [`../../adx/bicep/`](../../adx/bicep/).

> Verified on the live test environment: the Grafana managed identity appears in the ADX database
> principal list as `principalType: App` with role `Viewer`, and a query through the Grafana proxy
> returned real rows from `MysqlMetricSeries`.

## 2. Azure Monitor (Layer 1)

Platform metrics and the two resource log categories Flexible Server actually emits —
**`MySQL Audit Logs`** and **`MySQL Slow Logs`**, both landing in the `AzureDiagnostics` table.

Azure Managed Grafana **ships this data source built in**, already using managed identity, under the
fixed uid `azure-monitor-oob`. A uid cannot be changed after creation and the name must be unique,
so creating a second "Azure Monitor" fails with a 409. This repo therefore adopts the built-in uid
rather than duplicating it: `azure-monitor.json` updates the existing data source in place, pinning
`subscriptionId`. On a self-hosted Grafana the same file simply creates one with that uid, so
dashboard JSON stays portable across both.

**There is no error-log category** in Flexible Server diagnostic settings. Error-log data comes only
from the ADX data source, fed by the collector reading `performance_schema.error_log`.

## Which data source for which panel

| Panel | Data source | Why |
|---|---|---|
| Live health, alerting | ADX | Seconds-latency streaming ingestion |
| Benchmark v1 vs v2 | ADX | Only source with complete v2 preview coverage |
| Error log | ADX | Not available via Azure diagnostics at all |
| Slow / audit logs | Azure Monitor | Already collected by diagnostic settings; no need to duplicate |
| Storage, CPU, host-level | Azure Monitor | Platform view outside the engine |

## Rules

- **Never hardcode credentials, cluster URIs, or workspace IDs.** Use `${ENV_VAR}` expansion, and
  prefer managed identity so there is no secret to expand in the first place.
- Keep `uid` values stable — dashboards in [`../dashboards/`](../dashboards/) reference them.
- Both identities are **read-only**. Ingestion rights belong to the collector identity, never to
  Grafana.
- Data sources are provisioned from this repo, not created by hand in the UI.

## Configuration

| Variable | Description |
|---|---|
| `ADX_CLUSTER_URI` | `https://<cluster>.<region>.kusto.windows.net` |
| `ADX_DATABASE` | Database holding `MysqlTelemetry`, `MysqlMetricSeries` and `MysqlEvents` |
| `AZURE_SUBSCRIPTION_ID` | Subscription for the Azure Monitor data source |
| `LOG_ANALYTICS_WORKSPACE_ID` | Workspace receiving Slow/Audit logs |
| `RUN_ID` | Benchmark run identifier, surfaced to dashboards as `$run_id` |

MySQL connection variables (`MYSQL_HOST`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DB`) are **not**
used here. Grafana never connects to MySQL directly; it reads what the collector already ingested
into ADX. Those variables belong to
[`../../mysql-internal/collector/`](../../mysql-internal/collector/).
