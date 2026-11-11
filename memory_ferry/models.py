"""Immutable domain records and deterministic identity helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any


SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class CanonicalRecord:
    source_identity: str
    source_target: str
    source_path: str
    start_line: int
    end_line: int
    section: str
    content: str
    content_hash: str
    source_mtime_ns: int
    labels: tuple[str, ...] = ()
    created_at: str | None = None
    expires_at: str | None = None
    relationships: tuple[str, ...] = ()
    unsupported_fields: tuple[str, ...] = ("created_at", "expires_at", "relationships")
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["labels"] = list(self.labels)
        value["relationships"] = list(self.relationships)
        value["unsupported_fields"] = list(self.unsupported_fields)
        return value

    @property
    def marker(self) -> str:
        return f"mf:v1:{digest(self.source_identity)[:32]}"


@dataclass(frozen=True)
class Policy:
    allow_sensitive: bool = False
    include_targets: tuple[str, ...] = ("memory", "user")
    max_record_bytes: int = 64 * 1024
    revision: str = "policy-v1"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlanItem:
    record: CanonicalRecord
    decision: str
    reason: str
    similar_provider_record_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "record": self.record.to_dict(),
            "decision": self.decision,
            "reason": self.reason,
            "similar_provider_record_id": self.similar_provider_record_id,
        }


@dataclass(frozen=True)
class MigrationPlan:
    migration_id: str
    source_snapshot: str
    destination_identity: str
    adapter_version: str
    policy: Policy
    items: tuple[PlanItem, ...]
    generated_at: str = field(default_factory=utc_now)

    def material(self) -> dict[str, Any]:
        ordered_items = sorted(self.items, key=lambda item: item.record.source_identity)
        return {
            "schema_version": SCHEMA_VERSION,
            "migration_id": self.migration_id,
            "source_snapshot": self.source_snapshot,
            "destination_identity": self.destination_identity,
            "adapter_version": self.adapter_version,
            "policy": self.policy.to_dict(),
            "items": [item.to_dict() for item in ordered_items],
        }

    @property
    def plan_digest(self) -> str:
        return digest(canonical_json(self.material()))

    def to_dict(self) -> dict[str, Any]:
        return {**self.material(), "generated_at": self.generated_at, "plan_digest": self.plan_digest}

