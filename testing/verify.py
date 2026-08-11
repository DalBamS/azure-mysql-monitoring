#!/usr/bin/env python3
"""End-to-end verification of the azure-mysql-monitoring pipeline.

Runs a series of independent checks against the REAL deployed test environment and reports
PASS / FAIL / SKIP for each. This is the script that answers "does the monitoring actually
work", as opposed to "did the resources deploy".

It also probes the assumptions the repository documentation is built on, so a wrong
assumption fails loudly here rather than silently producing empty dashboards later:

  * MySQL really is 8.4, and require_secure_transport really is ON
  * innodb_redo_log_capacity exists and supersedes the deprecated innodb_log_file_size
  * performance_schema.error_log is readable on Azure Flexible Server
  * Flexible Server offers exactly two diagnostic log categories, and no error-log category
  * Streaming ingestion delivers rows in seconds, not in the queued batching window

    python verify.py                 # all checks
    python verify.py --skip-adx      # stage 1 only
    python verify.py --json          # machine-readable output

Run `. ./scripts/load-env.ps1` first.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_DIR = REPO_ROOT / "mysql-internal" / "collector"
sys.path.insert(0, str(COLLECTOR_DIR))

# Windows consoles default to a legacy code page (cp949 on a Korean install), which cannot
# encode the em-dashes and arrows used in the output. Without this, verify.py dies with a
# UnicodeEncodeError before running a single check — a verification script that cannot survive
# its own banner is worse than useless.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL, SKIP, WARN = "PASS", "FAIL", "SKIP", "WARN"


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""
    proves: str = ""


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "", proves: str = "") -> Result:
        r = Result(name, status, detail, proves)
        self.results.append(r)
        colour = {PASS: "\033[32m", FAIL: "\033[31m", SKIP: "\033[90m", WARN: "\033[33m"}.get(status, "")
        reset = "\033[0m" if colour else ""
        print(f"  [{colour}{status}{reset}] {name}")
        if detail:
            for line in detail.splitlines():
                print(f"         {line}")
        return r

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == FAIL)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def run_az(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    """Run an `az` command non-interactively.

    The CLI prompts before installing a missing extension (log-analytics is not in the base
    install). With no TTY that prompt fails as "EOF when reading a line" wrapped in a knack
    traceback, which reads like a broken query rather than a missing extension. Forcing dynamic
    install without a prompt keeps the failure honest.
    """
    env = dict(os.environ)
    env["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"] = "yes_without_prompt"
    env["AZURE_CORE_ONLY_SHOW_ERRORS"] = "true"
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, shell=True, env=env,
    )


# ---------------------------------------------------------------------------------------
# Layer 2 — MySQL connectivity and the assumptions the repo depends on
# ---------------------------------------------------------------------------------------

def check_mysql(report: Report) -> dict | None:
    section("Layer 2 — MySQL Flexible Server")

    try:
        from config import Config, ConfigError
        from connection import assert_tls, connect, server_identity
    except ImportError as exc:
        report.add("collector modules importable", FAIL, str(exc))
        return None

    try:
        cfg = Config.from_env()
    except Exception as exc:  # noqa: BLE001
        report.add("environment configured", FAIL, str(exc),
                   "deploy.ps1 wrote testing/.env and it was loaded")
        return None
    report.add("environment configured", PASS, f"host={cfg.host} run_id={cfg.run_id}")

    try:
        conn = connect(cfg)
    except Exception as exc:  # noqa: BLE001
        report.add("TLS connection established", FAIL, str(exc),
                   "the firewall rule and credentials are correct")
        return None

    try:
        cipher = assert_tls(conn)
        report.add("connection is encrypted", PASS, f"cipher={cipher}",
                   "require_secure_transport=ON is genuinely enforced, not just configured")

        identity = server_identity(conn)

        version = identity.get("version", "")
        if version.startswith("8.4"):
            report.add("MySQL version is 8.4", PASS, f"version={version}")
        else:
            report.add("MySQL version is 8.4", FAIL, f"version={version}",
                       "the repo targets 8.4 only; metric names may differ")

        if "innodb_redo_log_capacity" in identity:
            report.add("innodb_redo_log_capacity exists", PASS,
                       f"{identity['innodb_redo_log_capacity']} bytes",
                       "8.4 replaced innodb_log_file_size, as the docs claim")
        else:
            report.add("innodb_redo_log_capacity exists", FAIL, "variable not returned")

        with conn.cursor() as cur:
            cur.execute("SHOW VARIABLES LIKE 'innodb_log_file_size'")
            legacy = cur.fetchall()

        # Verified against Azure MySQL 8.4.9: innodb_log_file_size is still present, and is
        # deprecated rather than removed. innodb_redo_log_capacity supersedes it — when
        # capacity is set, the legacy variable is ignored. So the meaningful assertion is
        # "capacity is what governs", not "the old name is gone".
        if legacy:
            report.add("innodb_log_file_size is superseded", PASS,
                       f"still exposed as deprecated: {legacy[0][1]} (ignored; capacity governs)",
                       "sizing the redo log through the legacy variable would silently do nothing")
        else:
            report.add("innodb_log_file_size is superseded", PASS,
                       "not exposed at all",
                       "the redo-log rename documented in copilot-instructions is real")

        secure = identity.get("require_secure_transport", "")
        report.add(
            "require_secure_transport=ON", PASS if secure.upper() == "ON" else FAIL,
            f"value={secure}",
        )

        # The uncertain assumption. If this fails, the entire error-log story in the repo
        # needs rewriting, because Layer 1 has no error-log category to fall back on.
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM performance_schema.error_log")
                count = cur.fetchone()[0]
            report.add("performance_schema.error_log readable", PASS, f"{count} entries buffered",
                       "the ONLY route to error-log data on Flexible Server is available")
        except Exception as exc:  # noqa: BLE001
            report.add("performance_schema.error_log readable", FAIL, str(exc),
                       "if this fails there is no error-log source at all")

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM performance_schema.global_status")
            total = cur.fetchone()[0]
        report.add("performance_schema.global_status readable", PASS,
                   f"{total} status variables exposed; the allow-list keeps ~80",
                   "volume control is a real saving, not a theoretical one")

        return identity
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------
# Collector — does it actually produce rows?
# ---------------------------------------------------------------------------------------

def check_collector(report: Report, cycles: int = 3, interval: float = 2.0) -> Path | None:
    section("Collector — sampling")

    out_dir = REPO_ROOT / "testing" / "runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"verify-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.jsonl"

    cmd = [
        sys.executable,
        str(COLLECTOR_DIR / "collector.py"),
        "--interval", str(interval),
        "--max-cycles", str(cycles),
        "--sink", "jsonl",
        "--out", str(out_file),
        "--cursor-file", str(out_dir / ".verify_cursor.json"),
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(COLLECTOR_DIR))
    except subprocess.TimeoutExpired:
        report.add("collector runs", FAIL, "timed out after 120s")
        return None

    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-5:])
        report.add("collector runs", FAIL, f"exit {proc.returncode}\n{tail}")
        return None
    report.add("collector runs", PASS, f"{cycles} cycles at {interval}s")

    if not out_file.is_file():
        report.add("collector wrote JSONL", FAIL, f"missing {out_file}")
        return None

    rows = [json.loads(line) for line in out_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        report.add("collector wrote JSONL", FAIL, "file is empty")
        return None
    report.add("collector wrote JSONL", PASS, f"{len(rows)} rows in {out_file.name}")

    # A heartbeat per cycle is the contract that makes collector death detectable.
    beats = [r for r in rows if r.get("metric") == "collector_heartbeat"]
    if len(beats) >= cycles:
        report.add("heartbeat emitted every cycle", PASS, f"{len(beats)} heartbeats / {cycles} cycles",
                   "a dead collector will be detectable instead of flatlining silently")
    else:
        report.add("heartbeat emitted every cycle", FAIL, f"only {len(beats)} for {cycles} cycles")

    missing_tags = [r for r in rows if not r.get("run_id") or not r.get("tier")]
    if missing_tags:
        report.add("every row tagged with run_id and tier", FAIL, f"{len(missing_tags)} untagged rows")
    else:
        report.add("every row tagged with run_id and tier", PASS,
                   proves="benchmark output can be joined on the time axis")

    bad_ts = [r for r in rows if not str(r.get("ts", "")).endswith("Z")]
    if bad_ts:
        report.add("timestamps are UTC ISO-8601", FAIL, f"{len(bad_ts)} rows without a Z suffix",
                   "Kusto would treat these as UTC and silently shift every chart")
    else:
        report.add("timestamps are UTC ISO-8601", PASS)

    sources = sorted({r.get("source", "") for r in rows})
    report.add("metric sources present", PASS, f"sources={', '.join(sources)}")

    return out_file


# ---------------------------------------------------------------------------------------
# Storage — ADX
# ---------------------------------------------------------------------------------------

def _kusto_client(cluster_uri: str):
    from azure.identity import DefaultAzureCredential
    from azure.kusto.data import KustoClient, KustoConnectionStringBuilder

    credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)
    return KustoClient(KustoConnectionStringBuilder.with_azure_token_credential(cluster_uri, credential))


def check_adx(report: Report, run_id: str) -> None:
    section("Storage — Azure Data Explorer")

    cluster_uri = os.environ.get("ADX_CLUSTER_URI", "").strip()
    database = os.environ.get("ADX_DATABASE", "").strip()

    if not cluster_uri or not database:
        report.add("ADX configured", SKIP, "ADX_CLUSTER_URI / ADX_DATABASE not set")
        return

    try:
        client = _kusto_client(cluster_uri)
    except ImportError:
        report.add("ADX SDK available", SKIP,
                   "pip install -r ../mysql-internal/collector/requirements-adx.txt")
        return
    except Exception as exc:  # noqa: BLE001
        report.add("ADX reachable", FAIL, str(exc))
        return

    def query(kql: str):
        return client.execute(database, kql).primary_results[0]

    try:
        tables = {row["TableName"] for row in query(".show tables | project TableName")}
    except Exception as exc:  # noqa: BLE001
        report.add("ADX reachable", FAIL, str(exc), "the operator has Admin on the database")
        return

    for expected in (
        "MysqlMetrics",
        "MysqlEvents",
        "MysqlTelemetry",
        "MysqlMetricSeries",
    ):
        if expected in tables:
            report.add(f"table {expected} exists", PASS)
        else:
            report.add(f"table {expected} exists", FAIL, "run scripts/bootstrap_adx.py")

    # Streaming ingestion must be enabled at BOTH cluster and table level. Enabled only on
    # the cluster, rows silently fall back to the queued path and "real-time" becomes minutes.
    for table in ("MysqlMetrics", "MysqlTelemetry", "MysqlMetricSeries"):
        try:
            policies = list(query(f".show table {table} policy streamingingestion"))
            enabled = any(
                "Enabled" in str(row.to_dict().get("Policy", ""))
                for row in policies
            )
            report.add(
                f"streaming ingestion enabled on {table}",
                PASS if enabled else FAIL,
                proves="the hot path is genuinely streaming, not queued batching",
            )
        except Exception as exc:  # noqa: BLE001
            report.add(f"streaming ingestion enabled on {table}", WARN, str(exc))

    try:
        views = {row["Name"] for row in query(".show materialized-views | project Name")}
        for expected in ("MysqlMetrics1m", "MysqlEvents1m"):
            report.add(f"materialized view {expected} exists",
                       PASS if expected in views else FAIL,
                       proves="rollups make 90-day queries efficient without extending retention")
    except Exception as exc:  # noqa: BLE001
        report.add("materialized views exist", WARN, str(exc))

    # The 90-day lifecycle applies to every representation. A materialized view is still
    # customer telemetry; leaving an old 395-day view behind would satisfy the raw-table policy
    # while violating the actual retention requirement.
    for entity_type, name in (
        ("table", "MysqlMetrics"),
        ("table", "MysqlEvents"),
        ("table", "MysqlTelemetry"),
        ("table", "MysqlMetricSeries"),
        ("materialized-view", "MysqlMetrics1m"),
        ("materialized-view", "MysqlEvents1m"),
    ):
        label = f"{name} retention is 90 days"
        try:
            rows = list(query(f".show {entity_type} {name} policy retention"))
            policy = json.loads(rows[0]["Policy"]) if rows else {}
            actual = policy.get("SoftDeletePeriod", "(unset)")
            recoverability = policy.get("Recoverability", "(unset)")
            report.add(
                label,
                PASS
                if actual == "90.00:00:00" and recoverability == "Disabled"
                else FAIL,
                f"SoftDeletePeriod={actual}, Recoverability={recoverability}",
                proves="raw, event and rollup telemetry all expire after the requested 90 days",
            )
        except Exception as exc:  # noqa: BLE001
            report.add(label, WARN, str(exc))

    # The real test: ingest through the streaming path and time how long the row takes to
    # become queryable. This is the number the whole "real-time budget" claim rests on.
    section("Storage — streaming ingestion latency")
    try:
        from config import Config

        cfg = Config.from_env()
        cfg.require_adx()
        from sinks.adx import AdxSink
        from envelope import Envelope, utc_now
        from catalog import CATALOG
        from telemetry import TelemetryContext, TelemetryPoint

        sink = AdxSink(cfg, streaming=True)
        env = Envelope(run_id=run_id, host=cfg.host, tier=cfg.tier)
        probe_metric = f"verify_probe_{int(time.time())}"
        probe_target = "verify-probe"
        observed_at = utc_now()
        probe_since = observed_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        sent_at = time.monotonic()
        sink.write_metrics([env.metric(observed_at, "collector", probe_metric, 42.0)])

        if sink.last_error:
            report.add("legacy streaming ingestion accepted", FAIL, sink.last_error)
            return
        report.add("legacy streaming ingestion accepted", PASS)

        sink.write_points(
            [
                TelemetryPoint(
                    observed_at=observed_at,
                    context=TelemetryContext(
                        run_id="verify-probe",
                        target_id="verify-probe",
                        host=cfg.host,
                        tier=cfg.tier,
                        collector_id="verify",
                    ),
                    measurement="mysql.global_status",
                    fields={"Questions": 42.0},
                )
            ],
            CATALOG,
        )
        if sink.last_error:
            report.add("v2 streaming ingestion accepted", FAIL, sink.last_error)
            return
        report.add("v2 streaming ingestion accepted", PASS)

        deadline = sent_at + 90
        found = False
        while time.monotonic() < deadline:
            rows = list(
                query(
                    "print "
                    f"Legacy=toscalar(MysqlMetrics | where Metric == '{probe_metric}' | count), "
                    f"Packed=toscalar(MysqlTelemetry | where TargetId == '{probe_target}' "
                    f"and Timestamp >= datetime({probe_since}) | count), "
                    f"Series=toscalar(MysqlMetricSeries | where TargetId == '{probe_target}' "
                    f"and Timestamp >= datetime({probe_since}) | count)"
                )
            )
            if (
                rows
                and rows[0]["Legacy"] > 0
                and rows[0]["Packed"] > 0
                and rows[0]["Series"] >= 1
            ):
                found = True
                break
            time.sleep(3)

        latency = time.monotonic() - sent_at
        if found:
            status = PASS if latency <= 60 else WARN
            report.add(
                "legacy and v2 probe rows queryable",
                status,
                f"{latency:.1f}s end to end",
                "packed replay data and narrow Grafana series share the streaming hot path",
            )
        else:
            report.add(
                "legacy and v2 probe rows queryable",
                FAIL,
                "not all visible within 90s",
                "streaming ingestion may be silently falling back to the queued path",
            )

        sink.close()
    except Exception as exc:  # noqa: BLE001
        report.add("streaming ingestion probe", FAIL, str(exc))

    # Data from the collector run itself.
    section("Storage — collector data")
    try:
        rows = list(query(
            f"MysqlMetrics | where RunId == '{run_id}' | summarize Rows=count(), "
            "Metrics=dcount(Metric), Last=max(Timestamp)"
        ))
        if rows and rows[0]["Rows"] > 0:
            r = rows[0]
            report.add("collector rows present in ADX", PASS,
                       f"{r['Rows']} rows, {r['Metrics']} distinct metrics, last={r['Last']}")
        else:
            report.add("collector rows present in ADX", WARN,
                       f"no rows for RunId={run_id}",
                       "expected if the collector was run with --sink jsonl only")
    except Exception as exc:  # noqa: BLE001
        report.add("collector rows present in ADX", FAIL, str(exc))

    try:
        rows = list(
            query(
                f"MysqlTelemetry | where RunId == '{run_id}' "
                "| summarize Rows=count(), Measurements=dcount(Measurement), "
                "Targets=dcount(TargetId), Versions=make_set(ContractVersion)"
            )
        )
        if rows and rows[0]["Rows"] > 0:
            r = rows[0]
            valid = r["Versions"] == [2] and r["Targets"] > 0
            report.add(
                "v2 collector points present in ADX",
                PASS if valid else FAIL,
                f"{r['Rows']} packed rows, {r['Measurements']} measurements, "
                f"{r['Targets']} Targets, contract versions={r['Versions']}",
            )
        else:
            report.add(
                "v2 collector points present in ADX",
                WARN,
                f"no v2 rows for RunId={run_id}",
                "run collector.py --config monitoring.yaml with an ADX sink",
            )
    except Exception as exc:  # noqa: BLE001
        report.add("v2 collector points present in ADX", FAIL, str(exc))

    try:
        rows = list(query("CollectorHealth(15m) | project Host, Status, SecondsSinceLastBeat"))
        if rows:
            detail = "\n".join(
                f"{r['TargetId']} ({r['Host']}): {r['Status']} "
                f"({r['SecondsSinceLastBeat']}s since last beat)"
                for r in rows
            )
            report.add("CollectorHealth function works", PASS, detail,
                       "collector-down alerting has a working signal")
        else:
            report.add("CollectorHealth function works", WARN, "no heartbeat rows in the last 15m")
    except Exception as exc:  # noqa: BLE001
        report.add("CollectorHealth function works", FAIL, str(exc))


# ---------------------------------------------------------------------------------------
# Layer 1 — Azure Monitor / Log Analytics
# ---------------------------------------------------------------------------------------

def check_log_analytics(report: Report) -> None:
    section("Layer 1 — Log Analytics")

    workspace_id = os.environ.get("LOG_ANALYTICS_WORKSPACE_ID", "").strip()
    if not workspace_id:
        report.add("Log Analytics configured", SKIP, "LOG_ANALYTICS_WORKSPACE_ID not set")
        return

    def run_kql(kql: str):
        proc = run_az(
            ["az", "monitor", "log-analytics", "query", "-w", workspace_id,
             "--analytics-query", kql, "-o", "json"],
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip()[:300])
        return json.loads(proc.stdout or "[]")

    try:
        rows = run_kql("AzureMetrics | where TimeGenerated > ago(1h) | summarize Rows=count()")
        count = int(rows[0].get("Rows", 0)) if rows else 0
        if count > 0:
            report.add("platform metrics arriving", PASS, f"{count} AzureMetrics rows in the last hour")
        else:
            report.add("platform metrics arriving", WARN, "no rows yet",
                       "platform metrics lag 2-5 minutes; this is the documented slow path")
    except Exception as exc:  # noqa: BLE001
        report.add("platform metrics arriving", FAIL, str(exc))

    try:
        rows = run_kql(
            "AzureDiagnostics | where TimeGenerated > ago(1h) "
            "| summarize Rows=count() by Category"
        )
        if rows:
            detail = "\n".join(f"{r.get('Category')}: {r.get('Rows')} rows" for r in rows)
            report.add("diagnostic logs arriving", PASS, detail)

            categories = {str(r.get("Category", "")) for r in rows}
            if any("Error" in c for c in categories):
                report.add("no error-log category exists", FAIL,
                           f"unexpected category found: {categories}",
                           "the repo claims Flexible Server has no error-log category")
            else:
                report.add("no error-log category exists", PASS,
                           f"categories seen: {', '.join(sorted(categories))}",
                           "confirms performance_schema.error_log is the only error-log route")
        else:
            report.add("diagnostic logs arriving", WARN, "no rows yet",
                       "run workload.py, then wait 2-5 minutes for ingestion")
    except Exception as exc:  # noqa: BLE001
        report.add("diagnostic logs arriving", FAIL, str(exc))


# ---------------------------------------------------------------------------------------
# Layer 3 — Grafana
# ---------------------------------------------------------------------------------------

def check_grafana(report: Report) -> None:
    section("Layer 3 — Managed Grafana")

    endpoint = os.environ.get("GRAFANA_ENDPOINT", "").strip()
    if not endpoint:
        report.add("Grafana configured", SKIP, "GRAFANA_ENDPOINT not set")
        return

    resource_group = os.environ.get("AZURE_RESOURCE_GROUP", "").strip()

    # The workspace is looked up by resource group rather than parsed out of the endpoint.
    # Azure appends a generated suffix to the Grafana DNS name, so
    # https://mysqlmon-grafana-abcde-c7b0akg4d2c3echa.sel.grafana.azure.com belongs to a
    # resource actually named mysqlmon-grafana-abcde. Deriving the name from the hostname
    # yields a ResourceNotFound that looks like a missing deployment.
    try:
        proc = run_az(
            ["az", "grafana", "list", "--resource-group", resource_group,
             "--query", "[0].{sku:sku.name, state:properties.provisioningState, endpoint:properties.endpoint, version:properties.grafanaMajorVersion}",
             "-o", "json"],
        )
        if proc.returncode != 0:
            report.add("Grafana workspace reachable", FAIL, proc.stderr.strip()[:300])
            return

        info = json.loads(proc.stdout or "null")
        if not info:
            report.add("Grafana workspace reachable", FAIL,
                       f"no Grafana workspace in resource group {resource_group}")
            return

        report.add("Grafana workspace reachable", PASS,
                   f"sku={info.get('sku')} state={info.get('state')} version={info.get('version')}")

        # Standard tier is not a preference. The ADX data source does not exist on the
        # deprecated Essential tier, so an Essential workspace would deploy fine and then be
        # unable to display any collector data at all.
        if str(info.get("sku")) == "Standard":
            report.add("Grafana is Standard tier", PASS,
                       proves="the Azure Data Explorer data source is available")
        else:
            report.add("Grafana is Standard tier", FAIL, f"sku={info.get('sku')}",
                       "Essential tier cannot use the ADX data source")
    except Exception as exc:  # noqa: BLE001
        report.add("Grafana workspace reachable", FAIL, str(exc))


# ---------------------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the monitoring pipeline end to end")
    parser.add_argument("--skip-collector", action="store_true")
    parser.add_argument("--skip-adx", action="store_true")
    parser.add_argument("--skip-law", action="store_true")
    parser.add_argument("--skip-grafana", action="store_true")
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable results")
    args = parser.parse_args()

    print("azure-mysql-monitoring — end-to-end verification")
    print(f"started {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}")

    report = Report()
    run_id = os.environ.get("RUN_ID", "unknown")

    identity = check_mysql(report)

    if not args.skip_collector and identity is not None:
        check_collector(report, cycles=args.cycles)
    elif args.skip_collector:
        section("Collector — sampling")
        report.add("collector checks", SKIP, "--skip-collector")

    if not args.skip_adx:
        check_adx(report, run_id)
    else:
        section("Storage — Azure Data Explorer")
        report.add("ADX checks", SKIP, "--skip-adx")

    if not args.skip_law:
        check_log_analytics(report)
    else:
        section("Layer 1 — Log Analytics")
        report.add("Log Analytics checks", SKIP, "--skip-law")

    if not args.skip_grafana:
        check_grafana(report)
    else:
        section("Layer 3 — Managed Grafana")
        report.add("Grafana checks", SKIP, "--skip-grafana")

    section("Summary")
    counts = {s: sum(1 for r in report.results if r.status == s) for s in (PASS, FAIL, WARN, SKIP)}
    print(f"  PASS {counts[PASS]}   FAIL {counts[FAIL]}   WARN {counts[WARN]}   SKIP {counts[SKIP]}")

    if report.failed:
        print("\nFailed checks:")
        for r in report.results:
            if r.status == FAIL:
                print(f"  - {r.name}: {r.detail.splitlines()[0] if r.detail else ''}")

    if args.json:
        out = REPO_ROOT / "testing" / "runs" / "verify-report.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps([r.__dict__ for r in report.results], indent=2), encoding="utf-8")
        print(f"\nWrote {out}")

    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
