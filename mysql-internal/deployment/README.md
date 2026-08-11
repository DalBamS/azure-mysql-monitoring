# Collector VM deployment

Production packaging for the multi-Target collector: a Linux VM with no public NIC, stable NAT
egress, system-assigned managed identity, least-privilege Key Vault/ADX access, a hardened systemd
unit, persistent cursor state and bounded durable spool.

## Contents

| Path | Purpose |
|---|---|
| `bicep/main.bicep` | VM, VNet, NSG, NAT gateway, managed identity and optional access assignments |
| `cloud-init.yaml` | Base packages, service account and persistent directories |
| `config/` | Secret-reference-only Collection Plan and environment examples |
| `systemd/` | Hardened collector service |
| `scripts/install.sh` | Idempotent application and service installation |
| `scripts/health-check.sh` | Service, spool-capacity and corruption probe |
| `OPERATIONS.md` | Deployment, outage, replay, upgrade and rollback runbook |

## Deploy infrastructure

The template creates no inbound Internet path. Administration requires Azure Bastion, private
network access, or `az vm run-command`. Its NAT public IP is stable and can be added to an existing
Flexible Server firewall by setting `mysqlServerName`.

```bash
cd mysql-internal/deployment/bicep
cp main.parameters.example.json main.parameters.json
# Edit placeholders. Never commit main.parameters.json.
az deployment group create \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --template-file main.bicep \
  --parameters @main.parameters.json
```

`keyVaultName`, `adxClusterName`, `adxDatabaseName` and `mysqlServerName` refer to resources in the
same resource group. The deployer needs permission to create role assignments. List every allowed
password secret in `keyVaultSecretNames`; **Key Vault Secrets User** is assigned at those individual
secret scopes, not the whole vault. The VM gets ADX database **Ingestor** only.

When `createKeyVault=true`, first deploy with `keyVaultSecretNames: []`, create the secrets through
your approved secret-management process, then redeploy with their names. The template never accepts
or creates secret values.

## Install the service

Copy or clone this repository onto the private VM, then:

```bash
sudo mysql-internal/deployment/scripts/install.sh
sudoedit /etc/azure-mysql-monitoring/monitoring.yaml
sudoedit /etc/azure-mysql-monitoring/collector.env
sudo systemctl enable --now azure-mysql-monitoring.service
sudo mysql-internal/deployment/scripts/health-check.sh
```

The installer never overwrites existing configuration. It runs as the dedicated `mysqlmon` user,
uses Python 3.11+ (Ubuntu 24.04 supplies Python 3.12), and stores mutable state only under
`/var/lib/azure-mysql-monitoring`.

## Environment

Every runnable deployment uses references and Azure settings, never a literal password:

| Variable | Purpose |
|---|---|
| `ADX_CLUSTER_URI` | ADX engine URI for streaming ingestion |
| `ADX_INGEST_URI` | ADX ingestion URI for queued spool replay |
| `ADX_DATABASE` | ADX database name |
| `COLLECTOR_SPOOL_DIR` | Persistent per-Target JSONL spool directory |
| `COLLECTOR_SPOOL_MAX_BYTES` | Hard disk budget; new data is rejected loudly when full |
| `COLLECTOR_SPOOL_REPLAY_SECONDS` | Queued replay scan cadence |
| `COLLECTOR_SPOOL_CONFIRMATION_TIMEOUT_SECONDS` | Resubmit when ADX terminal status is missing; the ingestion tag prevents duplicates |
| `<TARGET>_RUN_ID` | `RUN_ID` stamped on every row, usually `prod` |
| `<TARGET>_MYSQL_USER` | Non-secret monitoring username reference |
| `MYSQL_HOST` | Single-target compatibility mode only |
| `MYSQL_USER` | Single-target compatibility mode only |
| `MYSQL_PASSWORD` | Single-target compatibility mode only; never store it in the service file |
| `MYSQL_DB` | Single-target compatibility mode only |

Target passwords belong in Key Vault and are resolved with the VM managed identity. All MySQL
connections use TLS, and the example pins certificate validation to the OS CA bundle.
