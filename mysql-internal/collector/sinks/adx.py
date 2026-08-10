"""Azure Data Explorer sink — the one sanctioned dependency exception.

Two ingestion paths share one set of tables:

* **streaming** (hot) — seconds of latency, the path production real-time monitoring uses.
  Capped at roughly 4 MB per request, so it is unsuitable for bulk loads.
* **queued** (cold) — batches for up to 5 minutes by default. Correct for replay, backfill
  and benchmark uploads; it cannot carry real-time monitoring, and shrinking the batching
  policy to force it to is what produces thousands of tiny extents and degrades the cluster.

Authentication uses ``DefaultAzureCredential``, so the same code path works with a managed
identity in Azure and with ``az login`` on a workstation. There is no ADX secret anywhere.
"""

from __future__ import annotations

import io
import json
import logging
from typing import Any, Sequence

log = logging.getLogger(__name__)

METRICS_TABLE = "MysqlMetrics"
EVENTS_TABLE = "MysqlEvents"
METRICS_MAPPING = "MysqlMetricsMapping"
EVENTS_MAPPING = "MysqlEventsMapping"

# Well under the ~4 MB streaming cap. Rows are small, but a backlog flush after a stall can
# be large, so batches are split rather than trusted to be small.
MAX_ROWS_PER_REQUEST = 1000


class AdxSink:
    def __init__(self, cfg: Any, streaming: bool = True) -> None:
        # Imported here rather than at module scope: sinks/__init__ imports this module
        # lazily, so the core collector never requires the Azure SDKs to be installed.
        from azure.identity import DefaultAzureCredential
        from azure.kusto.data import KustoConnectionStringBuilder
        from azure.kusto.ingest import (
            IngestionProperties,
            KustoStreamingIngestClient,
            QueuedIngestClient,
        )
        from azure.kusto.data.data_format import DataFormat

        self.name = "adx-streaming" if streaming else "adx-queued"
        self.streaming = streaming
        self.database = cfg.adx_database
        self.last_error: str | None = None

        credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)

        if streaming:
            # Streaming ingestion targets the *engine* endpoint, not the ingestion endpoint.
            # Using the ingest- URI here is the classic misconfiguration; it fails at run time
            # rather than at construction, so the endpoint is validated up front.
            uri = cfg.adx_cluster_uri or cfg.adx_ingest_uri.replace("https://ingest-", "https://")
            kcsb = KustoConnectionStringBuilder.with_azure_token_credential(uri, credential)
            self._client: Any = KustoStreamingIngestClient(kcsb)
        else:
            kcsb = KustoConnectionStringBuilder.with_azure_token_credential(
                cfg.adx_ingest_uri, credential
            )
            self._client = QueuedIngestClient(kcsb)

        self._props = IngestionProperties
        self._format = DataFormat.MULTIJSON
        log.info("%s sink ready (database=%s)", self.name, self.database)

    def _ingest(self, rows: Sequence[dict[str, Any]], table: str, mapping: str) -> None:
        if not rows:
            return

        props = self._props(
            database=self.database,
            table=table,
            data_format=self._format,
            ingestion_mapping_reference=mapping,
        )

        try:
            for start in range(0, len(rows), MAX_ROWS_PER_REQUEST):
                chunk = rows[start : start + MAX_ROWS_PER_REQUEST]
                payload = json.dumps(chunk, separators=(",", ":")).encode("utf-8")
                stream = io.BytesIO(payload)
                self._client.ingest_from_stream(stream, ingestion_properties=props)
            self.last_error = None
        except Exception as exc:  # noqa: BLE001 - ingestion must never kill the poll loop
            self.last_error = str(exc)
            log.error(
                "%s ingestion into %s failed: %s. Sampling continues; replay the JSONL "
                "archive through the queued path to recover this window.",
                self.name, table, exc,
            )

    def write_metrics(self, rows: Sequence[dict[str, Any]]) -> None:
        self._ingest(rows, METRICS_TABLE, METRICS_MAPPING)

    def write_events(self, rows: Sequence[dict[str, Any]]) -> None:
        self._ingest(rows, EVENTS_TABLE, EVENTS_MAPPING)

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()
