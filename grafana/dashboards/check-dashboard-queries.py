# check-dashboard-queries.py — run every panel's KQL against the real cluster.
#
# Importing a dashboard only proves the JSON parsed. A panel whose KQL references a column
# that does not exist imports perfectly and then renders an empty graph, which is exactly what
# a healthy idle server also looks like. So each query is extracted and executed here.
#
# Grafana macros and template variables are substituted with plausible values first, because
# ADX rejects them as syntax errors.
#
# Usage:
#   . ./testing/scripts/load-env.ps1
#   python grafana/dashboards/check-dashboard-queries.py

import json
import os
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from azure.identity import AzureCliCredential
    from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
except ImportError:
    sys.exit("Missing dependencies. Run: pip install azure-kusto-data azure-identity")

DASHBOARD_DIR = Path(__file__).parent

# Grafana expands these before the query reaches ADX; we stand in for Grafana.
MACROS = {
    "$__timeFilter(Timestamp)": "Timestamp > ago(30d)",
    "$__timeFrom": "ago(30d)",
    "$__timeTo": "now()",
}


def substitute(query: str, run_id: str, target_id: str) -> str:
    for macro, replacement in MACROS.items():
        query = query.replace(macro, replacement)
    # Multi-value variables are interpolated by Grafana as a quoted, comma-separated list.
    query = query.replace("($run_id)", f"('{run_id}')")
    query = query.replace("($target_id)", f"('{target_id}')")
    query = query.replace("$baseline_target", target_id)
    query = query.replace("$candidate_target", target_id)
    for var in ("$run_id", "$baseline", "$candidate"):
        query = query.replace(var, run_id)
    query = query.replace("$target_id", target_id)
    # Legacy variables may be unused in a given panel.
    query = re.sub(r"\$(host|metric)", "", query)
    return query


def iter_queries(dashboard: dict):
    """Yield (panel_title, refId, query) for panels and template variables alike."""
    for var in dashboard.get("templating", {}).get("list", []):
        query = var.get("query")
        if isinstance(query, dict) and query.get("query"):
            yield f"variable:{var['name']}", "-", query["query"]

    for panel in dashboard.get("panels", []):
        for target in panel.get("targets", []):
            if target.get("query"):
                yield panel.get("title", f"panel {panel.get('id')}"), target.get("refId", "?"), target["query"]


def main() -> int:
    cluster = os.environ.get("ADX_CLUSTER_URI")
    database = os.environ.get("ADX_DATABASE")
    if not cluster or not database:
        sys.exit("ADX_CLUSTER_URI and ADX_DATABASE must be set. Run: . ./testing/scripts/load-env.ps1")

    kcsb = KustoConnectionStringBuilder.with_azure_token_credential(cluster, AzureCliCredential())
    client = KustoClient(kcsb)

    run_id = os.environ.get("RUN_ID", "")
    if not run_id:
        result = client.execute(database, "MysqlTelemetry | where RunId != 'verify-probe' "
                                           "| summarize LastSeen = max(Timestamp) by RunId "
                                          "| order by LastSeen desc | take 1 | project RunId")
        rows = list(result.primary_results[0])
        run_id = rows[0]["RunId"] if rows else "none"
    result = client.execute(
        database,
        f"MysqlTelemetry | where RunId == '{run_id}' "
        "| where TargetId != 'verify-probe' "
        "| summarize LastSeen=max(Timestamp), Measurements=dcount(Measurement), Points=count() by TargetId "
        "| order by Measurements desc, Points desc, LastSeen desc | take 1 | project TargetId",
    )
    target_rows = list(result.primary_results[0])
    target_id = target_rows[0]["TargetId"] if target_rows else "none"
    print(f"Substituting run id: {run_id}; Target: {target_id}\n")

    passed = failed = empty = 0
    for path in sorted(DASHBOARD_DIR.glob("*.json")):
        dashboard = json.loads(path.read_text(encoding="utf-8"))
        print(f"=== {path.name} — {dashboard.get('title')} ===")

        for title, ref, raw in iter_queries(dashboard):
            query = substitute(raw, run_id, target_id).replace("${ADX_DATABASE}", database)
            label = f"  [{ref}] {title}"
            try:
                response = client.execute(database, query)
                count = len(list(response.primary_results[0]))
            except Exception as exc:  # noqa: BLE001 — the message is the whole point
                message = str(exc).split("\n")[0][:180]
                print(f"{label}\n      FAIL {message}")
                failed += 1
                continue

            if count == 0:
                # Not a failure: an error-log panel is legitimately empty on a healthy server.
                print(f"{label}\n      OK (0 rows — query is valid but matched nothing)")
                empty += 1
            else:
                print(f"{label}\n      OK ({count} rows)")
                passed += 1
        print()

    print(f"{passed} returning data, {empty} valid but empty, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
