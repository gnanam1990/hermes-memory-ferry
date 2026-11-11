"""Profile-scoped SQLite store and durable migration journal."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
import uuid
from typing import Iterator

from .models import canonical_json, digest, utc_now
from .search import rank


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS migrations (
  migration_id TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  source_snapshot TEXT NOT NULL,
  destination_identity TEXT NOT NULL,
  adapter_version TEXT NOT NULL,
  policy_json TEXT NOT NULL,
  plan_digest TEXT NOT NULL,
  approved_digest TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_records (
  migration_id TEXT NOT NULL REFERENCES migrations(migration_id),
  source_identity TEXT NOT NULL,
  marker TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason TEXT NOT NULL,
  canonical_json TEXT NOT NULL,
  similar_provider_record_id TEXT,
  PRIMARY KEY (migration_id, source_identity)
);
CREATE TABLE IF NOT EXISTS records (
  provider_record_id TEXT PRIMARY KEY,
  marker TEXT NOT NULL UNIQUE,
  source_identity TEXT NOT NULL,
  content TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  source_target TEXT NOT NULL,
  provenance_json TEXT NOT NULL,
  labels_json TEXT NOT NULL,
  migration_id TEXT,
  created_by_migration INTEGER NOT NULL CHECK(created_by_migration IN (0,1)),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_hash ON records(content_hash);
CREATE INDEX IF NOT EXISTS idx_records_migration ON records(migration_id);
CREATE TABLE IF NOT EXISTS operations (
  operation_id TEXT PRIMARY KEY,
  migration_id TEXT NOT NULL REFERENCES migrations(migration_id),
  source_identity TEXT NOT NULL,
  marker TEXT NOT NULL,
  operation TEXT NOT NULL,
  state TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  provider_record_id TEXT,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(migration_id, source_identity, operation)
);
CREATE TABLE IF NOT EXISTS parity_runs (
  parity_run_id TEXT PRIMARY KEY,
  migration_id TEXT NOT NULL REFERENCES migrations(migration_id),
  query_corpus_digest TEXT NOT NULL,
  report_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  migration_id TEXT,
  event_type TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


class FerryStore:
    """Owns local state under one Hermes profile."""

    def __init__(self, hermes_home: Path):
        self.hermes_home = Path(hermes_home).resolve()
        self.data_dir = self.hermes_home / "plugin-data" / "memory-ferry"
        self.artifacts_dir = self.data_dir / "artifacts"
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = self.data_dir / "memory-ferry.sqlite3"
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self.read_connection() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', '1')"
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @property
    def destination_identity(self) -> str:
        return f"sqlite:{digest(str(self.db_path))[:24]}:schema-v1"

    def preflight(self) -> dict:
        with self.read_connection() as conn:
            conn.execute("SELECT 1").fetchone()
            version = conn.execute("SELECT sqlite_version()").fetchone()[0]
        return {
            "adapter": "memory-ferry-sqlite-v1",
            "database": str(self.db_path),
            "destination_identity": self.destination_identity,
            "sqlite_version": version,
            "read": True,
            "write": True,
            "delete": True,
            "query": True,
            "metadata": True,
            "stable_provider_identity": True,
            "immutable_marker_lookup": True,
            "native_idempotency": "UNIQUE(marker)",
            "atomic_record_and_journal_commit": True,
        }

    def audit(self, event_type: str, evidence: dict, migration_id: str | None = None, conn=None) -> None:
        target = conn or self.connect()
        try:
            target.execute(
                "INSERT INTO audit_events(migration_id,event_type,evidence_json,created_at) VALUES(?,?,?,?)",
                (migration_id, event_type, canonical_json(evidence), utc_now()),
            )
        finally:
            if conn is None:
                target.close()

    def rows(self) -> list[dict]:
        with self.read_connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM records ORDER BY created_at, provider_record_id")]

    def search(self, query: str, limit: int = 8) -> list[dict]:
        rows = self.rows()
        return rank(query, rows, limit=limit)

    def get_by_marker(self, marker: str, conn=None) -> dict | None:
        target = conn or self.connect()
        try:
            row = target.execute("SELECT * FROM records WHERE marker=?", (marker,)).fetchone()
            return dict(row) if row else None
        finally:
            if conn is None:
                target.close()

    def get_by_hash(self, content_hash: str) -> list[dict]:
        with self.read_connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM records WHERE content_hash=?", (content_hash,))]

    def remember(self, content: str, labels: list[str] | None = None) -> dict:
        text = content.strip()
        if not text:
            raise ValueError("content is required")
        if len(text.encode("utf-8")) > 64 * 1024:
            raise ValueError("content exceeds 64 KiB")
        record_id = str(uuid.uuid4())
        marker = f"mf:manual:{record_id}"
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO records(provider_record_id,marker,source_identity,content,content_hash,
                source_target,provenance_json,labels_json,migration_id,created_by_migration,created_at)
                VALUES(?,?,?,?,?,?,?,?,NULL,0,?)""",
                (
                    record_id,
                    marker,
                    marker,
                    text,
                    digest(text),
                    "manual",
                    canonical_json({"source": "memory_ferry_remember"}),
                    canonical_json(labels or ["manual"]),
                    now,
                ),
            )
            self.audit("MANUAL_RECORD_CREATED", {"provider_record_id": record_id, "content_hash": digest(text)}, conn=conn)
        return {"provider_record_id": record_id, "content_hash": digest(text)}

    def forget_manual(self, provider_record_id: str) -> dict:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM records WHERE provider_record_id=?", (provider_record_id,)).fetchone()
            if not row:
                return {"removed": False, "reason": "NOT_FOUND"}
            if row["created_by_migration"]:
                return {"removed": False, "reason": "USE_MIGRATION_ROLLBACK"}
            conn.execute("DELETE FROM records WHERE provider_record_id=?", (provider_record_id,))
            self.audit("MANUAL_RECORD_REMOVED", {"provider_record_id": provider_record_id}, conn=conn)
        return {"removed": True, "provider_record_id": provider_record_id}

    def migration(self, migration_id: str) -> dict | None:
        with self.read_connection() as conn:
            row = conn.execute("SELECT * FROM migrations WHERE migration_id=?", (migration_id,)).fetchone()
            return dict(row) if row else None

    def plan_items(self, migration_id: str) -> list[dict]:
        with self.read_connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM plan_records WHERE migration_id=? ORDER BY source_identity", (migration_id,)
                )
            ]

    def operations(self, migration_id: str) -> list[dict]:
        with self.read_connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM operations WHERE migration_id=? ORDER BY created_at, operation_id", (migration_id,)
                )
            ]

    def latest_parity(self, migration_id: str) -> dict | None:
        with self.read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM parity_runs WHERE migration_id=? ORDER BY created_at DESC LIMIT 1", (migration_id,)
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            result["report"] = json.loads(result.pop("report_json"))
            return result

    def status(self, migration_id: str | None = None) -> dict:
        with self.read_connection() as conn:
            if migration_id:
                migration = conn.execute("SELECT * FROM migrations WHERE migration_id=?", (migration_id,)).fetchone()
                if not migration:
                    return {"found": False, "migration_id": migration_id}
                counts = {
                    row["decision"]: row["n"]
                    for row in conn.execute(
                        "SELECT decision, COUNT(*) n FROM plan_records WHERE migration_id=? GROUP BY decision",
                        (migration_id,),
                    )
                }
                operations = {
                    row["state"]: row["n"]
                    for row in conn.execute(
                        "SELECT state, COUNT(*) n FROM operations WHERE migration_id=? GROUP BY state",
                        (migration_id,),
                    )
                }
                return {
                    "found": True,
                    "migration": dict(migration),
                    "plan_counts": counts,
                    "operation_counts": operations,
                    "latest_parity": self.latest_parity(migration_id),
                }
            migrations = [dict(row) for row in conn.execute("SELECT * FROM migrations ORDER BY created_at DESC")]
            record_count = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            return {"migrations": migrations, "record_count": record_count, "database": str(self.db_path)}

