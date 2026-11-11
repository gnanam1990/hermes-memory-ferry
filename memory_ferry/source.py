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


class BuiltinMemorySource:
    """Snapshot and parse the profile's built-in memory without modifying it."""

    adapter_version = "builtin-markdown-v1"

    def __init__(self, hermes_home: Path):
        self.hermes_home = Path(hermes_home).resolve()
        self.paths = {
            "memory": self.hermes_home / "MEMORY.md",
            "user": self.hermes_home / "USER.md",
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
        if not path.exists():
            return
        text = path.read_text(encoding="utf-8-sig", errors="strict")
        stat = path.stat()
        lines = text.splitlines()
        section = "Unsectioned"
        block: list[str] = []
        block_start = 1

        def emit(end_line: int):
            nonlocal block
            content = "\n".join(block).strip()
            block = []
            if not content:
                return None
            content_hash = digest(content)
            source_identity = f"{target}:{path.name}:{block_start}:{end_line}:{content_hash}"
            labels = (target, section.casefold().replace(" ", "-")[:80])
            return CanonicalRecord(
                source_identity=source_identity,
                source_target=target,
                source_path=path.name,
                start_line=block_start,
                end_line=end_line,
                section=section,
                content=content,
                content_hash=content_hash,
                source_mtime_ns=stat.st_mtime_ns,
                labels=labels,
            )

        for index, line in enumerate(lines, start=1):
            if line.startswith("#"):
                record = emit(index - 1)
                if record:
                    yield record
                section = line.lstrip("#").strip() or "Untitled"
                block_start = index + 1
                continue
            if not line.strip():
                record = emit(index - 1)
                if record:
                    yield record
                block_start = index + 1
                continue
            if not block:
                block_start = index
            block.append(line)
        record = emit(len(lines))
        if record:
            yield record

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

