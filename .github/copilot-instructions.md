# Copilot Instructions — azure-mysql-monitoring

These rules apply to **every** prompt, suggestion, and code change in this repository.
Read them before proposing anything.

## Project purpose

General-purpose monitoring for services that use **Azure Database for MySQL Flexible Server**.

1. **Ongoing production monitoring** is the primary mode.
2. **Benchmark runs** compare **Premium SSD v1 vs Premium SSD v2** by reusing the same telemetry
   contract and dashboards with different `RUN_ID` / storage-tier dimensions.

The supported production topology is multiple Azure MySQL Targets feeding one collector VM, with
ADX as the 90-day telemetry store and Grafana as the operator view. Domain terms are defined in
[`CONTEXT.md`](../CONTEXT.md); architecture decisions are recorded in [`docs/adr/`](../docs/adr/).

## Target platform

- Azure Database for MySQL **Flexible Server** only (no Single Server, no self-managed MySQL).
- **MySQL 8.4 only.** Do not write code, SQL, or docs that target 5.7, 8.0, or 9.x.
  If a feature differs across versions, implement the 8.4 behaviour and say so in a comment.

### MySQL 8.4 specifics that must be respected

- `caching_sha2_password` is the **default authentication plugin**.
  `mysql_native_password` is **disabled by default** — never assume it, never suggest
  falling back to it, and never generate `CREATE USER ... IDENTIFIED WITH mysql_native_password`.
- `innodb_redo_log_capacity` **supersedes** `innodb_log_file_size` / `innodb_log_files_in_group`.
  Use the new variable in all queries, tuning docs, and dashboards. Note that the legacy
  variable is **deprecated, not removed**: Azure MySQL 8.4.9 still returns
  `innodb_log_file_size` from `SHOW VARIABLES`, and it is ignored when capacity is set. So do
  not test for its absence — reading it back gives a stale number that no longer governs
  anything, which is worse than an error.
- Azure enforces TLS: `require_secure_transport=ON`. **All clients must connect with SSL.**
  Every connection helper must set SSL options explicitly (e.g. `ssl_disabled=False`,
  CA bundle where required). Never generate a connection that disables TLS.
- Prefer `performance_schema` over deprecated/removed status sources.

## Architecture — two collection layers, one view layer

### Layer 1 — `azure-native/`

Azure-platform telemetry:

- Azure Monitor platform metrics.
- Diagnostic settings shipping logs to **Log Analytics**. Flexible Server exposes **only two
  resource log categories: `MySQL Audit Logs` and `MySQL Slow Logs`**, both landing in the
  `AzureDiagnostics` table. **There is no error-log category** — that gap is Layer 2's job.
- **Azure Workbooks** for the portal-side view.
- **KQL** queries.
- Alert rules — the slow, independent safety net (2–5 min), which keeps working if the collector dies.
- All infrastructure is defined as **Bicep IaC** — no portal click-ops, no ARM JSON authored by hand,
  no Terraform.

### Layer 2 — `mysql-internal/`

A **Python collector** running on a VM polls multiple servers directly over TLS:

- `SHOW GLOBAL STATUS` — cumulative counters, sampled each interval.
- `performance_schema.error_log` — a **ring buffer**, read incrementally with a `LOGGED` cursor.
  This is the only way to get MySQL error-log data on Flexible Server.
- `performance_schema` summary tables — snapshot and diff.
- `information_schema` capacity and InnoDB measurements at a slower cadence.

Collection is driven by a validated YAML **Collection Plan**. It selects repository-owned
Collection Groups through Profiles; the main loop must not hardcode each group. The canonical
output is a Telegraf-inspired **Telemetry Point** with a measurement, tags and fields. Its Metric
Catalog owns field kinds, units, dimensions, cardinality and derived semantics.

High-cardinality telemetry (digest, schema, table, index, user or process dimensions) is opt-in and
bounded by top-K, filters and interval floors.

**Layer 2 is the primary data source during benchmark runs.** Premium SSD v2 servers are in
**preview**, so Azure platform metrics and diagnostic logs may be incomplete or missing for them.
When the two layers disagree during a benchmark, trust Layer 2 and note the discrepancy.

The collector **must emit a `collector_heartbeat` metric every cycle**. A dead collector makes
charts flatline, which looks healthy — absence of data must itself be alertable.

### Storage — `adx/`

**Azure Data Explorer is the single unified store** for both numeric metrics and text log events.
Do not propose a MySQL metrics table, a local-file-only store, Cosmos DB, or Prometheus — those were
evaluated and rejected (respectively: no text search and weak long-term scaling; not queryable by
Grafana; RU cost on write-heavy telemetry; cannot hold logs).

- **Two ingestion paths, one set of tables:** *streaming ingestion* (hot, seconds) for live
  monitoring, and *queued ingestion* (cold) for JSONL replay, backfill and benchmark archives.
- Queued ingestion batches up to **5 minutes** by default and **cannot** carry real-time monitoring.
  Never try to fix real-time by shrinking the batching policy alone; enable streaming ingestion.
- Streaming ingestion is capped at roughly **4 MB per request** — bulk loads use the queued path.
- **All ADX telemetry is retained for exactly 90 days**: raw points, events, dimensional data,
  materialized views and rollups. No policy may retain telemetry longer.
- Kusto `datetime` is **always UTC**, which matches this repo's timestamp rule exactly.
- `RunId` is on every row, including production rows (use a sentinel like `prod`).
- **Event Hub is deliberately not used.** Introduce it only if fan-in grows to hundreds of
  collectors or ADX-outage buffering becomes a requirement.

### Layer 3 — `grafana/`

**Azure Managed Grafana (Standard tier) is the final monitoring view** — the single surface where
the ADX store and Azure Monitor are shown on one time axis. Standard tier is required because the
Azure Data Explorer data source is not available on the deprecated Essential tier.

- Presentation and alerting only. Grafana never collects or stores data.
- Two data sources, both authenticated with **managed identity** and both **read-only**:
  **Azure Data Explorer** (primary) and **Azure Monitor** (Layer 1).
- Grafana **never connects to MySQL directly.** A live `SHOW GLOBAL STATUS` returns an
  instantaneous snapshot with no time axis; Grafana reads what the collector already ingested.
- Dashboards are **JSON committed to this repo and provisioned**, never UI-only edits.
- **`$run_id` is a template variable** on benchmark dashboards, so a v1 run and a v2 run can be
  compared without editing panels.
- **Match table to time range**: raw tables for live/short ranges, rollup views for long ranges.
- Real-time budget is **~25–45s** end to end (poll 10s + ingestion ~5s + alert evaluation 10–30s).
  Keep dashboard refresh at 10s and ADX alert evaluation at 10–30s.
- `azure-native/workbooks/` remains the Azure-native, portal-side view. Grafana is the primary
  operator-facing dashboard.

## Security — non-negotiable

- **Never hardcode credentials**, hostnames, or connection strings. Not in code, not in
  Bicep parameter defaults, not in READMEs, not in examples, not in tests.
- Single-target compatibility mode reads connection info from **environment variables**:
  - `MYSQL_HOST`
  - `MYSQL_USER`
  - `MYSQL_PASSWORD`
  - `MYSQL_DB`
  - `MYSQL_TIER` — `premium-ssd-v1` / `premium-ssd-v2`, stamped on every row
  - `RUN_ID` — benchmark run identifier, or a sentinel such as `prod`
- Azure resource identifiers are also environment variables or Bicep parameters, never literals:
  `ADX_CLUSTER_URI`, `ADX_INGEST_URI`, `ADX_DATABASE`, `AZURE_SUBSCRIPTION_ID`,
  `LOG_ANALYTICS_WORKSPACE_ID`.
- **Prefer managed identity over secrets.** The collector, Grafana, and CI all authenticate to Azure
  with managed/workload identities, so for ADX and Azure Monitor there is no secret to store at all.
  Grant least privilege: collector = ingest-only, Grafana = read-only.
- **Document these environment variables in every README** that describes runnable code.
- Never log the value of `MYSQL_PASSWORD`. Redact it if a config dump is printed.
- Do not commit `.env` files, `.pem` keys, or Azure credentials.
- Multi-target YAML contains **references only**: an environment-variable name or an Azure Key
  Vault URI/secret name. Literal usernames or passwords in YAML are invalid.

## Python conventions

- **Python 3.11+**.
- Core collector runtime dependencies: **`mysql-connector-python`** (or **`PyMySQL`**) plus
  **PyYAML** for the multi-target Collection Plan. Do not add an ORM or telemetry framework.
- **One sanctioned exception:** the ADX sink may use `azure-kusto-ingest` and `azure-identity`.
  Keep it isolated in `sinks/adx.py` with its own `requirements-adx.txt`, imported lazily so the
  core never hard-depends on it. Do not add further dependencies without changing this file.
- **No ORM** (no SQLAlchemy, no Django ORM, no Peewee). Write plain SQL.
- Prefer the standard library for everything else (`os`, `json`, `csv`, `datetime`, `logging`,
  `argparse`, `sqlite3`).
- Use parameterised queries; never build SQL by string concatenation with user input.
- A sink failure must never kill the poll loop — buffer, retry with backoff, keep sampling.

## Data conventions

- **All timestamps are UTC, ISO-8601** (e.g. `2026-08-10T00:53:04Z`).
  Never emit local time, never emit naive datetimes. Kusto `datetime` is always UTC, so a naive or
  local timestamp silently shifts every dashboard.
- **Every row is tagged with `RUN_ID`**, read from the `RUN_ID` environment variable — metrics and
  events alike. This is what lets benchmark results and collector output be joined on the time axis.
  Outside benchmarks use a sentinel such as `prod` so no query needs a special case.
- Rows also carry the server identity and storage tier (`premium-ssd-v1` / `premium-ssd-v2`).
- **JSON Lines is the wire format** for both ingestion paths, so a file replayed later produces rows
  identical to those ingested live. Output is append-only so a run can be replayed and re-analysed.
- Store raw cumulative counters and derive rates at query time; never pre-compute away a counter
  reset, which is real signal.
- **Curate collection by measurement and Profile.** Low-cardinality production signals are enabled
  by default. High-cardinality measurement families require explicit opt-in and bounds. Filter at
  collection, not downstream.

## Testing — `testing/`

Changes to the collector, the ADX schema, or the Bicep must be verifiable against **real Azure
resources**. There is no emulator path: enforced TLS, diagnostic log categories, streaming
ingestion latency and managed-identity permissions only exist on the real platform.

- `testing/verify.py` is the contract. It asserts behaviour, not deployment success, and every
  check states what a failure would mean.
- **A new claim in a README needs a check in `verify.py`.** The documented limitations here
  (no error-log category, the 8.4 redo-log rename, Standard-tier Grafana) are asserted, so they
  stay proven rather than remembered.
- Load and collection must overlap. Sampling an idle server produces a flat series that is
  indistinguishable from a broken collector.
- The test environment is ephemeral and tag-guarded. Tear it down; ADX and Grafana bill while
  idle.

## Repository layout

```
azure-mysql-monitoring/
├── README.md
├── .github/copilot-instructions.md
├── azure-native/            # Layer 1: Azure Monitor / Log Analytics
│   ├── bicep/               # IaC: diagnostic settings, LA workspace, alert rules
│   ├── workbooks/           # Azure Workbook JSON definitions
│   ├── kql/                 # Reusable KQL queries
│   └── alerts/              # Alert rule definitions & thresholds
├── mysql-internal/          # Layer 2: in-server telemetry
│   ├── collector/           # Python collector (jsonl + ADX sinks)
│   └── sql/                 # SHOW GLOBAL STATUS / performance_schema queries
├── adx/                     # Unified store: metrics + log events
│   ├── bicep/               # Cluster, database, identities & roles
│   ├── tables/              # Table DDL, ingestion mappings, materialized views
│   └── policies/            # Streaming, batching, retention, caching
├── grafana/                 # Layer 3: final monitoring view
│   ├── dashboards/          # Dashboard JSON models
│   ├── datasources/         # ADX + Azure Monitor provisioning YAML
│   └── provisioning/        # Providers, folders, deployment wiring
├── testing/                 # Real-Azure test environment + end-to-end verification
│   ├── bicep/               # Disposable test env: MySQL, LAW, ADX, Grafana
│   ├── scripts/             # deploy / bootstrap / workload / teardown
│   └── verify.py            # PASS-FAIL assertions over the whole pipeline
└── benchmark-integration/   # Joins benchmark runs with collector output via RUN_ID
```

Put new files in the directory that matches their layer. Keep each directory's `README.md`
accurate when you add files.
