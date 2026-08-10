# grafana/provisioning/ — deployment wiring

Ties [`../dashboards/`](../dashboards/) and [`../datasources/`](../datasources/) together so a
Grafana instance comes up fully configured from this repository, with no manual UI setup.

## Expected contents

| File | Purpose |
|---|---|
| `dashboards.yaml` | Dashboard provider: folder, update interval, source path |
| `folders.yaml` | Folder layout (e.g. `Benchmark`, `Production`) |
| `alerting.yaml` | Grafana-managed alert rules and evaluation intervals |
| `grafana.bicep` | Azure Managed Grafana (Standard) + role assignments |
| `docker-compose.yaml` | Local self-hosted Grafana for development |

## Dashboard provider

```yaml
apiVersion: 1
providers:
  - name: azure-mysql-monitoring
    folder: MySQL
    type: file
    disableDeletion: false
    allowUiUpdates: false        # repo is the source of truth
    options:
      path: /etc/grafana/provisioning/dashboards/json
      foldersFromFilesStructure: true
```

`allowUiUpdates: false` is deliberate: dashboards are reviewed in pull requests, not edited in the
browser and lost on the next deploy.

## Deployment targets

**Azure Managed Grafana (Standard tier)** is the target. Standard is required because the Azure
Data Explorer data source is not available on the deprecated Essential tier.

Provision with Bicep alongside [`../../adx/bicep/`](../../adx/bicep/), and grant the workspace's
**managed identity**:

| Role | Scope | Why |
|---|---|---|
| ADX database `Viewer` | ADX database | Read metrics and error events |
| `Monitoring Reader` | Log Analytics workspace | Read Slow/Audit logs and platform metrics |

Both are read-only, so no ingestion or write path exists through Grafana, and no secret is stored
anywhere.

A self-hosted Grafana is supported for local development only: mount this directory at
`/etc/grafana/provisioning/` and supply the environment variables below.

## Alert rule provisioning

Grafana-managed alert rules are provisioned from this repo as well, so evaluation intervals stay
under review. The real-time budget assumes **10–30s evaluation** on ADX-backed rules, including the
mandatory `collector_heartbeat` rule described in [`../README.md`](../README.md).

## Rules

- **Never commit credentials.** Provisioning files may only reference `${ENV_VAR}`; with managed
  identity there is normally no secret to inject at all.
- Do not commit a real `.env` or a rendered provisioning file containing expanded values.
- Keep data source `uid` values stable across environments so dashboard JSON stays portable.
- Grafana identities are **read-only**; ingestion rights belong to the collector identity.

## Configuration

| Variable | Description |
|---|---|
| `ADX_CLUSTER_URI` | `https://<cluster>.<region>.kusto.windows.net` |
| `ADX_DATABASE` | Database holding `MysqlMetrics` / `MysqlEvents` |
| `AZURE_SUBSCRIPTION_ID` | Subscription for the Azure Monitor data source |
| `LOG_ANALYTICS_WORKSPACE_ID` | Workspace receiving Slow/Audit logs |
| `RUN_ID` | Benchmark run identifier, surfaced as the `$run_id` dashboard variable |

```bash
export ADX_CLUSTER_URI="https://<cluster>.<region>.kusto.windows.net"
export ADX_DATABASE="<database>"
export RUN_ID="ssdv2-2026-08-10-01"

docker compose up -d    # local development instance only
```
