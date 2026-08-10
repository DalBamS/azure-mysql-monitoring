"""Versioned telemetry contract shared by collection groups and sinks.

The first collector encoded dimensions in metric names (for example
``digest.<hash>.latency_ms``). That works for a small allow-list but leaks schema knowledge into
ADX and every dashboard. ``TelemetryPoint`` keeps the Telegraf-shaped measurement/tags/fields
model intact until a sink deliberately projects it.

This module owns the wire invariants. Collection groups own MySQL queries; sinks own transport.
Neither is allowed to invent a competing row shape.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from envelope import iso

CONTRACT_VERSION = 2
FieldValue = int | float | bool | str


class ContractError(ValueError):
    """Raised when telemetry does not satisfy the repository contract."""


class FieldKind(StrEnum):
    """How a numeric field behaves across observations."""

    COUNTER = "counter"
    GAUGE = "gauge"
    STATE = "state"


class Cardinality(StrEnum):
    """Expected series growth for one measurement."""

    LOW = "low"
    BOUNDED = "bounded"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class FieldSpec:
    kind: FieldKind
    unit: str
    value_type: type[int] | type[float] | type[bool] | type[str] = float
    series: bool = True

    def validate(self, measurement: str, name: str, value: FieldValue) -> None:
        # bool is a subclass of int, so accepting it as an integer silently turns state into a
        # counter. Require exact bool semantics while allowing integers in real-valued fields.
        if self.value_type is bool:
            valid = type(value) is bool
        elif self.value_type is float:
            valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif self.value_type is int:
            valid = isinstance(value, int) and not isinstance(value, bool)
        else:
            valid = isinstance(value, self.value_type)
        if not valid:
            raise ContractError(
                f"{measurement}.{name} expects {self.value_type.__name__}, "
                f"got {type(value).__name__}"
            )


@dataclass(frozen=True, slots=True)
class MeasurementSpec:
    name: str
    fields: Mapping[str, FieldSpec]
    tags: frozenset[str] = frozenset()
    cardinality: Cardinality = Cardinality.LOW
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name or any(char.isspace() for char in self.name):
            raise ContractError("measurement name must be non-empty and contain no whitespace")
        if not self.fields:
            raise ContractError(f"{self.name} must declare at least one field")
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))


@dataclass(frozen=True, slots=True)
class TelemetryContext:
    """Dimensions attached by the runtime, never by an individual collection group."""

    run_id: str
    target_id: str
    host: str
    tier: str
    azure_resource_id: str = ""
    collector_id: str = ""

    def __post_init__(self) -> None:
        required = {
            "run_id": self.run_id,
            "target_id": self.target_id,
            "host": self.host,
            "tier": self.tier,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ContractError(f"telemetry context requires: {', '.join(missing)}")


@dataclass(frozen=True, slots=True)
class TelemetryPoint:
    observed_at: datetime
    context: TelemetryContext
    measurement: str
    fields: Mapping[str, FieldValue]
    tags: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ContractError("TelemetryPoint.observed_at must be timezone-aware")
        if not self.measurement:
            raise ContractError("TelemetryPoint.measurement must not be empty")
        if not self.fields:
            raise ContractError(f"{self.measurement} must contain at least one field")
        if any(not name for name in self.fields):
            raise ContractError(f"{self.measurement} contains an empty field name")
        if any(not name or value is None for name, value in self.tags.items()):
            raise ContractError(f"{self.measurement} tags require non-empty names and values")

        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))
        object.__setattr__(self, "tags", MappingProxyType(dict(self.tags)))

    @property
    def series_key(self) -> str:
        """Stable key for one measurement/tag combination.

        Tag order in YAML or SQL results must not split one logical series into multiple ADX
        series, so the hash uses a canonical JSON encoding.
        """

        identity = {
            "target_id": self.context.target_id,
            "measurement": self.measurement,
            "tags": dict(sorted(self.tags.items())),
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:24]

    def packed_row(self) -> dict[str, Any]:
        """Canonical version-2 JSONL/ADX row."""

        return {
            "ts": iso(self.observed_at.astimezone(timezone.utc)),
            "contract_version": CONTRACT_VERSION,
            "run_id": self.context.run_id,
            "target_id": self.context.target_id,
            "host": self.context.host,
            "azure_resource_id": self.context.azure_resource_id,
            "tier": self.context.tier,
            "collector_id": self.context.collector_id,
            "measurement": self.measurement,
            "series_key": self.series_key,
            "tags": dict(self.tags),
            "fields": dict(self.fields),
        }


class MetricCatalog:
    """Validates points against repository-owned measurement semantics."""

    def __init__(self, measurements: list[MeasurementSpec]) -> None:
        by_name = {measurement.name: measurement for measurement in measurements}
        if len(by_name) != len(measurements):
            raise ContractError("metric catalog contains duplicate measurement names")
        self._measurements = MappingProxyType(by_name)

    def measurement(self, name: str) -> MeasurementSpec:
        try:
            return self._measurements[name]
        except KeyError as exc:
            raise ContractError(f"unknown measurement {name!r}") from exc

    def validate(self, point: TelemetryPoint) -> TelemetryPoint:
        spec = self.measurement(point.measurement)
        unknown_fields = set(point.fields) - set(spec.fields)
        missing_fields = set(spec.fields) - set(point.fields)
        unknown_tags = set(point.tags) - set(spec.tags)
        missing_tags = set(spec.tags) - set(point.tags)
        problems = []
        if unknown_fields:
            problems.append(f"unknown fields: {', '.join(sorted(unknown_fields))}")
        if missing_fields:
            problems.append(f"missing fields: {', '.join(sorted(missing_fields))}")
        if unknown_tags:
            problems.append(f"unknown tags: {', '.join(sorted(unknown_tags))}")
        if missing_tags:
            problems.append(f"missing tags: {', '.join(sorted(missing_tags))}")
        if problems:
            raise ContractError(f"{point.measurement}: {'; '.join(problems)}")
        for name, value in point.fields.items():
            spec.fields[name].validate(point.measurement, name, value)
        return point

    def series_rows(self, point: TelemetryPoint) -> list[dict[str, Any]]:
        """Project dashboard-critical fields to narrow time-series rows."""

        spec = self.measurement(point.measurement)
        self.validate(point)
        common = {
            "ts": iso(point.observed_at.astimezone(timezone.utc)),
            "contract_version": CONTRACT_VERSION,
            "run_id": point.context.run_id,
            "target_id": point.context.target_id,
            "host": point.context.host,
            "tier": point.context.tier,
            "measurement": point.measurement,
            "series_key": point.series_key,
            "tags": dict(point.tags),
        }
        return [
            {
                **common,
                "field": name,
                "value": float(value),
                "kind": field_spec.kind.value,
                "unit": field_spec.unit,
            }
            for name, value in point.fields.items()
            for field_spec in (spec.fields[name],)
            if field_spec.series and isinstance(value, (int, float)) and not isinstance(value, bool)
        ]


def legacy_metric_rows(
    point: TelemetryPoint,
    catalog: MetricCatalog,
    *,
    source: str,
    field_names: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Compatibility adapter for the version-1 ``MysqlMetrics`` table.

    Existing dashboards continue to work while sinks and ADX migrate to version 2. Tags are not
    encoded into the legacy metric name here; dimensional measurements must stay on the v2 path
    rather than creating another string convention that future code has to unwind.
    """

    if point.tags:
        raise ContractError(
            f"{point.measurement} has tags and cannot be represented by the legacy metric table"
        )
    names = field_names or {}
    catalog.validate(point)
    return [
        {
            "ts": iso(point.observed_at.astimezone(timezone.utc)),
            "run_id": point.context.run_id,
            "host": point.context.host,
            "tier": point.context.tier,
            "source": source,
            "metric": names.get(name, name),
            "value": float(value),
        }
        for name, value in point.fields.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
