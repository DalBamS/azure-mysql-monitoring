"""Sink interface.

A sink failure must never kill the poll loop. Every implementation swallows transport
errors, reports them through ``last_error``, and lets the collector keep sampling.
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence


class Sink(Protocol):
    name: str

    def write_metrics(self, rows: Sequence[dict[str, Any]]) -> None: ...

    def write_events(self, rows: Sequence[dict[str, Any]]) -> None: ...

    def write_points(self, points: Sequence[Any], catalog: Any) -> None: ...

    def close(self) -> None: ...


def build_sink(kind: str, cfg: Any, out_path: str | None) -> Sink:
    """Construct a sink by name.

    The ADX sinks are imported lazily so the core collector runs on
    ``mysql-connector-python`` alone — a benchmark run must not require the Azure SDKs.
    """
    if kind == "jsonl":
        from sinks.jsonl import JsonlSink

        return JsonlSink(out_path)

    if kind in ("adx-streaming", "adx-queued"):
        cfg.require_adx()
        from sinks.adx import AdxSink

        return AdxSink(cfg, streaming=(kind == "adx-streaming"))

    raise ValueError(f"unknown sink {kind!r}; expected jsonl, adx-streaming or adx-queued")
