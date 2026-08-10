# grafana/provisioning/ — deployment wiring

Ties [`../dashboards/`](../dashboards/) and [`../datasources/`](../datasources/) together so a
Grafana instance comes up fully configured from this repository, with no manual UI setup.

## Expected contents

| File | Purpose |
|---|---|
| `dashboards.yaml` | Dashboard provider: folder, update interval, source path |
| `folders.yaml` | Folder layout (e.g. `Benchmark`, `Production`) |
| `grafana.bicep` | Azure Managed Grafana instance + role assignments (optional) |
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

- **Azure Managed Grafana (preferred).** Provision with Bicep, matching
  [`../../azure-native/bicep/`](../../azure-native/bicep/). Grant its **managed identity** the
  `Monitoring Reader` role on the Log Analytics workspace so the Azure Monitor data source needs no
  secret.
- **Self-hosted Grafana.** Mount this directory at `/etc/grafana/provisioning/` and supply the
  environment variables below to the container.

Network access to the Flexible Server (private endpoint or firewall rule) must allow the Grafana
instance to reach `MYSQL_HOST` on 3306 **over TLS** — Azure enforces
`require_secure_transport=ON`.

## Rules

- **Never commit credentials.** Provisioning files may only reference `${ENV_VAR}`; secrets are
  injected at startup from Key Vault, CI secrets, or a managed identity.
- Do not commit a real `.env` or a rendered provisioning file containing expanded values.
- Keep data source `uid` values stable across environments so dashboard JSON stays portable.
- MySQL 8.4 only: the monitoring user uses `caching_sha2_password` (8.4 disables
  `mysql_native_password` by default), and the connection is TLS-only.

## Configuration

| Variable | Description |
|---|---|
| `MYSQL_HOST` | Flexible Server FQDN |
| `MYSQL_USER` | Read-only monitoring user |
| `MYSQL_PASSWORD` | Password (never logged, never committed) |
| `MYSQL_DB` | Database holding the collector's metrics table |
| `RUN_ID` | Benchmark run identifier, surfaced as the `$run_id` dashboard variable |
| `AZURE_SUBSCRIPTION_ID` | Subscription for the Azure Monitor data source |
| `LOG_ANALYTICS_WORKSPACE_ID` | Workspace queried by the Azure Monitor data source |

```bash
export MYSQL_HOST="<server>.mysql.database.azure.com"
export MYSQL_USER="<user>"
export MYSQL_PASSWORD="<password>"
export MYSQL_DB="<database>"
export RUN_ID="ssdv2-2026-08-10-01"

docker compose up -d    # local development instance
```
