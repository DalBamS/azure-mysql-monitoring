"""Compile multi-target YAML into a validated collection plan.

Operators choose Targets and Profiles. Collection implementations remain repository-owned: YAML
cannot inject SQL, invent measurements, lower safety floors, or contain literal credentials.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml

from telemetry import Cardinality


class PlanError(ValueError):
    """Raised when a YAML collection plan is unsafe or contradictory."""


_DURATION = re.compile(r"^(?P<value>[1-9][0-9]*)(?P<unit>ms|s|m|h)$")
_DURATION_FACTORS = {
    "ms": 0.001,
    "s": 1,
    "m": 60,
    "h": 3600,
}
VALID_TIERS = frozenset({"premium-ssd-v1", "premium-ssd-v2"})


def parse_duration(value: object, *, path: str) -> timedelta:
    if not isinstance(value, str):
        raise PlanError(f"{path} must be a duration string such as 10s or 5m")
    match = _DURATION.fullmatch(value.strip())
    if not match:
        raise PlanError(f"{path} must match <positive integer><ms|s|m|h>; got {value!r}")
    seconds = int(match.group("value")) * _DURATION_FACTORS[match.group("unit")]
    return timedelta(seconds=seconds)


@dataclass(frozen=True, slots=True)
class EnvReference:
    variable: str


@dataclass(frozen=True, slots=True)
class KeyVaultReference:
    vault_uri: str
    secret: str
    version: str = ""


SecretReference = EnvReference | KeyVaultReference


def parse_secret_reference(value: object, *, path: str) -> SecretReference:
    """Accept references only; a scalar would be a committed credential."""

    if not isinstance(value, dict):
        raise PlanError(
            f"{path} must reference an environment variable or Key Vault secret; "
            "literal values are forbidden"
        )
    if set(value) == {"env"}:
        variable = value["env"]
        if not isinstance(variable, str) or not variable.strip():
            raise PlanError(f"{path}.env must be a non-empty environment-variable name")
        return EnvReference(variable.strip())
    if set(value) == {"key_vault"}:
        key_vault = value["key_vault"]
        if not isinstance(key_vault, dict):
            raise PlanError(f"{path}.key_vault must be an object")
        allowed = {"vault_uri", "secret", "version"}
        unknown = set(key_vault) - allowed
        if unknown:
            raise PlanError(f"{path}.key_vault has unknown keys: {', '.join(sorted(unknown))}")
        vault_uri = key_vault.get("vault_uri", "")
        secret = key_vault.get("secret", "")
        version = key_vault.get("version", "")
        if not isinstance(vault_uri, str) or not vault_uri.startswith("https://"):
            raise PlanError(f"{path}.key_vault.vault_uri must be an https URI")
        if not isinstance(secret, str) or not secret.strip():
            raise PlanError(f"{path}.key_vault.secret must be non-empty")
        if not isinstance(version, str):
            raise PlanError(f"{path}.key_vault.version must be a string")
        return KeyVaultReference(vault_uri, secret.strip(), version.strip())
    raise PlanError(f"{path} must contain exactly one of: env, key_vault")


@dataclass(frozen=True, slots=True)
class CollectionGroupSpec:
    name: str
    default_interval: timedelta
    minimum_interval: timedelta
    cardinality: Cardinality
    default_top_k: int | None = None


GROUP_CATALOG = MappingProxyType(
    {
        # Fast production signals.
        "global-status": CollectionGroupSpec(
            "global-status", timedelta(seconds=10), timedelta(seconds=5), Cardinality.LOW
        ),
        "innodb-metrics": CollectionGroupSpec(
            "innodb-metrics", timedelta(seconds=30), timedelta(seconds=10), Cardinality.LOW
        ),
        "error-log": CollectionGroupSpec(
            "error-log", timedelta(seconds=10), timedelta(seconds=5), Cardinality.LOW
        ),
        "collector-health": CollectionGroupSpec(
            "collector-health", timedelta(seconds=10), timedelta(seconds=5), Cardinality.LOW
        ),
        # Diagnostic groups: useful by default in the extended Profile, but slower.
        "global-variables": CollectionGroupSpec(
            "global-variables", timedelta(minutes=15), timedelta(minutes=5), Cardinality.LOW
        ),
        "file-io": CollectionGroupSpec(
            "file-io",
            timedelta(minutes=1),
            timedelta(seconds=30),
            Cardinality.BOUNDED,
            default_top_k=100,
        ),
        "process-states": CollectionGroupSpec(
            "process-states", timedelta(minutes=1), timedelta(seconds=30), Cardinality.LOW
        ),
        # These dimensions can grow with the customer's workload and are always opt-in.
        "statement-digests": CollectionGroupSpec(
            "statement-digests",
            timedelta(minutes=1),
            timedelta(seconds=30),
            Cardinality.HIGH,
            default_top_k=50,
        ),
        "schema-size": CollectionGroupSpec(
            "schema-size",
            timedelta(minutes=15),
            timedelta(minutes=5),
            Cardinality.HIGH,
            default_top_k=500,
        ),
        "table-io": CollectionGroupSpec(
            "table-io",
            timedelta(minutes=5),
            timedelta(minutes=1),
            Cardinality.HIGH,
            default_top_k=100,
        ),
        "index-io": CollectionGroupSpec(
            "index-io",
            timedelta(minutes=5),
            timedelta(minutes=1),
            Cardinality.HIGH,
            default_top_k=100,
        ),
    }
)


@dataclass(frozen=True, slots=True)
class GroupPlan:
    name: str
    interval: timedelta
    top_k: int | None = None
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    groups: Mapping[str, GroupPlan]
    allow_high_cardinality: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "groups", MappingProxyType(dict(self.groups)))


@dataclass(frozen=True, slots=True)
class Target:
    target_id: str
    host: str
    database: str
    tier: str
    profile: str
    username: SecretReference
    password: SecretReference
    run_id: EnvReference
    port: int = 3306
    azure_resource_id: str = ""
    ssl_ca: str = ""


@dataclass(frozen=True, slots=True)
class CollectionJob:
    target: Target
    group: GroupPlan


@dataclass(frozen=True, slots=True)
class CollectionPlan:
    profiles: Mapping[str, Profile]
    targets: tuple[Target, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "profiles", MappingProxyType(dict(self.profiles)))

    @property
    def jobs(self) -> tuple[CollectionJob, ...]:
        return tuple(
            CollectionJob(target=target, group=group)
            for target in self.targets
            for group in self.profiles[target.profile].groups.values()
        )


def _mapping(value: object, *, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlanError(f"{path} must be an object")
    return value


def _compile_group(
    name: str, raw: object, *, path: str, allow_high_cardinality: bool
) -> GroupPlan:
    try:
        spec = GROUP_CATALOG[name]
    except KeyError as exc:
        raise PlanError(f"{path}: unknown collection group {name!r}") from exc
    config = {} if raw is None else _mapping(raw, path=path)
    allowed = {"interval", "top_k", "options", "enabled"}
    unknown = set(config) - allowed
    if unknown:
        raise PlanError(f"{path} has unknown keys: {', '.join(sorted(unknown))}")
    if config.get("enabled", True) is False:
        raise PlanError(f"{path}.enabled=false is only valid when overriding an inherited group")
    if spec.cardinality is Cardinality.HIGH and not allow_high_cardinality:
        raise PlanError(
            f"{path} is high-cardinality; set allow_high_cardinality: true on the Profile"
        )
    interval = (
        parse_duration(config["interval"], path=f"{path}.interval")
        if "interval" in config
        else spec.default_interval
    )
    if interval < spec.minimum_interval:
        raise PlanError(
            f"{path}.interval is below the safety floor of "
            f"{int(spec.minimum_interval.total_seconds())}s"
        )
    top_k = config.get("top_k", spec.default_top_k)
    if top_k is not None and (not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1):
        raise PlanError(f"{path}.top_k must be a positive integer")
    if spec.cardinality in (Cardinality.BOUNDED, Cardinality.HIGH) and top_k is None:
        raise PlanError(f"{path} requires top_k to bound series growth")
    options = config.get("options", {})
    if not isinstance(options, dict):
        raise PlanError(f"{path}.options must be an object")
    return GroupPlan(name=name, interval=interval, top_k=top_k, options=options)


def _compile_profiles(raw_profiles: object) -> dict[str, Profile]:
    profiles_data = _mapping(raw_profiles, path="profiles")
    compiled: dict[str, Profile] = {}
    visiting: set[str] = set()

    def compile_one(name: str) -> Profile:
        if name in compiled:
            return compiled[name]
        if name in visiting:
            raise PlanError(f"profiles.{name}: inheritance cycle detected")
        if name not in profiles_data:
            raise PlanError(f"unknown Profile {name!r}")
        visiting.add(name)
        raw = _mapping(profiles_data[name], path=f"profiles.{name}")
        allowed = {"extends", "allow_high_cardinality", "groups"}
        unknown = set(raw) - allowed
        if unknown:
            raise PlanError(f"profiles.{name} has unknown keys: {', '.join(sorted(unknown))}")

        parent_name = raw.get("extends")
        if parent_name is not None and not isinstance(parent_name, str):
            raise PlanError(f"profiles.{name}.extends must be a Profile name")
        parent = compile_one(parent_name) if parent_name else None
        allow_high = bool(
            raw.get(
                "allow_high_cardinality",
                parent.allow_high_cardinality if parent else False,
            )
        )
        groups = dict(parent.groups) if parent else {}
        raw_groups = _mapping(raw.get("groups", {}), path=f"profiles.{name}.groups")
        for group_name, group_raw in raw_groups.items():
            path = f"profiles.{name}.groups.{group_name}"
            if isinstance(group_raw, dict) and group_raw.get("enabled") is False:
                if set(group_raw) != {"enabled"}:
                    raise PlanError(f"{path}: enabled=false cannot be combined with other options")
                if group_name not in groups:
                    raise PlanError(f"{path}: cannot disable a group that is not inherited")
                del groups[group_name]
                continue
            groups[group_name] = _compile_group(
                group_name, group_raw, path=path, allow_high_cardinality=allow_high
            )
        if not groups:
            raise PlanError(f"profiles.{name} must enable at least one Collection Group")
        profile = Profile(name=name, groups=groups, allow_high_cardinality=allow_high)
        compiled[name] = profile
        visiting.remove(name)
        return profile

    for profile_name in profiles_data:
        compile_one(profile_name)
    return compiled


def _compile_target(raw: object, *, index: int, profiles: Mapping[str, Profile]) -> Target:
    path = f"targets[{index}]"
    data = _mapping(raw, path=path)
    allowed = {
        "id",
        "host",
        "port",
        "database",
        "tier",
        "profile",
        "azure_resource_id",
        "ssl_ca",
        "run_id",
        "credentials",
    }
    unknown = set(data) - allowed
    if unknown:
        raise PlanError(f"{path} has unknown keys: {', '.join(sorted(unknown))}")

    def required_string(name: str) -> str:
        value = data.get(name)
        if not isinstance(value, str) or not value.strip():
            raise PlanError(f"{path}.{name} must be a non-empty string")
        return value.strip()

    target_id = required_string("id")
    tier = required_string("tier")
    if tier not in VALID_TIERS:
        raise PlanError(f"{path}.tier must be one of: {', '.join(sorted(VALID_TIERS))}")
    profile = required_string("profile")
    if profile not in profiles:
        raise PlanError(f"{path}.profile references unknown Profile {profile!r}")
    port = data.get("port", 3306)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise PlanError(f"{path}.port must be an integer from 1 to 65535")
    credentials = _mapping(data.get("credentials"), path=f"{path}.credentials")
    unknown_credentials = set(credentials) - {"username", "password"}
    if unknown_credentials:
        raise PlanError(
            f"{path}.credentials has unknown keys: {', '.join(sorted(unknown_credentials))}"
        )
    run_id = parse_secret_reference(data.get("run_id"), path=f"{path}.run_id")
    if not isinstance(run_id, EnvReference):
        raise PlanError(f"{path}.run_id must reference an environment variable")
    return Target(
        target_id=target_id,
        host=required_string("host"),
        port=port,
        database=required_string("database"),
        tier=tier,
        profile=profile,
        azure_resource_id=str(data.get("azure_resource_id", "")).strip(),
        ssl_ca=str(data.get("ssl_ca", "")).strip(),
        username=parse_secret_reference(
            credentials.get("username"), path=f"{path}.credentials.username"
        ),
        password=parse_secret_reference(
            credentials.get("password"), path=f"{path}.credentials.password"
        ),
        run_id=run_id,
    )


def load_plan(path: str | Path) -> CollectionPlan:
    """Read and compile a collection plan without resolving any credential."""

    source = Path(path)
    try:
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PlanError(f"could not read collection plan {source}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise PlanError(f"invalid YAML in {source}: {exc}") from exc
    root = _mapping(document, path="document")
    allowed = {"version", "profiles", "targets"}
    unknown = set(root) - allowed
    if unknown:
        raise PlanError(f"document has unknown keys: {', '.join(sorted(unknown))}")
    if root.get("version") != 1:
        raise PlanError("document.version must be 1")
    profiles = _compile_profiles(root.get("profiles"))
    targets_data = root.get("targets")
    if not isinstance(targets_data, list) or not targets_data:
        raise PlanError("targets must be a non-empty list")
    targets = tuple(
        _compile_target(raw, index=index, profiles=profiles)
        for index, raw in enumerate(targets_data)
    )
    ids = [target.target_id for target in targets]
    if len(set(ids)) != len(ids):
        duplicates = sorted({target_id for target_id in ids if ids.count(target_id) > 1})
        raise PlanError(f"duplicate Target ids: {', '.join(duplicates)}")
    return CollectionPlan(profiles=profiles, targets=targets)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate an Azure MySQL multi-target Collection Plan"
    )
    parser.add_argument("path", help="YAML Collection Plan")
    args = parser.parse_args(argv)
    try:
        plan = load_plan(args.path)
    except PlanError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 2

    print(
        f"VALID: {len(plan.targets)} Targets, {len(plan.profiles)} Profiles, "
        f"{len(plan.jobs)} Collection Jobs"
    )
    for target in plan.targets:
        groups = ", ".join(plan.profiles[target.profile].groups)
        print(f"  {target.target_id}: {target.host} [{target.profile}] -> {groups}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
