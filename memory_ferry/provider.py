"""Hermes MemoryProvider implementation backed by the local FerryStore."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.memory_provider import MemoryProvider, RecallStatus

from .store import FerryStore


SEARCH_SCHEMA = {
    "name": "memory_ferry_search",
    "description": "Search the active profile's local Memory Ferry store. Returns provenance-aware redacted snippets.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Terms to retrieve from local memory."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

STATUS_SCHEMA = {
    "name": "memory_ferry_status",
    "description": "Show local Memory Ferry migration and record counts without revealing memory content.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

REMEMBER_SCHEMA = {
    "name": "memory_ferry_remember",
    "description": "Store a new manual local memory. This never changes built-in MEMORY.md or USER.md.",
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The fact or preference to remember."},
            "labels": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        },
        "required": ["content"],
        "additionalProperties": False,
    },
}

FORGET_SCHEMA = {
    "name": "memory_ferry_forget",
    "description": "Remove a manual Memory Ferry record by ID. Migrated records require the operator-only rollback command.",
    "parameters": {
        "type": "object",
        "properties": {"provider_record_id": {"type": "string"}},
        "required": ["provider_record_id"],
        "additionalProperties": False,
    },
}


class MemoryFerryProvider(MemoryProvider):
    def __init__(self):
        self._store: FerryStore | None = None
        self._session_id = ""
        self._recall_count = 0

    @property
    def name(self) -> str:
        return "memory-ferry"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        home = kwargs.get("hermes_home")
        if not home:
            raise RuntimeError("Hermes did not provide profile-scoped hermes_home")
        self._store = FerryStore(Path(home))
        self._session_id = session_id
        self._recall_count = 0

    def system_prompt_block(self) -> str:
        return (
            "# Memory Ferry\n"
            "A local provenance-preserving memory provider is active. Use memory_ferry_search for explicit recall. "
            "Migration and rollback require the operator's `hermes memory-ferry` CLI approval flow."
        )

    def get_config_schema(self) -> list[dict[str, Any]]:
        return []

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [SEARCH_SCHEMA, STATUS_SCHEMA, REMEMBER_SCHEMA, FORGET_SCHEMA]

    def _require_store(self) -> FerryStore:
        if self._store is None:
            raise RuntimeError("Memory Ferry provider is not initialized")
        return self._store

    @staticmethod
    def _public_row(row: dict) -> dict:
        content = str(row.get("content") or "")
        snippet = content[:240] + ("…" if len(content) > 240 else "")
        return {
            "provider_record_id": row.get("provider_record_id"),
            "snippet": snippet,
            "source_target": row.get("source_target"),
            "score": row.get("score"),
            "labels": json.loads(row.get("labels_json") or "[]"),
        }

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not query.strip():
            self._recall_count = 0
            return ""
        rows = self._require_store().search(query, limit=5)
        self._recall_count = len(rows)
        if not rows:
            return ""
        lines = ["## Memory Ferry recall"]
        for row in rows:
            public = self._public_row(row)
            lines.append(f"- [{public['provider_record_id']}] ({public['source_target']}) {public['snippet']}")
        return "\n".join(lines)

    def recall_status(self) -> RecallStatus | None:
        return RecallStatus(provider_label="Memory Ferry", count=self._recall_count, glyph="⛴") if self._recall_count else None

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        store = self._require_store()
        try:
            if tool_name == "memory_ferry_search":
                query = str(args.get("query") or "").strip()
                if not query:
                    return json.dumps({"ok": False, "error": "query is required"})
                limit = max(1, min(10, int(args.get("limit", 5))))
                return json.dumps({"ok": True, "results": [self._public_row(row) for row in store.search(query, limit)]})
            if tool_name == "memory_ferry_status":
                status = store.status()
                return json.dumps(
                    {
                        "ok": True,
                        "record_count": status["record_count"],
                        "migrations": [
                            {"migration_id": row["migration_id"], "state": row["state"], "updated_at": row["updated_at"]}
                            for row in status["migrations"][:10]
                        ],
                    }
                )
            if tool_name == "memory_ferry_remember":
                labels = [str(label)[:80] for label in args.get("labels", []) if str(label).strip()][:8]
                return json.dumps({"ok": True, **store.remember(str(args.get("content") or ""), labels)})
            if tool_name == "memory_ferry_forget":
                result = store.forget_manual(str(args.get("provider_record_id") or ""))
                return json.dumps({"ok": result.get("removed", False), **result})
            return json.dumps({"ok": False, "error": f"unknown tool: {tool_name}"})
        except (ValueError, RuntimeError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})

