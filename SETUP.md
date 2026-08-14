# SETUP.md — configuring monitoring for a production server

This guide takes an **existing** Azure Database for MySQL Flexible Server (MySQL 8.4) and builds the
full monitoring stack around it: Azure Monitor diagnostics, an ADX telemetry store, the Layer 2
collector, and Grafana dashboards.

> **Just want to see it work first?** [`testing/`](testing/) deploys a throwaway environment —
> server included — and verifies it end to end in about 25 minutes. That path creates its own
> MySQL server; this one attaches to yours. Do not run `testing/scripts/deploy.ps1` against a
> production resource group.

## Before you start

| Requirement | Notes |
|---|---|
| An Azure Database for MySQL Flexible Server, **8.4** | 8.0 will mostly work but is not tested here |
| Azure CLI, logged in | `az login`, then `az account set -s <subscription>` |
| Python **3.11+** | For the collector and the verification scripts |
| A VM to run the collector | Deploy the production package in [`mysql-internal/deployment/`](mysql-internal/deployment/) |
| `Contributor` on the resource group | Needed to create the workspace, ADX cluster and Grafana |

```bash
pip install -r mysql-internal/collector/requirements.txt
pip install azure-kusto-data azure-identity          # for the verification scripts
```

Decide two values now, because everything downstream is tagged with them:

| Value | Meaning |
|---|---|
| `RUN_ID` | Groups every metric row. Use `prod` for ongoing monitoring, or `ssdv2-2026-08-10-01` for a benchmark run |
| `MYSQL_TIER` | `premium-ssd-v1` or `premium-ssd-v2`. This is what makes the storage comparison possible |

---

## Step 1 — Layer 1: diagnostics into Log Analytics

Sends platform metrics and the two resource-log categories to a Log Analytics workspace, and
creates the metric alerts.

```bash
cd azure-native/bicep
cp main.parameters.example.json main.parameters.json
# edit main.parameters.json: mysqlServerName, and workspaceName if reusing one

az deployment group create \
  --resource-group <your-rg> \
  --template-file main.bicep \
  --parameters @main.parameters.json
```

Set `createWorkspace: false` and supply `existingWorkspaceId` to attach to a workspace you already
have, which is usually right in production — one workspace per environment, not per database.

### Step 1b — the part Bicep cannot do

**A diagnostic setting only forwards logs MySQL is already writing.** Deploying Step 1 and stopping
there produces an empty `AzureDiagnostics` table that looks exactly like a quiet server. The server
parameters have to be turned on separately:

```bash
SERVER=<your-server>
RG=<your-rg>

# Slow query log
az mysql flexible-server parameter set -g $RG -s $SERVER -n slow_query_log -v ON
az mysql flexible-server parameter set -g $RG -s $SERVER -n long_query_time -v 1

# Audit log — audit_log_events defaults to a narrow set; CONNECTION is what makes
# failed logins visible, which is usually the reason for enabling audit at all.
az mysql flexible-server parameter set -g $RG -s $SERVER -n audit_log_enabled -v ON
az mysql flexible-server parameter set -g $RG -s $SERVER -n audit_log_events -v "CONNECTION,GENERAL"
```

> `audit_log_events` with `GENERAL` logs every statement. On a busy gaming workload that is a large
> volume of data and a real cost. Start with `CONNECTION` alone unless you need statement auditing.

Logs take 5–15 minutes to first appear. Verify with:

```bash
export LOG_ANALYTICS_WORKSPACE_ID=<workspace customerId>
python azure-native/kql/check-kql.py
```

---

## Step 2 — the ADX telemetry store

Layer 1 gives you slow logs, audit logs and platform metrics. It does **not** give you the MySQL
error log, per-query digests, or reliable Premium SSD v2 coverage. Those come from Layer 2, and
ADX is where they land.

```bash
cd adx/bicep
cp main.parameters.example.json main.parameters.json
# edit: clusterName, collectorPrincipalId, grafanaPrincipalId

az deployment group create \
  --resource-group <your-rg> \
  --template-file main.bicep \
  --parameters @main.parameters.json
```

**Check SKU availability in your region first.** An unavailable SKU fails roughly ten minutes into
the deployment, and neither `bicep build` nor `what-if` catches it:

```bash
az kusto cluster list-sku -l <region> --query "[].{name:name, tier:tier}" -o table
```

`collectorPrincipalId` is the managed identity of whatever runs the collector; `grafanaPrincipalId`
is the Grafana workspace's identity. They get `Ingestor` and `Viewer` respectively — Grafana is
read-only by design, and never holds ingestion rights.

Then apply the schema:

```bash
export ADX_CLUSTER_URI="https://<cluster>.<region>.kusto.windows.net"
export ADX_DATABASE="mysqlmonitoring"
python testing/scripts/bootstrap_adx.py
```

This is idempotent — re-running it is how you apply a schema change.

---

## Step 3 — the collector

The collector reads `SHOW GLOBAL STATUS`, `performance_schema` digests, and
`performance_schema.error_log`, then writes to ADX.

**Nothing is hardcoded.** All connection detail comes from the environment:

```bash
export MYSQL_HOST="<server>.mysql.database.azure.com"
export MYSQL_USER="<user>"
export MYSQL_PASSWORD="<password>"      # from Key Vault, not from a file in this repo
export MYSQL_DB="<database>"
export MYSQL_TIER="premium-ssd-v1"
export RUN_ID="prod"
export ADX_CLUSTER_URI="https://<cluster>.<region>.kusto.windows.net"
export ADX_INGEST_URI="https://ingest-<cluster>.<region>.kusto.windows.net"
export ADX_DATABASE="mysqlmonitoring"
```

Grant the MySQL user the minimum it needs — it never reads customer data:

```sql
CREATE USER 'monitor'@'%' IDENTIFIED BY '<password>';
GRANT SELECT ON performance_schema.* TO 'monitor'@'%';
GRANT PROCESS, REPLICATION CLIENT ON *.* TO 'monitor'@'%';
```

`caching_sha2_password` is the default in 8.4 and `mysql_native_password` is disabled, so an older
client library will fail to authenticate. TLS is mandatory (`require_secure_transport=ON`); a
client without TLS configured fails the handshake and is counted as an aborted connection, never
reaching the error log.

Run it once in the foreground to confirm it connects:

```bash
python mysql-internal/collector/collector.py --interval 10 --max-cycles 3 --sink adx-streaming --verbose
```

Deploy the production VM and systemd package from
[`mysql-internal/deployment/`](mysql-internal/deployment/). It provides:

- no public VM NIC or public SSH path;
- stable NAT egress for MySQL firewall allow-listing;
- managed-identity Key Vault secret reads and ADX database ingestion;
- a dedicated non-root service account and hardened systemd unit;
- persistent per-Target error-log cursors;
- a bounded, per-Target JSONL spool that automatically replays ADX failures through queued ingestion.

```bash
sudo mysql-internal/deployment/scripts/install.sh
sudoedit /etc/azure-mysql-monitoring/monitoring.yaml
sudoedit /etc/azure-mysql-monitoring/collector.env
sudo systemctl enable --now azure-mysql-monitoring.service
sudo mysql-internal/deployment/scripts/health-check.sh
```

The spool is at-least-once: a crash after ADX accepts a request but before local deletion can create
duplicates. It never deletes older pending data to make room. When its hard limit is reached, new
batches are rejected with a CRITICAL journal entry so disk exhaustion cannot be silent.

Production intervals of 10–15s are a deliberate trade: benchmarks use 1–5s for resolution, but that
rate against a production server is a meaningful query load of its own.

---

## Step 4 — Grafana

```bash
cd grafana/provisioning
./deploy.ps1 -GrafanaName <workspace> -ResourceGroup <your-rg>
```

This creates both data sources and imports the dashboards into a `MySQL` folder. Both data sources
authenticate with the workspace's **managed identity**, so no secret is stored anywhere.

Azure Managed Grafana must be **Standard** tier — the ADX plugin is not available on Essential.

A note on the Azure Monitor data source: Managed Grafana ships one built in under the fixed uid
`azure-monitor-oob`, and a uid cannot be changed after creation. This repo adopts that uid rather
than creating a duplicate, so `azure-monitor.json` updates the built-in in place.

---

## Step 5 — prove it works

Deployment success does not mean monitoring works. Verify behaviour:

```bash
python azure-native/kql/check-kql.py                    # Layer 1 queries return rows
python grafana/dashboards/check-dashboard-queries.py    # every panel's KQL runs
```

Then confirm the two layers agree. Both record `Slow_queries` independently — Layer 1 from platform
metrics, Layer 2 from `SHOW GLOBAL STATUS`. If they diverge, one of them is misconfigured, and
that is worth knowing before an incident rather than during one.

Set up the one alert that protects everything else:

> **`collector_heartbeat` absence.** A dead collector does not produce errors — it produces flat
> lines, and a flat line is indistinguishable from a healthy idle server. Every other dashboard in
> this repo is only trustworthy while this alert is quiet. Open **Collector Health** in Grafana and
> confirm the heartbeat table reads `OK` before relying on anything else.

---

## What each layer can and cannot tell you

| Question | Layer 1 (Azure Monitor) | Layer 2 (collector → ADX) |
|---|---|---|
| CPU, memory, storage percent | ✅ | ❌ |
| Slow query text | ✅ (sampled, normalised) | ✅ (per-digest, unsampled) |
| Failed logins | ✅ audit log | ❌ |
| **MySQL error log** | ❌ **no such category** | ✅ only source |
| Per-query latency distribution | ❌ | ✅ |
| Premium SSD v2 coverage | ⚠️ preview, may have gaps | ✅ authoritative |
| Control-plane restarts/failovers | ✅ `AzureActivity` | ❌ |
| Sub-minute resolution | ❌ 1-minute floor | ✅ configurable |

This is why both layers exist. Layer 1 sees the platform from outside and survives the server being
unreachable; Layer 2 sees the engine from inside and is the only layer that works completely on a
preview storage tier.

---

## Cost

The ADX cluster dominates. Options, cheapest first:

- **Dev (No SLA) `Standard_D11_v2`** — single node, fine for a benchmark or a pilot.
- **Standard tier, 2 nodes** — for production, where the SLA matters.
- Enable auto-stop on non-production clusters. The production template sets `enableAutoStop: false`
  deliberately: a stopped cluster silently drops ingestion.

Retention is fixed at **90 days** for raw telemetry, events and materialized rollups. The
`MysqlMetrics1m` rollup keeps long-range queries inside that window cheap; it does not preserve
data after the 90-day lifecycle. See [`adx/policies/`](adx/policies/).

For Log Analytics, the workspace is deployed with **no daily cap**. A cap looks like cost control
and behaves like an outage: ingestion stops mid-day and the resulting gap is indistinguishable from
a dead server. Use commitment tiers to control cost instead.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `AzureDiagnostics` empty | Server parameters not set — see Step 1b |
| Collector authentication fails | Client library too old for `caching_sha2_password` |
| Collector connection times out | Firewall rule missing, or your public IP changed |
| Service runs but ADX data stops | Run deployment `health-check.sh`; inspect pending spool and managed-identity roles |
| Spool grows continuously | ADX streaming and queued endpoints are both unreachable or denied |
| ADX deployment fails ~10 min in | SKU unavailable in that region — check `az kusto cluster list-sku` |
| Grafana panels empty, no error | Metric name case is wrong; names are case-sensitive |
| Dashboards import but show nothing | Run `check-dashboard-queries.py` — it distinguishes a broken query from genuinely absent data |

More, including the failures found by actually deploying this, in
[`testing/README.md`](testing/README.md).
