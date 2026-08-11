#!/usr/bin/env python3
"""Generate a reproducible Markdown performance comparison from ADX telemetry."""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode


HIGHER_IS_BETTER = {
    "QPS",
    "Read IOPS",
    "Write IOPS",
    "Read throughput MiB/s",
    "Write throughput MiB/s",
}
LOWER_IS_BETTER = {
    "Read latency ms",
    "Write latency ms",
    "Query p95 ms",
    "Query p99 ms",
    "Query errors/s",
    "Redo log waits/s",
}


@dataclass(frozen=True, slots=True)
class RunSelection:
    run_id: str
    target_id: str


@dataclass(frozen=True, slots=True)
class MetricComparison:
    metric: str
    baseline: float
    candidate: float
    percent_change: float
    verdict: str


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare two benchmark Run/Target pairs and write a Markdown report."
    )
    parser.add_argument("--baseline-run", required=True)
    parser.add_argument("--baseline-target", required=True)
    parser.add_argument("--candidate-run", required=True)
    parser.add_argument("--candidate-target", required=True)
    parser.add_argument("--from-utc", dest="from_utc")
    parser.add_argument("--to-utc", dest="to_utc")
    parser.add_argument(
        "--material-change-pct",
        type=float,
        default=5.0,
        help="Absolute percent change treated as material (default: 5).",
    )
    parser.add_argument(
        "--max-duration-difference-pct",
        type=float,
        default=10.0,
        help="Maximum run-duration difference for a comparable result (default: 10).",
    )
    parser.add_argument(
        "--max-sample-difference-pct",
        type=float,
        default=20.0,
        help="Maximum per-metric sample-count difference (default: 20).",
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"timestamp must include a UTC offset: {value}")
    return parsed.astimezone(timezone.utc)


def compare_metric(
    metric: str,
    baseline: float,
    candidate: float,
    material_change_pct: float,
) -> MetricComparison:
    if baseline == 0:
        if metric not in HIGHER_IS_BETTER | LOWER_IS_BETTER:
            return MetricComparison(metric, baseline, candidate, 0.0, "INCONCLUSIVE")
        if candidate == 0:
            return MetricComparison(
                metric, baseline, candidate, 0.0, "NO MATERIAL CHANGE"
            )
        verdict = "IMPROVEMENT" if metric in HIGHER_IS_BETTER else "REGRESSION"
        return MetricComparison(metric, baseline, candidate, float("inf"), verdict)
    change = 100.0 * (candidate - baseline) / baseline
    if metric in HIGHER_IS_BETTER:
        verdict = (
            "IMPROVEMENT"
            if change >= material_change_pct
            else "REGRESSION"
            if change <= -material_change_pct
            else "NO MATERIAL CHANGE"
        )
    elif metric in LOWER_IS_BETTER:
        verdict = (
            "IMPROVEMENT"
            if change <= -material_change_pct
            else "REGRESSION"
            if change >= material_change_pct
            else "NO MATERIAL CHANGE"
        )
    else:
        verdict = "INCONCLUSIVE"
    return MetricComparison(metric, baseline, candidate, change, verdict)


def relative_difference(left: float, right: float) -> float:
    denominator = max(abs(left), abs(right))
    return 0.0 if denominator == 0 else 100.0 * abs(left - right) / denominator


def kql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def kql_datetime(value: datetime) -> str:
    return f"datetime({value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')})"


def inventory_query(
    baseline: RunSelection,
    candidate: RunSelection,
    start: datetime,
    end: datetime,
) -> str:
    pairs = _pair_filter(baseline, candidate)
    return f"""
MysqlTelemetry
| where Timestamp between ({kql_datetime(start)} .. {kql_datetime(end)})
| where {pairs}
| summarize Start=min(Timestamp), End=max(Timestamp), Points=count(),
            Measurements=dcount(Measurement)
  by RunId, TargetId, Host, Tier
| extend DurationSeconds=datetime_diff('second', End, Start)
| project RunId, TargetId, Host, Tier, Start, End, DurationSeconds, Points, Measurements
| order by RunId asc, TargetId asc
""".strip()


def summary_query(
    baseline: RunSelection,
    candidate: RunSelection,
    start: datetime,
    end: datetime,
) -> str:
    return f"""
BenchmarkSummary(
  {kql_string(baseline.run_id)},
  {kql_string(candidate.run_id)},
  {kql_datetime(start)},
  {kql_datetime(end)}
)
| where {_pair_filter(baseline, candidate)}
| project Metric, RunId, TargetId, Host, Tier, Avg, P95, Max, Samples
| order by Metric asc, RunId asc, TargetId asc
""".strip()


def _pair_filter(baseline: RunSelection, candidate: RunSelection) -> str:
    return (
        f"(RunId == {kql_string(baseline.run_id)} "
        f"and TargetId == {kql_string(baseline.target_id)}) or "
        f"(RunId == {kql_string(candidate.run_id)} "
        f"and TargetId == {kql_string(candidate.target_id)})"
    )


def query_adx(cluster: str, database: str, query: str) -> list[dict[str, Any]]:
    try:
        from azure.identity import DefaultAzureCredential
        from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
    except ImportError as exc:
        raise RuntimeError(
            "ADX reporting requires mysql-internal/collector/requirements-adx.txt"
        ) from exc

    credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)
    client = KustoClient(
        KustoConnectionStringBuilder.with_azure_token_credential(cluster, credential)
    )
    try:
        table = client.execute(database, query).primary_results[0]
        columns = [column.column_name for column in table.columns]
        return [dict(zip(columns, row)) for row in table]
    finally:
        client.close()
        credential.close()


def build_comparisons(
    summary_rows: Iterable[Mapping[str, Any]],
    baseline: RunSelection,
    candidate: RunSelection,
    material_change_pct: float,
) -> tuple[list[MetricComparison], dict[str, float]]:
    by_metric: dict[str, dict[tuple[str, str], Mapping[str, Any]]] = {}
    for row in summary_rows:
        by_metric.setdefault(str(row["Metric"]), {})[
            (str(row["RunId"]), str(row["TargetId"]))
        ] = row

    comparisons = []
    sample_differences = {}
    baseline_key = (baseline.run_id, baseline.target_id)
    candidate_key = (candidate.run_id, candidate.target_id)
    for metric, runs in sorted(by_metric.items()):
        if baseline_key not in runs or candidate_key not in runs:
            continue
        base_row, candidate_row = runs[baseline_key], runs[candidate_key]
        comparisons.append(
            compare_metric(
                metric,
                float(base_row["P95"]),
                float(candidate_row["P95"]),
                material_change_pct,
            )
        )
        sample_differences[metric] = relative_difference(
            float(base_row["Samples"]), float(candidate_row["Samples"])
        )
    return comparisons, sample_differences


def grafana_url(
    endpoint: str,
    baseline: RunSelection,
    candidate: RunSelection,
    start: datetime,
    end: datetime,
) -> str:
    params = {
        "var-baseline": baseline.run_id,
        "var-baseline_target": baseline.target_id,
        "var-candidate": candidate.run_id,
        "var-candidate_target": candidate.target_id,
        "from": str(int(start.timestamp() * 1000)),
        "to": str(int(end.timestamp() * 1000)),
    }
    return (
        endpoint.rstrip("/")
        + "/d/mysqlmon-benchmark-ssd/benchmark-premium-ssd-v1-vs-v2?"
        + urlencode(params)
    )


def render_report(
    *,
    baseline: RunSelection,
    candidate: RunSelection,
    start: datetime,
    end: datetime,
    inventory: list[Mapping[str, Any]],
    comparisons: list[MetricComparison],
    sample_differences: Mapping[str, float],
    max_duration_difference_pct: float,
    max_sample_difference_pct: float,
    material_change_pct: float,
    dashboard_url: str | None,
) -> str:
    duration_by_pair = {
        (str(row["RunId"]), str(row["TargetId"])): float(row["DurationSeconds"])
        for row in inventory
    }
    baseline_duration = duration_by_pair.get((baseline.run_id, baseline.target_id), 0.0)
    candidate_duration = duration_by_pair.get((candidate.run_id, candidate.target_id), 0.0)
    duration_difference = relative_difference(baseline_duration, candidate_duration)
    quality_issues = []
    if len(inventory) != 2:
        quality_issues.append("Both selected Run/Target pairs were not found.")
    if duration_difference > max_duration_difference_pct:
        quality_issues.append(
            f"Run durations differ by {duration_difference:.1f}% "
            f"(limit {max_duration_difference_pct:.1f}%)."
        )
    bad_samples = {
        metric: difference
        for metric, difference in sample_differences.items()
        if difference > max_sample_difference_pct
    }
    if bad_samples:
        quality_issues.append(
            "Sample counts differ beyond the configured limit for: "
            + ", ".join(sorted(bad_samples))
            + "."
        )
    if not comparisons:
        quality_issues.append("No comparable benchmark summary metrics were found.")
    metric_names = {comparison.metric for comparison in comparisons}
    missing_required = {"QPS", "Query p95 ms", "Query p99 ms"} - metric_names
    if missing_required:
        quality_issues.append(
            "Required comparison metrics are missing: "
            + ", ".join(sorted(missing_required))
            + "."
        )
    non_positive_required = sorted(
        comparison.metric
        for comparison in comparisons
        if comparison.metric in {"QPS", "Query p95 ms", "Query p99 ms"}
        and (comparison.baseline <= 0 or comparison.candidate <= 0)
    )
    if non_positive_required:
        quality_issues.append(
            "Required metrics have non-positive values: "
            + ", ".join(non_positive_required)
            + "."
        )
    if not metric_names.intersection({"Read IOPS", "Write IOPS"}):
        quality_issues.append("No comparable file IOPS metric was found.")
    if not metric_names.intersection({"Read latency ms", "Write latency ms"}):
        quality_issues.append("No comparable file latency metric was found.")

    regression_metrics = [
        comparison.metric
        for comparison in comparisons
        if comparison.verdict == "REGRESSION"
    ]
    if quality_issues:
        overall = "INCONCLUSIVE"
    elif regression_metrics:
        overall = "REGRESSION"
    else:
        overall = "PASS"

    lines = [
        "# Azure MySQL performance evaluation",
        "",
        f"- **Overall result:** {overall}",
        f"- **Analysis window:** {start.isoformat()} to {end.isoformat()}",
        f"- **Baseline:** `{baseline.run_id}` / `{baseline.target_id}`",
        f"- **Candidate:** `{candidate.run_id}` / `{candidate.target_id}`",
        f"- **Material-change threshold:** {material_change_pct:.1f}%",
    ]
    if dashboard_url:
        lines.append(f"- **Grafana comparison:** [Open selected runs]({dashboard_url})")

    lines.extend(["", "## Executive interpretation", ""])
    if quality_issues:
        lines.append(
            "The result is **inconclusive** until the following data-quality issues are resolved:"
        )
        lines.extend(f"- {issue}" for issue in quality_issues)
    elif regression_metrics:
        lines.append(
            "The candidate has material regressions in: "
            + ", ".join(f"`{metric}`" for metric in regression_metrics)
            + "."
        )
    else:
        lines.append(
            "No material regression was detected in the collected QPS, IOPS, throughput, "
            "IO latency, query tail-latency, error, or redo-wait metrics."
        )

    lines.extend(
        [
            "",
            "## Run comparability",
            "",
            "| Run | Target | Host | Tier | Start (UTC) | Duration (s) | Points | Measurements |",
            "|---|---|---|---|---:|---:|---:|---:|",
        ]
    )
    for row in inventory:
        lines.append(
            f"| {row['RunId']} | {row['TargetId']} | {row['Host']} | {row['Tier']} "
            f"| {row['Start']} | {float(row['DurationSeconds']):.0f} "
            f"| {int(row['Points'])} | {int(row['Measurements'])} |"
        )

    lines.extend(
        [
            "",
            "## P95 metric comparison",
            "",
            "| Metric | Baseline | Candidate | Change | Interpretation | Sample difference |",
            "|---|---:|---:|---:|---|---:|",
        ]
    )
    for comparison in comparisons:
        lines.append(
            f"| {comparison.metric} | {comparison.baseline:.3f} "
            f"| {comparison.candidate:.3f} | {comparison.percent_change:+.1f}% "
            f"| {comparison.verdict} "
            f"| {sample_differences.get(comparison.metric, 0.0):.1f}% |"
        )

    lines.extend(
        [
            "",
            "## Analysis rules",
            "",
            "- Higher is better for QPS, read/write IOPS and throughput.",
            "- Lower is better for IO latency, query p95/p99, query errors and redo log waits.",
            "- File latency is operation-weighted (`delta(wait_ms) / delta(operations)`), "
            "never an average of per-file averages.",
            "- P95 is the decision statistic; Avg and Max remain supporting context in Grafana.",
            "- A single pair is descriptive, not proof of causality. Repeat at least three times "
            "with identical workload, server SKU, data set, warm-up, and duration.",
            "- Azure Monitor is supplementary for Premium SSD v2 preview; collector telemetry in "
            "ADX is authoritative.",
            "",
            "## Decision record",
            "",
            "- Decision: _fill in after review_",
            "- Reviewer: _fill in_",
            "- Workload/version: _fill in_",
            "- Known anomalies: _fill in_",
            "- Follow-up: _fill in_",
            "",
        ]
    )
    return "\n".join(lines)


def default_output(candidate: RunSelection) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{candidate.run_id}-{candidate.target_id}")
    return Path(__file__).parent / "report" / f"{safe}.md"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if min(
        args.material_change_pct,
        args.max_duration_difference_pct,
        args.max_sample_difference_pct,
    ) <= 0:
        print("all percentage thresholds must be greater than zero", file=sys.stderr)
        return 2

    end = parse_utc(args.to_utc) if args.to_utc else datetime.now(timezone.utc)
    start = parse_utc(args.from_utc) if args.from_utc else end - timedelta(days=90)
    if start >= end:
        print("--from-utc must be before --to-utc", file=sys.stderr)
        return 2

    cluster = os.environ.get("ADX_CLUSTER_URI", "").strip()
    database = os.environ.get("ADX_DATABASE", "").strip()
    if not cluster or not database:
        print("ADX_CLUSTER_URI and ADX_DATABASE are required", file=sys.stderr)
        return 2

    baseline = RunSelection(args.baseline_run, args.baseline_target)
    candidate = RunSelection(args.candidate_run, args.candidate_target)
    inventory = query_adx(
        cluster, database, inventory_query(baseline, candidate, start, end)
    )
    summary = query_adx(
        cluster, database, summary_query(baseline, candidate, start, end)
    )
    comparisons, sample_differences = build_comparisons(
        summary, baseline, candidate, args.material_change_pct
    )

    endpoint = os.environ.get("GRAFANA_ENDPOINT", "").strip()
    report = render_report(
        baseline=baseline,
        candidate=candidate,
        start=start,
        end=end,
        inventory=inventory,
        comparisons=comparisons,
        sample_differences=sample_differences,
        max_duration_difference_pct=args.max_duration_difference_pct,
        max_sample_difference_pct=args.max_sample_difference_pct,
        material_change_pct=args.material_change_pct,
        dashboard_url=(
            grafana_url(endpoint, baseline, candidate, start, end) if endpoint else None
        ),
    )
    output = args.output or default_output(candidate)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    print(f"WROTE: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
