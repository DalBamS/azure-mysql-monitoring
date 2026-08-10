# azure-native/bicep/ — Infrastructure as Code

**Bicep** templates that provision every Azure resource this monitoring solution needs.
This is the only sanctioned way to create monitoring infrastructure: no portal click-ops,
no hand-authored ARM JSON, no Terraform.

## Expected contents

| File | Purpose |
|---|---|
| `main.bicep` | Entry point; wires the modules below together |
| `logAnalytics.bicep` | Log Analytics workspace (retention, SKU) |
| `diagnosticSettings.bicep` | Diagnostic settings on the Flexible Server → Log Analytics |
| `alertRules.bicep` | Metric/log alert rules and action groups |
| `main.parameters.example.json` | **Example** parameters — placeholders only |

## Rules

- **Never hardcode credentials, hostnames, or resource IDs** in templates or parameter defaults.
  Pass them as parameters, and mark secrets with `@secure()`.
- Do not commit a real `main.parameters.json`. Commit only `*.example.json` with placeholders.
- Target Azure Database for MySQL **Flexible Server** running **MySQL 8.4**.

## Configuration

Deployment inputs come from environment variables / CI secrets, not from the repo:

| Variable | Description |
|---|---|
| `AZURE_SUBSCRIPTION_ID` | Target subscription |
| `AZURE_RESOURCE_GROUP` | Resource group for the monitoring resources |
| `MYSQL_SERVER_NAME` | Flexible Server resource name |
| `LOG_ANALYTICS_WORKSPACE` | Workspace name |

Database connection details, if ever needed by tooling here, use the repo-wide variables and are
never hardcoded:

| Variable | Description |
|---|---|
| `MYSQL_HOST` | Flexible Server FQDN |
| `MYSQL_USER` | MySQL user |
| `MYSQL_PASSWORD` | Password (never logged, never committed) |
| `MYSQL_DB` | Default database/schema |
| `RUN_ID` | Benchmark run identifier |

## Deploy

```bash
az deployment group create \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --template-file main.bicep \
  --parameters mysqlServerName="$MYSQL_SERVER_NAME" \
               workspaceName="$LOG_ANALYTICS_WORKSPACE"
```

Validate before deploying:

```bash
az bicep build --file main.bicep
az deployment group what-if --resource-group "$AZURE_RESOURCE_GROUP" --template-file main.bicep
```

## Premium SSD v2 note

Premium SSD v2 is in **preview**. Some diagnostic categories or metric definitions may not be
available for v2-backed servers; templates should tolerate their absence rather than fail the
deployment.
