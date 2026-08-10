from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

COLLECTOR = Path(__file__).parents[2] / "mysql-internal" / "collector"
sys.path.insert(0, str(COLLECTOR))

from plan import EnvReference, KeyVaultReference, PlanError, load_plan  # noqa: E402


def write_plan(content: str) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    with handle:
        handle.write(textwrap.dedent(content))
    return Path(handle.name)


BASE = """
version: 1
profiles:
  standard:
    groups:
      global-status: {interval: 10s}
targets:
  - id: orders
    host: orders.mysql.database.azure.com
    database: app
    tier: premium-ssd-v2
    profile: standard
    run_id: {env: RUN_ID}
    credentials:
      username: {env: MYSQL_USER}
      password: {env: MYSQL_PASSWORD}
"""


class CollectionPlanTests(unittest.TestCase):
    def test_loads_multiple_targets_and_builds_jobs(self) -> None:
        path = write_plan(
            BASE
            + """
  - id: sessions
    host: sessions.mysql.database.azure.com
    database: app
    tier: premium-ssd-v1
    profile: standard
    run_id: {env: RUN_ID}
    credentials:
      username: {env: MYSQL_USER}
      password:
        key_vault:
          vault_uri: https://monitoring.vault.azure.net
          secret: sessions-password
"""
        )
        plan = load_plan(path)
        self.assertEqual(len(plan.targets), 2)
        self.assertEqual(len(plan.jobs), 2)
        self.assertIsInstance(plan.targets[0].username, EnvReference)
        self.assertIsInstance(plan.targets[1].password, KeyVaultReference)

    def test_profile_inheritance_overrides_cadence(self) -> None:
        path = write_plan(
            BASE.replace(
                "targets:",
                """
  benchmark:
    extends: standard
    groups:
      global-status: {interval: 5s}
targets:""",
            ).replace("profile: standard", "profile: benchmark")
        )
        plan = load_plan(path)
        self.assertEqual(
            plan.profiles["benchmark"].groups["global-status"].interval.total_seconds(), 5
        )

    def test_rejects_literal_password(self) -> None:
        path = write_plan(BASE.replace("{env: MYSQL_PASSWORD}", "not-a-reference"))
        with self.assertRaisesRegex(PlanError, "literal values are forbidden"):
            load_plan(path)

    def test_rejects_high_cardinality_without_opt_in(self) -> None:
        path = write_plan(
            BASE.replace(
                "global-status: {interval: 10s}",
                "statement-digests: {interval: 1m, top_k: 50}",
            )
        )
        with self.assertRaisesRegex(PlanError, "high-cardinality"):
            load_plan(path)

    def test_accepts_bounded_high_cardinality_group(self) -> None:
        path = write_plan(
            BASE.replace(
                "groups:\n      global-status: {interval: 10s}",
                """allow_high_cardinality: true
    groups:
      global-status: {interval: 10s}
      statement-digests: {interval: 1m, top_k: 50}""",
            )
        )
        plan = load_plan(path)
        self.assertEqual(plan.profiles["standard"].groups["statement-digests"].top_k, 50)

    def test_rejects_interval_below_safety_floor(self) -> None:
        path = write_plan(BASE.replace("interval: 10s", "interval: 1s"))
        with self.assertRaisesRegex(PlanError, "safety floor"):
            load_plan(path)

    def test_rejects_duplicate_target_ids(self) -> None:
        second = BASE.split("targets:\n", 1)[1]
        path = write_plan(BASE + second)
        with self.assertRaisesRegex(PlanError, "duplicate Target ids"):
            load_plan(path)


if __name__ == "__main__":
    unittest.main()

