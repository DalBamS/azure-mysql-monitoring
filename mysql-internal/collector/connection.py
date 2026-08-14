"""TLS-enforced MySQL connection factory.

Azure Database for MySQL Flexible Server sets ``require_secure_transport=ON``, so an
unencrypted connection is rejected by the server. This module never offers a way to disable
TLS: the only choice is whether the server certificate is verified against a supplied CA
bundle (``MYSQL_SSL_CA``) or against the driver's default trust.

MySQL 8.4 uses ``caching_sha2_password`` as the default authentication plugin and
``mysql_native_password`` is disabled, so the driver must support the SHA-2 exchange.
``mysql-connector-python`` does.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import mysql.connector

from config import Config

log = logging.getLogger(__name__)

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"

# Statements are read from ../sql/ rather than embedded, so the queries stay reviewable on
# their own and the collector never becomes the source of truth for SQL.
_STATEMENT_CACHE: dict[str, str] = {}


def load_sql(name: str) -> str:
    """Read a statement from ../sql/, stripping comment lines and the trailing semicolon."""
    if name not in _STATEMENT_CACHE:
        raw = (SQL_DIR / name).read_text(encoding="utf-8")
        body = "\n".join(
            line for line in raw.splitlines() if not line.lstrip().startswith("--")
        ).strip()
        _STATEMENT_CACHE[name] = body.rstrip(";")
    return _STATEMENT_CACHE[name]


def connect(cfg: Config) -> Any:
    """Open a TLS-encrypted connection to Flexible Server."""
    params: dict[str, Any] = {
        "host": cfg.host,
        "port": cfg.port,
        "user": cfg.user,
        "password": cfg.password,
        "database": cfg.database,
        # TLS is mandatory. ssl_disabled is pinned to False and is never configurable.
        "ssl_disabled": False,
        "connection_timeout": 10,
        "use_pure": True,
        "autocommit": True,
        # The collector must never mutate server state; it only reads.
        "time_zone": "+00:00",
    }

    if cfg.ssl_ca:
        ca_path = Path(cfg.ssl_ca)
        if not ca_path.is_file():
            raise FileNotFoundError(f"MYSQL_SSL_CA points to a missing file: {ca_path}")
        params["ssl_ca"] = str(ca_path)
        params["ssl_verify_cert"] = True
        params["ssl_verify_identity"] = True
    else:
        log.warning(
            "MYSQL_SSL_CA is not set: the connection is encrypted but the server certificate "
            "is not verified against a pinned CA. Set MYSQL_SSL_CA to the DigiCert bundle for "
            "production use."
        )

    conn = mysql.connector.connect(**params)
    log.info("connected to %s:%s as %s (TLS enforced)", cfg.host, cfg.port, cfg.user)
    return conn


def assert_tls(conn: Any) -> str:
    """Confirm the session is genuinely encrypted, and return the negotiated cipher.

    ``require_secure_transport=ON`` is a server-side promise. Verifying it client-side costs
    one query and catches the case where a proxy or a driver default quietly downgraded the
    session — which would otherwise only surface as an Azure compliance finding much later.
    """
    with conn.cursor() as cur:
        cur.execute("SHOW STATUS LIKE 'Ssl_cipher'")
        row = cur.fetchone()

    cipher = row[1] if row else ""
    if not cipher:
        raise RuntimeError(
            "connection is NOT encrypted (Ssl_cipher is empty). Azure requires "
            "require_secure_transport=ON; refusing to collect over plaintext."
        )
    return cipher


def server_identity(conn: Any) -> dict[str, str]:
    """Snapshot version and the MySQL 8.4 settings this repo depends on."""
    with conn.cursor() as cur:
        cur.execute(load_sql("server_identity.sql"))
        rows = cur.fetchall()
    return {name: value for name, value in rows}
