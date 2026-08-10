# adx/bicep/ — cluster and access IaC

Bicep templates for the Azure Data Explorer cluster and database, plus the role assignments that
let the collector write and Grafana read **without any secret existing anywhere**.

## Expected contents

| File | Purpose |
|---|---|
| `main.bicep` | Entry point wiring the modules below |
| `cluster.bicep` | ADX cluster, SKU, and `enableStreamingIngest` |
| `database.bicep` | Database and default retention/cache |
| `roleAssignments.bicep` | Collector ingest identity + Grafana reader identity |
| `main.parameters.example.json` | **Example** parameters — placeholders only |

## Streaming ingestion must be enabled at the cluster

The real-time path depends on it, and it cannot be turned on per table alone:

```bicep
resource cluster 'Microsoft.Kusto/clusters@2023-08-15' = {
  name: clusterName
  location: location
  sku: {
    name: skuName          // e.g. Dev(No SLA)_Standard_E2a_v4 for non-production
    tier: skuTier
    capacity: capacity
  }
  properties: {
    enableStreamingIngest: true    // required for the hot path
    enableAutoStop: false          // a stopped cluster silently drops monitoring
  }
}
```

`enableAutoStop: false` matters: auto-stop is a cost feature for idle dev clusters, and leaving it
on for a monitoring cluster means telemetry quietly stops.

## Least-privilege access

Two identities, two roles, no shared credentials:

| Principal | ADX role | Why |
|---|---|---|
| Collector managed identity | `Ingestor` on the database | Write-only; cannot read other tenants' data |
| Managed Grafana managed identity | `Viewer` on the database | Read-only dashboards |
| Deployment identity (CI) | `Admin` on the database | Applies table DDL and policies |

Grant the Grafana identity its role by principal ID so no connection string or app secret is ever
stored in [`../../grafana/datasources/`](../../grafana/datasources/).

## Rules

- **Never hardcode credentials, cluster URIs, or principal IDs.** Pass them as parameters; mark
  secrets `@secure()`.
- Commit only `*.example.json` parameters with placeholders — never a real `main.parameters.json`.
- Table DDL and policies live in [`../tables/`](../tables/) and [`../policies/`](../policies/) and
  are applied by CI after the cluster exists, not embedded in Bicep.

## Configuration

| Variable | Description |
|---|---|
| `AZURE_SUBSCRIPTION_ID` | Target subscription |
| `AZURE_RESOURCE_GROUP` | Resource group |
| `ADX_CLUSTER_NAME` | Cluster name |
| `ADX_DATABASE` | Database name |
| `ADX_CLUSTER_URI` | `https://<cluster>.<region>.kusto.windows.net` |
| `ADX_INGEST_URI` | `https://ingest-<cluster>.<region>.kusto.windows.net` |

```bash
az deployment group create \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --template-file main.bicep \
  --parameters clusterName="$ADX_CLUSTER_NAME" databaseName="$ADX_DATABASE"
```

## Sizing note

Cluster cost is the main reason to size deliberately. The dominant input is ingestion volume, which
is driven by **collector interval × number of retained status variables × server count** — MySQL
8.4 exposes 400+ status variables, so curating the list is the cheapest optimisation available.
Start small; ADX supports scaling the SKU later without changing schema or dashboards.
