"""Read-only adapter for Hermes's built-in MEMORY.md and USER.md files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .models import CanonicalRecord, canonical_json, digest


_SECRET_PATTERNS = (
    re.compile(r"\b(?:api[_ -]?key|secret|password|private[_ -]?key|seed phrase)\b", re.I),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|0x[a-fA-F0-9]{64})\b"),
)

# Hermes's built-in MemoryStore serializes entries with this exact delimiter.
# Splitting on the full value intentionally keeps a bare ``§`` as record content.
_ENTRY_DELIMITER = "\n§\n"


class BuiltinMemorySource:
    """Snapshot and parse the profile's built-in memory without modifying it."""

    adapter_version = "builtin-memory-v2"

    def __init__(self, hermes_home: Path):
        self.hermes_home = Path(hermes_home).resolve()
        memory_dir = self.hermes_home / "memories"
        self.paths = {
            "memory": memory_dir / "MEMORY.md",
            "user": memory_dir / "USER.md",
        }

    def preflight(self) -> dict:
        files = []
        for target, path in self.paths.items():
            exists = path.exists()
            files.append(
                {
                    "target": target,
                    "path": str(path),
                    "exists": exists,
                    "readable": exists and path.is_file(),
                    "bytes": path.stat().st_size if exists and path.is_file() else 0,
                }
            )
        return {
            "adapter": self.adapter_version,
            "read": True,
            "write": False,
            "delete": False,
            "stable_snapshot_identity": True,
            "record_identity": "path + line span + content hash",
            "query": True,
            "metadata": {
                "provenance": True,
                "source_mtime": True,
                "created_at": False,
                "expires_at": False,
                "relationships": False,
            },
            "files": files,
        }

    def _iter_blocks(self, target: str, path: Path) -> Iterable[CanonicalRecord]:
        if not path.is_file():
            return
        text = path.read_text(encoding="utf-8-sig", errors="strict")
        stat = path.stat()
        source_path = path.relative_to(self.hermes_home).as_posix()
        cursor = 0

        for raw_entry in text.split(_ENTRY_DELIMITER):
            entry_start = cursor
            entry_end = entry_start + len(raw_entry)
            cursor = entry_end + len(_ENTRY_DELIMITER)

            content = raw_entry.strip()
            if not content:
                continue

            # Match Hermes MemoryStore._parse_entries while retaining useful source spans.
            content_start = entry_start + (len(raw_entry) - len(raw_entry.lstrip()))
            content_end = entry_start + len(raw_entry.rstrip())
            start_line = text.count("\n", 0, content_start) + 1
            end_line = text.count("\n", 0, max(content_start, content_end - 1)) + 1

            section = "Unsectioned"
            for line in content.splitlines():
                if line.startswith("#"):
                    section = line.lstrip("#").strip() or "Untitled"
                    break

            content_hash = digest(content)
            source_identity = f"{target}:{source_path}:{start_line}:{end_line}:{content_hash}"
            labels = (target, section.casefold().replace(" ", "-")[:80])
            yield CanonicalRecord(
                source_identity=source_identity,
                source_target=target,
                source_path=source_path,
                start_line=start_line,
                end_line=end_line,
                section=section,
                content=content,
                content_hash=content_hash,
                source_mtime_ns=stat.st_mtime_ns,
                labels=labels,
            )

    def records(self, include_targets: tuple[str, ...] = ("memory", "user")) -> list[CanonicalRecord]:
        result: list[CanonicalRecord] = []
        for target in include_targets:
            path = self.paths.get(target)
            if path is not None:
                result.extend(self._iter_blocks(target, path) or [])
        return result

    def snapshot_identity(self, records: list[CanonicalRecord]) -> str:
        material = [
            {
                "source_identity": record.source_identity,
                "content_hash": record.content_hash,
                "source_mtime_ns": record.source_mtime_ns,
            }
            for record in sorted(records, key=lambda item: item.source_identity)
        ]
        return digest(canonical_json(material))


def looks_sensitive(content: str) -> bool:
    return any(pattern.search(content) for pattern in _SECRET_PATTERNS)

