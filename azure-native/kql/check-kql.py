# check-kql.py — run every committed .kql file against the real Log Analytics workspace.
#
# A KQL file that references a column Azure does not emit fails silently in a workbook: the
# query errors in a tile most people never expand, or returns nothing and reads as "quiet".
# Running them here turns that into a build-time failure instead.
#
# Only the active query in each file is executed. Commented variants are documentation, and
# are reported as skipped rather than silently ignored.
#
# Usage:
#   . ./testing/scripts/load-env.ps1
#   python azure-native/kql/check-kql.py

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

KQL_DIR = Path(__file__).parent


def strip_comments(text: str) -> str:
    """Drop comment lines, keeping only the executable query.

    The result is flattened to a single line. On Windows `az` is a batch file, so the CLI is
    invoked through cmd.exe, which truncates an argument at its first newline — a multi-line
    query arrives as just its `let` statements. Flattening is safe only because the `//`
    comments have already been removed; otherwise a comment would swallow the rest.
    """
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("//")]
    return " ".join(" ".join(lines).split())


def run_query(workspace: str, query: str) -> tuple[bool, str]:
    env = {**os.environ, "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "yes_without_prompt"}
    # Resolved explicitly rather than run through the shell: on Windows `az` is a .cmd, and
    # shell=True would flatten this multi-line query into a single line, leaving the CLI with
    # nothing but the `let` statements. That surfaces as "No tabular expression statement
    # found", which reads like a bug in the query rather than in how it was invoked.
    az = shutil.which("az")
    if not az:
        return False, "az CLI not found on PATH"

    proc = subprocess.run(        [az, "monitor", "log-analytics", "query", "-w", workspace,
         "--analytics-query", query, "-o", "json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    if proc.returncode != 0:
        # Azure CLI errors are multi-line JSON; the last line is usually just a brace, so
        # collapse the whole thing and keep the informative part.
        raw = (proc.stderr or proc.stdout).strip()
        return False, " ".join(raw.split())[:400] or "unknown error"
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return False, "response was not JSON"
    return True, f"{len(rows)} rows"


def main() -> int:
    workspace = os.environ.get("LOG_ANALYTICS_WORKSPACE_ID")
    if not workspace:
        sys.exit("LOG_ANALYTICS_WORKSPACE_ID must be set. Run: . ./testing/scripts/load-env.ps1")

    failed = 0
    for path in sorted(KQL_DIR.glob("*.kql")):
        query = strip_comments(path.read_text(encoding="utf-8"))
        if not query:
            print(f"{path.name}\n    SKIP (no executable query)")
            continue

        ok, detail = run_query(workspace, query)
        status = "OK" if ok else "FAIL"
        print(f"{path.name}\n    {status} {detail}")
        if not ok:
            failed += 1

    print(f"\n{failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
