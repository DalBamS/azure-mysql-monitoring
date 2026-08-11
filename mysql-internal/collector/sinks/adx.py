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
TELEMETRY_TABLE = "MysqlTelemetry"
SERIES_TABLE = "MysqlMetricSeries"
METRICS_MAPPING = "MysqlMetricsMapping"
EVENTS_MAPPING = "MysqlEventsMapping"
TELEMETRY_MAPPING = "MysqlTelemetryMapping"
SERIES_MAPPING = "MysqlMetricSeriesMapping"

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
        from azure.kusto.ingest.ingestion_properties import ReportLevel
        from azure.kusto.ingest.status import KustoIngestStatusQueues
        from azure.kusto.data.data_format import DataFormat

        self.name = "adx-streaming" if streaming else "adx-queued"
        self.streaming = streaming
        self.database = cfg.adx_database
        self.last_error: str | None = None
        self.last_source_ids: list[str] = []
        self._status_queues: Any | None = None
        self._report_level = ReportLevel

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
            self._status_queues = KustoIngestStatusQueues(self._client)

        self._props = IngestionProperties
        self._format = DataFormat.MULTIJSON
        log.info("%s sink ready (database=%s)", self.name, self.database)

    def _ingest(
        self,
        rows: Sequence[dict[str, Any]],
        table: str,
        mapping: str,
        ingestion_tag: str | None = None,
    ) -> tuple[str | None, list[str]]:
        if not rows:
            return None, []

        property_args: dict[str, Any] = {
            "database": self.database,
            "table": table,
            "data_format": self._format,
            "ingestion_mapping_reference": mapping,
        }
        if not self.streaming and ingestion_tag:
            property_args["report_level"] = self._report_level.FailuresAndSuccesses
            property_args["ingest_by_tags"] = [ingestion_tag]
            property_args["ingest_if_not_exists"] = [f"ingest-by:{ingestion_tag}"]
        props = self._props(
            **property_args
        )

        try:
            source_ids = []
            for start in range(0, len(rows), MAX_ROWS_PER_REQUEST):
                chunk = rows[start : start + MAX_ROWS_PER_REQUEST]
                payload = json.dumps(chunk, separators=(",", ":")).encode("utf-8")
                stream = io.BytesIO(payload)
                result = self._client.ingest_from_stream(
                    stream, ingestion_properties=props
                )
                source_id = getattr(result, "source_id", None)
                if source_id is not None:
                    source_ids.append(str(source_id))
            return None, source_ids
        except Exception as exc:  # noqa: BLE001 - ingestion must never kill the poll loop
            log.error(
                "%s ingestion into %s failed: %s. Sampling continues; replay the JSONL "
                "archive through the queued path to recover this window.",
                self.name, table, exc,
            )
            return str(exc), []

    def write_raw_rows(
        self,
        rows: Sequence[dict[str, Any]],
        table: str,
        mapping: str,
        ingestion_tag: str | None = None,
    ) -> list[str]:
        """Ingest an already-projected batch.

        The durable spool stores the exact table projection so replay never has to
        reconstruct a TelemetryPoint or depend on a newer catalog.
        """

        self.last_error, self.last_source_ids = self._ingest(
            rows, table, mapping, ingestion_tag
        )
        return self.last_source_ids

    def pop_ingestion_statuses(self, limit: int = 32) -> list[tuple[str, Any]]:
        """Return terminal queued-ingestion statuses.

        Status reporting is enabled only for queued ingestion. The durable spool
        owns this client and is therefore the only consumer of these queues.
        """

        if self._status_queues is None:
            return []
        try:
            failures = [
                ("failure", message)
                for message in self._status_queues.failure.pop(limit)
            ]
            successes = [
                ("success", message)
                for message in self._status_queues.success.pop(limit)
            ]
            return failures + successes
        except Exception as exc:  # noqa: BLE001 - status polling retries next cycle
            self.last_error = f"queued ingestion status poll failed: {exc}"
            log.warning("%s", self.last_error)
            return []

    def write_metrics(self, rows: Sequence[dict[str, Any]]) -> None:
        self.write_raw_rows(rows, METRICS_TABLE, METRICS_MAPPING)

    def write_events(self, rows: Sequence[dict[str, Any]]) -> None:
        self.write_raw_rows(rows, EVENTS_TABLE, EVENTS_MAPPING)

    def write_points(self, points: Sequence[Any], catalog: Any) -> None:
        packed = [point.packed_row() for point in points]
        series = [row for point in points for row in catalog.series_rows(point)]
        errors = [
            error
            for error in (
                self._ingest(packed, TELEMETRY_TABLE, TELEMETRY_MAPPING)[0],
                self._ingest(series, SERIES_TABLE, SERIES_MAPPING)[0],
            )
            if error
        ]
        self.last_error = "; ".join(errors) if errors else None

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()
