#!/usr/bin/env python3
"""Apply the committed ADX schema to a live cluster.

Reads the .kql files from adx/tables/ and adx/policies/ and executes them as control
commands. The files are the source of truth; nothing is typed ad hoc into the Kusto UI.

Order matters. Tables must exist before mappings reference them, and streaming ingestion
cannot be enabled on a table that has not been created yet.

    python bootstrap_adx.py                 # apply everything
    python bootstrap_adx.py --dry-run       # print what would run

Authentication uses DefaultAzureCredential, so `az login` is enough locally and a managed
identity works unchanged in CI.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Applied in this order: schema, then the mappings that reference it, then the views that
# read it, then the policies that govern all of the above.
SCHEMA_FILES = [
    REPO_ROOT / "adx" / "tables" / "MysqlMetrics.kql",
    REPO_ROOT / "adx" / "tables" / "MysqlEvents.kql",
    REPO_ROOT / "adx" / "tables" / "MysqlTelemetry.kql",
    REPO_ROOT / "adx" / "tables" / "mappings.kql",
    REPO_ROOT / "adx" / "tables" / "materialized-views.kql",
    REPO_ROOT / "adx" / "tables" / "functions.kql",
    REPO_ROOT / "adx" / "policies" / "policies.kql",
]


def split_commands(text: str) -> list[str]:
    """Split a .kql file into individual control commands.

    A command starts at a line beginning with '.' in column zero. Everything until the next
    such line belongs to it, which keeps multi-line ``` blocks and KQL bodies intact.
    Comment-only lines between commands are discarded.
    """
    lines = text.splitlines()
    commands: list[list[str]] = []
    current: list[str] | None = None

    for line in lines:
        if re.match(r"^\.\w", line):
            if current:
                commands.append(current)
            current = [line]
        elif current is not None:
            current.append(line)

    if current:
        commands.append(current)

    result = []
    for block in commands:
        # Trailing comment lines belong to the *next* command, not this one.
        while block and (not block[-1].strip() or block[-1].lstrip().startswith("//")):
            block.pop()
        text_block = "\n".join(block).strip()
        if text_block:
            result.append(text_block)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply the ADX schema from committed .kql files")
    parser.add_argument("--cluster-uri", default=os.environ.get("ADX_CLUSTER_URI", ""))
    parser.add_argument("--database", default=os.environ.get("ADX_DATABASE", ""))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.cluster_uri or not args.database:
        print(
            "ADX_CLUSTER_URI and ADX_DATABASE must be set (or passed as flags).\n"
            "Run: . ./scripts/load-env.ps1",
            file=sys.stderr,
        )
        return 2

    print(f"Cluster  : {args.cluster_uri}")
    print(f"Database : {args.database}")

    if args.dry_run:
        for path in SCHEMA_FILES:
            commands = split_commands(path.read_text(encoding="utf-8"))
            print(f"\n--- {path.relative_to(REPO_ROOT)} ({len(commands)} commands) ---")
            for cmd in commands:
                print(f"  {cmd.splitlines()[0][:100]}")
        return 0

    try:
        from azure.identity import DefaultAzureCredential
        from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
    except ImportError:
        print(
            "azure-kusto-data is required. Install the ADX extra:\n"
            "  pip install -r ../mysql-internal/collector/requirements-adx.txt",
            file=sys.stderr,
        )
        return 2

    credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)
    kcsb = KustoConnectionStringBuilder.with_azure_token_credential(args.cluster_uri, credential)
    client = KustoClient(kcsb)

    applied = failed = 0
    for path in SCHEMA_FILES:
        commands = split_commands(path.read_text(encoding="utf-8"))
        print(f"\n--- {path.relative_to(REPO_ROOT)} ({len(commands)} commands) ---")

        for cmd in commands:
            label = cmd.splitlines()[0][:90]
            try:
                client.execute_mgmt(args.database, cmd)
                print(f"  OK   {label}")
                applied += 1
            except Exception as exc:  # noqa: BLE001 - report and continue; one bad policy
                # should not block the rest of the schema
                print(f"  FAIL {label}\n       {exc}", file=sys.stderr)
                failed += 1

    print(f"\n{applied} commands applied, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
