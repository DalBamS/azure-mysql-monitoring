# grafana/datasources/ — data source provisioning

Data source definitions for **Azure Managed Grafana**. Two data sources back every dashboard.

> **Tier requirement:** the Azure Data Explorer data source is available on the Managed Grafana
> **Standard** tier. It is not offered on the deprecated Essential tier.

## Expected contents

| File | Purpose |
|---|---|
| `azure-data-explorer.yaml` | **Primary** — the unified metrics + logs store |
| `azure-monitor.yaml` | Layer 1 — platform metrics, plus Slow/Audit logs in Log Analytics |

## 1. Azure Data Explorer (primary)

Reads everything the collector produces: numeric metrics, `performance_schema.error_log` events,
and benchmark run metadata. This is the real-time path and the long-term store.

```yaml
apiVersion: 1
datasources:
  - name: ADX
    type: grafana-azure-data-explorer-datasource
    uid: adx
    jsonData:
      azureCredentials:
        authType: msi          # Managed Grafana's managed identity — no secret
      clusterUrl: ${ADX_CLUSTER_URI}
      defaultDatabase: ${ADX_DATABASE}
      dataConsistency: strongconsistency
```

Grant that managed identity the **`Viewer`** role on the ADX database — read-only, assigned in
[`../../adx/bicep/`](../../adx/bicep/). Nothing else is needed, and no credential is stored.

## 2. Azure Monitor (Layer 1)

Platform metrics and the two resource log categories Flexible Server actually emits —
**`MySQL Audit Logs`** and **`MySQL Slow Logs`**, both landing in the `AzureDiagnostics` table.

```yaml
apiVersion: 1
datasources:
  - name: AzureMonitor
    type: grafana-azure-monitor-datasource
    uid: azmon
    jsonData:
      azureAuthType: msi
      subscriptionId: ${AZURE_SUBSCRIPTION_ID}
      logAnalyticsDefaultWorkspace: ${LOG_ANALYTICS_WORKSPACE_ID}
```

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
| `ADX_DATABASE` | Database holding `MysqlMetrics` / `MysqlEvents` |
| `AZURE_SUBSCRIPTION_ID` | Subscription for the Azure Monitor data source |
| `LOG_ANALYTICS_WORKSPACE_ID` | Workspace receiving Slow/Audit logs |
| `RUN_ID` | Benchmark run identifier, surfaced to dashboards as `$run_id` |

MySQL connection variables (`MYSQL_HOST`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DB`) are **not**
used here. Grafana never connects to MySQL directly; it reads what the collector already ingested
into ADX. Those variables belong to
[`../../mysql-internal/collector/`](../../mysql-internal/collector/).
