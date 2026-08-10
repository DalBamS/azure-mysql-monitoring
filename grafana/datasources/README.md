# grafana/datasources/ — data source provisioning

Grafana data source definitions (provisioning YAML). Two data sources back every dashboard, one per
monitoring layer.

## Expected contents

| File | Purpose |
|---|---|
| `azure-monitor.yaml` | Layer 1 — Azure Monitor / Log Analytics |
| `mysql.yaml` | Layer 2 — the collector's metrics table on MySQL 8.4 |

## 1. Azure Monitor (Layer 1)

Queries platform metrics and Log Analytics logs. Reuse the KQL in
[`../../azure-native/kql/`](../../azure-native/kql/) rather than writing divergent copies.

Prefer **Azure Managed Grafana with a managed identity** — then no client secret exists at all:

```yaml
apiVersion: 1
datasources:
  - name: AzureMonitor
    type: grafana-azure-monitor-datasource
    uid: azmon
    jsonData:
      azureAuthType: msi          # managed identity — no secret to store
      subscriptionId: ${AZURE_SUBSCRIPTION_ID}
      logAnalyticsDefaultWorkspace: ${LOG_ANALYTICS_WORKSPACE_ID}
```

If managed identity is unavailable, inject the client secret from a vault at runtime via
`${...}` expansion. **Never commit a secret value.**

## 2. MySQL (Layer 2)

Reads the metrics rows written by [`../../mysql-internal/collector/`](../../mysql-internal/collector/).
This is the **primary source during benchmark runs**, because Premium SSD v2 is in preview and its
Azure platform telemetry may be incomplete.

```yaml
apiVersion: 1
datasources:
  - name: MySQLMetrics
    type: mysql
    uid: mysqlmetrics
    url: ${MYSQL_HOST}:3306
    user: ${MYSQL_USER}
    database: ${MYSQL_DB}
    jsonData:
      tlsAuth: true
      sslmode: require            # Azure enforces require_secure_transport=ON
      timezone: UTC               # rows are stored as UTC
    secureJsonData:
      password: ${MYSQL_PASSWORD} # expanded at runtime, never committed
```

### MySQL 8.4 notes

- Grafana's MySQL driver supports **`caching_sha2_password`**, the 8.4 default. Do **not** try to
  move the monitoring user to `mysql_native_password` — it is disabled by default in 8.4.
- Azure sets `require_secure_transport=ON`, so **TLS is mandatory**. Never set an
  SSL-disabled/skip-verify mode; a plaintext connection is rejected by the server anyway.
- Use a **read-only** monitoring user; this data source must never be able to write.
- Keep `timezone: UTC` aligned with the collector, which stores UTC ISO-8601 timestamps. A mismatch
  shifts every panel and silently invalidates a v1 vs v2 comparison.

## Rules

- **Never hardcode credentials, hostnames, or workspace IDs.** Use `${ENV_VAR}` expansion only.
- Keep `uid` values stable — dashboards in [`../dashboards/`](../dashboards/) reference them.
- Data sources are provisioned from this repo, not created by hand in the UI.

## Configuration

All values come from environment variables:

| Variable | Description |
|---|---|
| `MYSQL_HOST` | Flexible Server FQDN, e.g. `<name>.mysql.database.azure.com` |
| `MYSQL_USER` | Read-only monitoring user |
| `MYSQL_PASSWORD` | Password (never logged, never committed) |
| `MYSQL_DB` | Database holding the collector's metrics table |
| `RUN_ID` | Benchmark run identifier, surfaced to dashboards as `$run_id` |
| `AZURE_SUBSCRIPTION_ID` | Subscription for the Azure Monitor data source |
| `LOG_ANALYTICS_WORKSPACE_ID` | Default Log Analytics workspace |
