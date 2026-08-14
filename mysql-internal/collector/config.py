"""Configuration for the MySQL monitoring collector.

Every value comes from an environment variable. Nothing is hardcoded, and the password is
never exposed by ``describe()`` so a config dump can be logged safely.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or self-contradictory."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"environment variable {name} is required and must not be empty. "
            "See mysql-internal/collector/README.md for the full list."
        )
    return value


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


VALID_TIERS = ("premium-ssd-v1", "premium-ssd-v2")


@dataclass(frozen=True)
class Config:
    """Resolved collector configuration."""

    host: str
    user: str
    password: str = field(repr=False)
    database: str
    tier: str
    run_id: str
    port: int = 3306
    ssl_ca: str = ""

    adx_ingest_uri: str = ""
    adx_cluster_uri: str = ""
    adx_database: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        tier = _require("MYSQL_TIER")
        if tier not in VALID_TIERS:
            raise ConfigError(
                f"MYSQL_TIER must be one of {', '.join(VALID_TIERS)}; got {tier!r}. "
                "The tier is stamped on every row, so a typo silently splits a benchmark "
                "comparison into two unrelated series."
            )

        port_raw = _optional("MYSQL_PORT", "3306")
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise ConfigError(f"MYSQL_PORT must be an integer; got {port_raw!r}") from exc

        return cls(
            host=_require("MYSQL_HOST"),
            user=_require("MYSQL_USER"),
            password=_require("MYSQL_PASSWORD"),
            database=_require("MYSQL_DB"),
            tier=tier,
            run_id=_require("RUN_ID"),
            port=port,
            ssl_ca=_optional("MYSQL_SSL_CA"),
            adx_ingest_uri=_optional("ADX_INGEST_URI"),
            adx_cluster_uri=_optional("ADX_CLUSTER_URI"),
            adx_database=_optional("ADX_DATABASE"),
        )

    def require_adx(self) -> None:
        """Validate the ADX-specific settings, only when an ADX sink is actually selected."""
        missing = [
            name
            for name, value in (
                ("ADX_INGEST_URI", self.adx_ingest_uri),
                ("ADX_DATABASE", self.adx_database),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                f"ADX sink selected but {', '.join(missing)} not set. "
                "There is no ADX secret to configure — authentication uses a managed identity "
                "(or your az login when running locally) — but the endpoint must be supplied."
            )

    def describe(self) -> dict[str, str]:
        """Loggable view of the configuration. The password is never included."""
        return {
            "host": self.host,
            "port": str(self.port),
            "user": self.user,
            "password": "***redacted***",
            "database": self.database,
            "tier": self.tier,
            "run_id": self.run_id,
            "ssl_ca": self.ssl_ca or "(system trust store)",
            "adx_ingest_uri": self.adx_ingest_uri or "(unset)",
            "adx_cluster_uri": self.adx_cluster_uri or "(unset)",
            "adx_database": self.adx_database or "(unset)",
        }
