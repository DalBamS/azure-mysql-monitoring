# Copilot Instructions — azure-mysql-monitoring

These rules apply to **every** prompt, suggestion, and code change in this repository.
Read them before proposing anything.

## Project purpose

Monitoring for **Azure Database for MySQL Flexible Server**. Two use cases share one codebase:

1. **Benchmark runs** — comparing **Premium SSD v1 vs Premium SSD v2** storage.
2. **Ongoing production monitoring** for gaming customers.

## Target platform

- Azure Database for MySQL **Flexible Server** only (no Single Server, no self-managed MySQL).
- **MySQL 8.4 only.** Do not write code, SQL, or docs that target 5.7, 8.0, or 9.x.
  If a feature differs across versions, implement the 8.4 behaviour and say so in a comment.

### MySQL 8.4 specifics that must be respected

- `caching_sha2_password` is the **default authentication plugin**.
  `mysql_native_password` is **disabled by default** — never assume it, never suggest
  falling back to it, and never generate `CREATE USER ... IDENTIFIED WITH mysql_native_password`.
- `innodb_redo_log_capacity` **replaces** `innodb_log_file_size` / `innodb_log_files_in_group`.
  Use the new variable in all queries, tuning docs, and dashboards.
- Azure enforces TLS: `require_secure_transport=ON`. **All clients must connect with SSL.**
  Every connection helper must set SSL options explicitly (e.g. `ssl_disabled=False`,
  CA bundle where required). Never generate a connection that disables TLS.
- Prefer `performance_schema` over deprecated/removed status sources.

## Architecture — two collection layers, one view layer

### Layer 1 — `azure-native/`

Azure-platform telemetry:

- Azure Monitor platform metrics.
- Diagnostic settings shipping logs/metrics to **Log Analytics**.
- **Azure Workbooks** for dashboards.
- **KQL** queries.
- Alert rules.
- All infrastructure is defined as **Bicep IaC** — no portal click-ops, no ARM JSON authored by hand,
  no Terraform.

### Layer 2 — `mysql-internal/`

A **Python collector** that polls the server directly:

- `SHOW GLOBAL STATUS`
- `performance_schema` tables

**Layer 2 is the primary data source during benchmark runs.** Premium SSD v2 servers are in
**preview**, so Azure platform metrics and diagnostic logs may be incomplete or missing for them.
When the two layers disagree during a benchmark, trust Layer 2 and note the discrepancy.

### Layer 3 — `grafana/`

**Grafana is the final monitoring view** — the single surface where Layer 1 and Layer 2 are shown
on one time axis.

- Presentation only. Grafana never collects data; it reads what Layers 1 and 2 already produce.
- Two data sources: **Azure Monitor** (Layer 1, reusing the KQL in `azure-native/kql/`) and
  **MySQL** (Layer 2, reading the collector's metrics table).
- Dashboards are **JSON committed to this repo and provisioned**, never UI-only edits.
- **`$run_id` is a template variable** on benchmark dashboards, so a v1 run and a v2 run can be
  compared without editing panels.
- Grafana's MySQL driver supports `caching_sha2_password` (the 8.4 default) — never suggest
  switching the user to `mysql_native_password`. The data source must use **TLS `require`**, since
  Azure enforces `require_secure_transport=ON`.
- **Do not point Grafana at `SHOW GLOBAL STATUS` directly** — a live `SHOW` returns an
  instantaneous snapshot that cannot be graphed. Grafana reads the collector's persisted rows,
  which means the collector needs a **MySQL sink** with a `ts` (UTC) + `run_id` schema.
- Set the MySQL data source `timezone` to **UTC** to match stored timestamps; a mismatch shifts
  every panel and silently invalidates a v1 vs v2 comparison.
- `azure-native/workbooks/` remains the Azure-native, portal-side view. Grafana is the primary
  operator-facing dashboard.

## Security — non-negotiable

- **Never hardcode credentials**, hostnames, or connection strings. Not in code, not in
  Bicep parameter defaults, not in READMEs, not in examples, not in tests.
- All connection info comes from **environment variables**:
  - `MYSQL_HOST`
  - `MYSQL_USER`
  - `MYSQL_PASSWORD`
  - `MYSQL_DB`
- **Document these environment variables in every README** that describes runnable code.
- Never log the value of `MYSQL_PASSWORD`. Redact it if a config dump is printed.
- Do not commit `.env` files, `.pem` keys, or Azure credentials.

## Python conventions

- **Python 3.11+**.
- Allowed runtime dependencies: **`mysql-connector-python`** (or **`PyMySQL`**) — nothing else.
- **No ORM** (no SQLAlchemy, no Django ORM, no Peewee). Write plain SQL.
- Prefer the standard library for everything else (`os`, `json`, `csv`, `datetime`, `logging`,
  `argparse`, `sqlite3`).
- Use parameterised queries; never build SQL by string concatenation with user input.

## Data conventions

- **All timestamps are UTC, ISO-8601** (e.g. `2026-08-10T00:53:04Z`).
  Never emit local time, never emit naive datetimes.
- **Every metric row is tagged with `RUN_ID`**, read from the `RUN_ID` environment variable.
  This is what lets benchmark results and collector output be joined on the time axis.
- Metric output should be append-only and machine-readable (CSV/JSON Lines) so a run can be
  replayed and re-analysed.

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
│   ├── collector/           # Python collector
│   └── sql/                 # SHOW GLOBAL STATUS / performance_schema queries
├── grafana/                 # Layer 3: final monitoring view
│   ├── dashboards/          # Dashboard JSON models
│   ├── datasources/         # Azure Monitor + MySQL provisioning YAML
│   └── provisioning/        # Providers, folders, deployment wiring
└── benchmark-integration/   # Joins benchmark runs with collector output via RUN_ID
```

Put new files in the directory that matches their layer. Keep each directory's `README.md`
accurate when you add files.
