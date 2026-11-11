"""Migration planning, approval, execution, verification, and rollback."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import json
import os
import uuid

from .models import SCHEMA_VERSION, MigrationPlan, PlanItem, Policy, canonical_json, digest, utc_now
from .search import jaccard, rank, tokens
from .source import BuiltinMemorySource, looks_sensitive
from .store import FerryStore


ADAPTER_VERSION = "memory-ferry-sqlite-v1"


class MigrationError(RuntimeError):
    pass


class MigrationService:
    def __init__(self, hermes_home: Path):
        self.hermes_home = Path(hermes_home).resolve()
        self.source = BuiltinMemorySource(self.hermes_home)
        self.store = FerryStore(self.hermes_home)

    def preflight(self) -> dict:
        source = self.source.preflight()
        destination = self.store.preflight()
        files_present = any(item["readable"] for item in source["files"])
        result = {
            "status": "SUPPORTED" if files_present else "UNSUPPORTED",
            "reason": "READY" if files_present else "NO_BUILTIN_MEMORY_FILES",
            "scope": "builtin-memory-to-memory-ferry-local-provider",
            "source": source,
            "destination": destination,
            "critical_contract": {
                "source_read": files_present,
                "source_preservation": True,
                "destination_stable_identity": destination["stable_provider_identity"],
                "destination_marker_lookup": destination["immutable_marker_lookup"],
                "destination_delete": destination["delete"],
                "query": source["query"] and destination["query"],
            },
            "limitations": [
                "Only built-in MEMORY.md and USER.md are supported as source.",
                "Arbitrary external-provider export is not available through the public Hermes API.",
                "Built-in Markdown does not provide created_at, expiry, or relationship metadata.",
                "Migration is one-way; rollback removes only destination records and never rewrites source files.",
            ],
        }
        self.store.audit("PREFLIGHT", {"status": result["status"], "reason": result["reason"]})
        return result

    def _policy_decision(self, record, policy: Policy) -> tuple[str, str]:
        if record.source_target not in policy.include_targets:
            return "BLOCKED_POLICY", "SOURCE_TARGET_EXCLUDED"
        if len(record.content.encode("utf-8")) > policy.max_record_bytes:
            return "BLOCKED_POLICY", "RECORD_TOO_LARGE"
        if looks_sensitive(record.content) and not policy.allow_sensitive:
            return "BLOCKED_POLICY", "SENSITIVE_CONTENT_REQUIRES_EXPLICIT_OPT_IN"
        return "CREATE", "NEW_DESTINATION_RECORD"

    def create_plan(self, policy: Policy | None = None) -> dict:
        policy = policy or Policy()
        preflight = self.preflight()
        if preflight["status"] != "SUPPORTED":
            raise MigrationError(f"preflight unsupported: {preflight['reason']}")
        records = self.source.records(policy.include_targets)
        snapshot = self.source.snapshot_identity(records)
        migration_id = str(uuid.uuid4())
        destination_rows = self.store.rows()
        items: list[PlanItem] = []
        for record in records:
            decision, reason = self._policy_decision(record, policy)
            similar_id = None
            marker_row = next((row for row in destination_rows if row["marker"] == record.marker), None)
            exact_rows = [row for row in destination_rows if row["content_hash"] == record.content_hash]
            if decision == "CREATE" and marker_row:
                if marker_row["content_hash"] == record.content_hash:
                    decision, reason = "EXACT_DUPLICATE", "IMMUTABLE_MARKER_ALREADY_IMPORTED"
                    similar_id = marker_row["provider_record_id"]
                else:
                    decision, reason = "CONFLICT", "IMMUTABLE_MARKER_CONTENT_MISMATCH"
                    similar_id = marker_row["provider_record_id"]
            elif decision == "CREATE" and exact_rows:
                decision, reason = "EXACT_DUPLICATE", "CONTENT_HASH_ALREADY_PRESENT"
                similar_id = exact_rows[0]["provider_record_id"]
            elif decision == "CREATE":
                best = max(destination_rows, key=lambda row: jaccard(record.content, row["content"]), default=None)
                if best and jaccard(record.content, best["content"]) >= 0.82:
                    decision, reason = "BLOCKED_POLICY", "SEMANTIC_CANDIDATE_REQUIRES_MANUAL_REVIEW"
                    similar_id = best["provider_record_id"]
            items.append(PlanItem(record, decision, reason, similar_id))
        plan = MigrationPlan(
            migration_id=migration_id,
            source_snapshot=snapshot,
            destination_identity=self.store.destination_identity,
            adapter_version=ADAPTER_VERSION,
            policy=policy,
            items=tuple(items),
        )
        artifact = self.store.artifacts_dir / f"{migration_id}.plan.json"
        export = self.store.artifacts_dir / f"{migration_id}.canonical.jsonl"
        artifact.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with export.open("w", encoding="utf-8") as handle:
            for item in plan.items:
                handle.write(canonical_json(item.record.to_dict()) + "\n")
        os.chmod(artifact, 0o600)
        os.chmod(export, 0o600)
        now = utc_now()
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO migrations(migration_id,state,source_snapshot,destination_identity,
                adapter_version,policy_json,plan_digest,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    migration_id,
                    "APPROVAL_REQUIRED",
                    snapshot,
                    self.store.destination_identity,
                    ADAPTER_VERSION,
                    canonical_json(policy.to_dict()),
                    plan.plan_digest,
                    now,
                    now,
                ),
            )
            conn.executemany(
                """INSERT INTO plan_records(migration_id,source_identity,marker,content_hash,decision,
                reason,canonical_json,similar_provider_record_id) VALUES(?,?,?,?,?,?,?,?)""",
                [
                    (
                        migration_id,
                        item.record.source_identity,
                        item.record.marker,
                        item.record.content_hash,
                        item.decision,
                        item.reason,
                        canonical_json(item.record.to_dict()),
                        item.similar_provider_record_id,
                    )
                    for item in plan.items
                ],
            )
            self.store.audit(
                "PLAN_CREATED",
                {
                    "plan_digest": plan.plan_digest,
                    "source_snapshot": snapshot,
                    "counts": dict(Counter(item.decision for item in plan.items)),
                },
                migration_id,
                conn,
            )
        return {
            "migration_id": migration_id,
            "state": "APPROVAL_REQUIRED",
            "plan_digest": plan.plan_digest,
            "source_snapshot": snapshot,
            "counts": dict(Counter(item.decision for item in plan.items)),
            "plan_artifact": str(artifact),
            "canonical_export": str(export),
        }

    def _current_snapshot(self, migration: dict) -> str:
        policy = Policy(**json.loads(migration["policy_json"]))
        return self.source.snapshot_identity(self.source.records(policy.include_targets))

    def _fence_plan(self, migration_id: str, reason: str) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE migrations SET state='PLAN_INTEGRITY_FAILED',updated_at=? WHERE migration_id=?",
                (utc_now(), migration_id),
            )
            self.store.audit("PLAN_INTEGRITY_FAILED", {"reason": reason}, migration_id, conn)

    def _assert_plan_integrity(self, migration: dict) -> None:
        migration_id = migration["migration_id"]
        try:
            policy = json.loads(migration["policy_json"])
            items = []
            for item in self.store.plan_items(migration_id):
                record = json.loads(item["canonical_json"])
                required = {
                    "source_identity", "source_target", "source_path", "start_line", "end_line",
                    "content", "content_hash", "source_mtime_ns", "labels", "unsupported_fields", "schema_version",
                }
                if not isinstance(record, dict) or not required.issubset(record):
                    raise ValueError("canonical record fields are incomplete")
                if record["schema_version"] != SCHEMA_VERSION:
                    raise ValueError("canonical record schema version changed")
                if digest(str(record["content"])) != record["content_hash"]:
                    raise ValueError("canonical record content hash mismatch")
                expected_identity = (
                    f"{record['source_target']}:{record['source_path']}:{record['start_line']}:"
                    f"{record['end_line']}:{record['content_hash']}"
                )
                if record["source_identity"] != expected_identity:
                    raise ValueError("canonical source identity mismatch")
                if (
                    item["source_identity"] != record["source_identity"]
                    or item["content_hash"] != record["content_hash"]
                    or item["marker"] != f"mf:v1:{digest(record['source_identity'])[:32]}"
                ):
                    raise ValueError("durable plan columns do not match canonical record")
                if item["decision"] not in {"CREATE", "EXACT_DUPLICATE", "BLOCKED_POLICY", "CONFLICT"}:
                    raise ValueError("unknown plan decision")
                items.append(
                    {
                        "record": record,
                        "decision": item["decision"],
                        "reason": item["reason"],
                        "similar_provider_record_id": item["similar_provider_record_id"],
                    }
                )
            items.sort(key=lambda value: value["record"]["source_identity"])
            material = {
                "schema_version": SCHEMA_VERSION,
                "migration_id": migration_id,
                "source_snapshot": migration["source_snapshot"],
                "destination_identity": migration["destination_identity"],
                "adapter_version": migration["adapter_version"],
                "policy": policy,
                "items": items,
            }
            material_digest = digest(canonical_json(material))
            if material_digest != migration["plan_digest"]:
                raise ValueError("durable plan digest mismatch")
            artifact_path = self.store.artifacts_dir / f"{migration_id}.plan.json"
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            artifact_material = {key: artifact[key] for key in material}
            if artifact.get("plan_digest") != migration["plan_digest"] or canonical_json(artifact_material) != canonical_json(material):
                raise ValueError("plan artifact does not match durable approved material")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError) as exc:
            reason = str(exc) or exc.__class__.__name__
            self._fence_plan(migration_id, reason)
            raise MigrationError(f"plan integrity check failed: {reason}") from exc

    def approve(self, migration_id: str, plan_digest: str) -> dict:
        migration = self.store.migration(migration_id)
        if not migration:
            raise MigrationError("migration not found")
        if migration["state"] != "APPROVAL_REQUIRED":
            raise MigrationError(f"migration is {migration['state']}, not APPROVAL_REQUIRED")
        self._assert_plan_integrity(migration)
        if plan_digest != migration["plan_digest"]:
            raise MigrationError("plan digest mismatch")
        current_snapshot = self._current_snapshot(migration)
        if current_snapshot != migration["source_snapshot"]:
            raise MigrationError("source snapshot changed; create a new plan")
        now = utc_now()
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE migrations SET state='PLAN_READY',approved_digest=?,updated_at=? WHERE migration_id=?",
                (plan_digest, now, migration_id),
            )
            self.store.audit("PLAN_APPROVED", {"plan_digest": plan_digest}, migration_id, conn)
        return {"migration_id": migration_id, "state": "PLAN_READY", "approved_digest": plan_digest}

    @staticmethod
    def _owned_record_matches(row, item: dict, migration_id: str) -> bool:
        return bool(
            row
            and row["marker"] == item["marker"]
            and row["content_hash"] == item["content_hash"]
            and digest(row["content"]) == item["content_hash"]
            and row["migration_id"] == migration_id
            and row["source_identity"] == item["source_identity"]
            and row["created_by_migration"]
        )

    def _reconcile_or_create(self, migration_id: str, item: dict) -> str:
        operation_id = digest(f"import:{migration_id}:{item['source_identity']}")
        now = utc_now()
        request_digest = digest(item["canonical_json"])
        recovered_missing_destination = False
        conflict_message = None
        with self.store.transaction() as conn:
            existing_op = conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            marker_row = conn.execute("SELECT * FROM records WHERE marker=?", (item["marker"],)).fetchone()
            journal_mismatch = existing_op and (
                existing_op["migration_id"] != migration_id
                or existing_op["source_identity"] != item["source_identity"]
                or existing_op["marker"] != item["marker"]
                or existing_op["operation"] != "IMPORT"
                or existing_op["request_digest"] != request_digest
            )
            if journal_mismatch:
                conn.execute(
                    "UPDATE operations SET state='CONFLICT',error_code='JOURNAL_REQUEST_MISMATCH',updated_at=? WHERE operation_id=?",
                    (now, operation_id),
                )
                conflict_message = f"journal conflict for {item['source_identity']}"
            elif marker_row and not self._owned_record_matches(marker_row, item, migration_id):
                if existing_op:
                    conn.execute(
                        "UPDATE operations SET state='CONFLICT',error_code='OWNERSHIP_OR_CONTENT_MISMATCH',updated_at=? WHERE operation_id=?",
                        (now, operation_id),
                    )
                else:
                    conn.execute(
                        """INSERT INTO operations(operation_id,migration_id,source_identity,marker,operation,state,
                        request_digest,evidence_json,error_code,created_at,updated_at)
                        VALUES(?,?,?,?,?,'CONFLICT',?,'{}','OWNERSHIP_OR_CONTENT_MISMATCH',?,?)""",
                        (operation_id, migration_id, item["source_identity"], item["marker"], "IMPORT", request_digest, now, now),
                    )
                conflict_message = f"marker conflict for {item['source_identity']}"
            elif marker_row:
                if not existing_op:
                    conn.execute(
                        """INSERT INTO operations(operation_id,migration_id,source_identity,marker,operation,state,
                        request_digest,provider_record_id,evidence_json,created_at,updated_at)
                        VALUES(?,?,?,?,?,'IMPORTED',?,?,?,?,?)""",
                        (operation_id, migration_id, item["source_identity"], item["marker"], "IMPORT", request_digest,
                         marker_row["provider_record_id"], canonical_json({"reconciled": True}), now, now),
                    )
                else:
                    conn.execute(
                        """UPDATE operations SET state='IMPORTED',provider_record_id=?,evidence_json=?,
                        error_code=NULL,updated_at=? WHERE operation_id=?""",
                        (marker_row["provider_record_id"], canonical_json({"reconciled": True}), now, operation_id),
                    )
                return "RECONCILED"
            else:
                provider_row = None
                if existing_op and existing_op["provider_record_id"]:
                    provider_row = conn.execute(
                        "SELECT * FROM records WHERE provider_record_id=?", (existing_op["provider_record_id"],)
                    ).fetchone()
                if provider_row:
                    conn.execute(
                        "UPDATE operations SET state='CONFLICT',error_code='JOURNAL_PROVIDER_ID_REBOUND',updated_at=? WHERE operation_id=?",
                        (now, operation_id),
                    )
                    conflict_message = f"provider record identity conflict for {item['source_identity']}"
                elif existing_op:
                    recovered_missing_destination = existing_op["state"] == "IMPORTED"
                    conn.execute(
                        """UPDATE operations SET state='INTENT_RECORDED',provider_record_id=NULL,
                        evidence_json=?,error_code=?,updated_at=? WHERE operation_id=?""",
                        (canonical_json({"destination_missing": recovered_missing_destination}),
                         "DESTINATION_RECORD_MISSING" if recovered_missing_destination else None, now, operation_id),
                    )
                else:
                    conn.execute(
                        """INSERT INTO operations(operation_id,migration_id,source_identity,marker,operation,state,
                        request_digest,evidence_json,created_at,updated_at)
                        VALUES(?,?,?,?,?,'INTENT_RECORDED',?,'{}',?,?)""",
                        (operation_id, migration_id, item["source_identity"], item["marker"], "IMPORT", request_digest, now, now),
                    )
        if conflict_message:
            raise MigrationError(conflict_message)

        record = json.loads(item["canonical_json"])
        provider_record_id = str(uuid.uuid4())
        conflict_message = None
        with self.store.transaction() as conn:
            marker_row = conn.execute("SELECT * FROM records WHERE marker=?", (item["marker"],)).fetchone()
            if marker_row and not self._owned_record_matches(marker_row, item, migration_id):
                conn.execute(
                    "UPDATE operations SET state='CONFLICT',error_code='OWNERSHIP_OR_CONTENT_MISMATCH',updated_at=? WHERE operation_id=?",
                    (utc_now(), operation_id),
                )
                conflict_message = f"marker conflict for {item['source_identity']}"
                reconciled = False
            elif marker_row:
                provider_record_id = marker_row["provider_record_id"]
                reconciled = True
            else:
                conn.execute(
                    """INSERT INTO records(provider_record_id,marker,source_identity,content,content_hash,
                    source_target,provenance_json,labels_json,migration_id,created_by_migration,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,1,?)""",
                    (provider_record_id, item["marker"], item["source_identity"], record["content"], record["content_hash"],
                     record["source_target"], canonical_json({"source_path": record["source_path"],
                     "start_line": record["start_line"], "end_line": record["end_line"], "section": record["section"],
                     "source_mtime_ns": record["source_mtime_ns"], "unsupported_fields": record["unsupported_fields"]}),
                     canonical_json(record["labels"]), migration_id, utc_now()),
                )
                reconciled = False
            if not conflict_message:
                conn.execute(
                    """UPDATE operations SET state='IMPORTED',provider_record_id=?,evidence_json=?,error_code=NULL,updated_at=?
                    WHERE operation_id=?""",
                    (provider_record_id, canonical_json({"reconciled": reconciled,
                     "recovered_missing_destination": recovered_missing_destination}), utc_now(), operation_id),
                )
                self.store.audit("RECORD_IMPORTED",
                    {"source_identity_hash": digest(item["source_identity"]), "provider_record_id": provider_record_id},
                    migration_id, conn)
        if conflict_message:
            raise MigrationError(conflict_message)
        if recovered_missing_destination:
            return "RECREATED"
        return "RECONCILED" if reconciled else "IMPORTED"

    def run(self, migration_id: str) -> dict:
        migration = self.store.migration(migration_id)
        if not migration:
            raise MigrationError("migration not found")
        if migration["state"] not in {"PLAN_READY", "IMPORTING", "INCOMPLETE"}:
            raise MigrationError(f"migration is {migration['state']}, not runnable")
        if migration["approved_digest"] != migration["plan_digest"]:
            raise MigrationError("approved plan digest is missing or stale")
        self._assert_plan_integrity(migration)
        if self._current_snapshot(migration) != migration["source_snapshot"]:
            raise MigrationError("source snapshot changed after approval")
        with self.store.transaction() as conn:
            conn.execute("UPDATE migrations SET state='IMPORTING',updated_at=? WHERE migration_id=?", (utc_now(), migration_id))
        outcomes = Counter()
        for item in self.store.plan_items(migration_id):
            if item["decision"] != "CREATE":
                outcomes[item["decision"]] += 1
                continue
            outcomes[self._reconcile_or_create(migration_id, item)] += 1
        terminal = "VERIFYING"
        with self.store.transaction() as conn:
            conn.execute("UPDATE migrations SET state=?,updated_at=? WHERE migration_id=?", (terminal, utc_now(), migration_id))
            self.store.audit("IMPORT_FINISHED", {"outcomes": dict(outcomes)}, migration_id, conn)
        return {"migration_id": migration_id, "state": terminal, "outcomes": dict(outcomes)}

    def _default_queries(self, migration_id: str) -> list[str]:
        queries = []
        for item in self.store.plan_items(migration_id):
            if item["decision"] not in {"CREATE", "EXACT_DUPLICATE"}:
                continue
            record = json.loads(item["canonical_json"])
            unique_terms = []
            for term in tokens(str(record.get("content") or "")):
                if term not in unique_terms:
                    unique_terms.append(term)
                if len(unique_terms) >= 6:
                    break
            query = " ".join(unique_terms)
            if query and query.casefold() not in {value.casefold() for value in queries}:
                queries.append(query)
            if len(queries) >= 8:
                break
        return queries

    def verify(self, migration_id: str, queries: list[str] | None = None, limit: int = 8) -> dict:
        migration = self.store.migration(migration_id)
        if not migration:
            raise MigrationError("migration not found")
        if migration["state"] not in {"VERIFYING", "SUCCEEDED", "INCOMPLETE"}:
            raise MigrationError(f"migration is {migration['state']}, not verifiable")
        self._assert_plan_integrity(migration)
        queries = [query.strip() for query in (queries or self._default_queries(migration_id)) if query.strip()]
        if not queries:
            raise MigrationError("query corpus is empty")
        source_rows = []
        for item in self.store.plan_items(migration_id):
            if item["decision"] in {"CREATE", "EXACT_DUPLICATE"}:
                record = json.loads(item["canonical_json"])
                source_rows.append({"source_identity": item["source_identity"], "content": record["content"]})
        all_destination_rows = self.store.rows()
        rows_by_id = {row["provider_record_id"]: row for row in all_destination_rows}
        rows_by_marker = {row["marker"]: row for row in all_destination_rows}
        destination_rows = []
        missing_destination_evidence = []
        for item in self.store.plan_items(migration_id):
            if item["decision"] not in {"CREATE", "EXACT_DUPLICATE"}:
                continue
            row = rows_by_marker.get(item["marker"])
            if item["decision"] == "EXACT_DUPLICATE" and item["similar_provider_record_id"]:
                row = rows_by_id.get(item["similar_provider_record_id"])
            if (
                not row
                or row["content_hash"] != item["content_hash"]
                or digest(row["content"]) != item["content_hash"]
            ):
                missing_destination_evidence.append(digest(item["source_identity"])[:16])
                continue
            if item["decision"] == "CREATE" and (
                row["migration_id"] != migration_id
                or row["source_identity"] != item["source_identity"]
                or not row["created_by_migration"]
            ):
                missing_destination_evidence.append(digest(item["source_identity"])[:16])
                continue
            destination_rows.append({**row, "source_identity": item["source_identity"]})
        results = []
        complete = not missing_destination_evidence
        for query in queries:
            source = rank(query, source_rows, limit=limit)
            destination = rank(query, destination_rows, limit=limit)
            source_ids = [row["source_identity"] for row in source]
            destination_ids = [row["source_identity"] for row in destination]
            overlap = len(set(source_ids) & set(destination_ids)) / len(set(source_ids)) if source_ids else None
            status = "COMPARABLE"
            if not source_ids:
                status, complete = "NO_SOURCE_EVIDENCE", False
            elif overlap is None:
                status, complete = "INCOMPARABLE", False
            elif overlap < 1.0:
                status, complete = "DRIFT", False
            results.append(
                {
                    "query": query,
                    "status": status,
                    "source_count": len(source_ids),
                    "destination_count": len(destination_ids),
                    "overlap": overlap,
                    "source_identity_hashes": [digest(value)[:16] for value in source_ids],
                    "destination_identity_hashes": [digest(value)[:16] for value in destination_ids],
                }
            )
        report = {
            "status": "PASS" if complete else "INCOMPLETE",
            "query_count": len(queries),
            "queries": results,
            "missing_destination_identity_hashes": missing_destination_evidence,
            "disclosure": "Deterministic token retrieval parity does not prove semantic equivalence.",
        }
        parity_run_id = str(uuid.uuid4())
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO parity_runs(parity_run_id,migration_id,query_corpus_digest,report_json,created_at) VALUES(?,?,?,?,?)",
                (parity_run_id, migration_id, digest(canonical_json(queries)), canonical_json(report), utc_now()),
            )
            state = "SUCCEEDED" if complete else "INCOMPLETE"
            conn.execute("UPDATE migrations SET state=?,updated_at=? WHERE migration_id=?", (state, utc_now(), migration_id))
            self.store.audit("PARITY_COMPLETED", {"parity_run_id": parity_run_id, "status": report["status"]}, migration_id, conn)
        return {"migration_id": migration_id, "state": "SUCCEEDED" if complete else "INCOMPLETE", "report": report}

    def rollback(self, migration_id: str) -> dict:
        migration = self.store.migration(migration_id)
        if not migration:
            raise MigrationError("migration not found")
        self._assert_plan_integrity(migration)
        if migration["state"] == "ROLLED_BACK":
            return {"migration_id": migration_id, "state": "ROLLED_BACK", "removed": 0, "idempotent": True}
        removed = 0
        conflicts = []
        for item in self.store.plan_items(migration_id):
            if item["decision"] != "CREATE":
                continue
            operation_id = digest(f"rollback:{migration_id}:{item['source_identity']}")
            now = utc_now()
            with self.store.transaction() as conn:
                row = conn.execute("SELECT * FROM records WHERE marker=?", (item["marker"],)).fetchone()
                conn.execute(
                    """INSERT OR IGNORE INTO operations(operation_id,migration_id,source_identity,marker,operation,
                    state,request_digest,evidence_json,created_at,updated_at)
                    VALUES(?,?,?,?,?,'INTENT_RECORDED',?,'{}',?,?)""",
                    (
                        operation_id,
                        migration_id,
                        item["source_identity"],
                        item["marker"],
                        "ROLLBACK",
                        digest(f"rollback:{item['marker']}"),
                        now,
                        now,
                    ),
                )
                if not row:
                    conn.execute(
                        "UPDATE operations SET state='ROLLBACK_NOT_APPLIED',evidence_json=?,updated_at=? WHERE operation_id=?",
                        (canonical_json({"marker_absent": True}), now, operation_id),
                    )
                    continue
                if row["migration_id"] != migration_id or not row["created_by_migration"] or row["source_identity"] != item["source_identity"]:
                    conn.execute(
                        "UPDATE operations SET state='ROLLBACK_CONFLICT',error_code='OWNERSHIP_NOT_PROVEN',updated_at=? WHERE operation_id=?",
                        (now, operation_id),
                    )
                    conflicts.append(item["source_identity"])
                    continue
                conn.execute("DELETE FROM records WHERE provider_record_id=?", (row["provider_record_id"],))
                conn.execute(
                    "UPDATE operations SET state='ROLLED_BACK',provider_record_id=?,evidence_json=?,updated_at=? WHERE operation_id=?",
                    (row["provider_record_id"], canonical_json({"ownership_proven": True}), now, operation_id),
                )
                removed += 1
        state = "ROLLBACK_CONFLICT" if conflicts else "ROLLED_BACK"
        with self.store.transaction() as conn:
            conn.execute("UPDATE migrations SET state=?,updated_at=? WHERE migration_id=?", (state, utc_now(), migration_id))
            self.store.audit("ROLLBACK_FINISHED", {"removed": removed, "conflict_count": len(conflicts)}, migration_id, conn)
        return {
            "migration_id": migration_id,
            "state": state,
            "removed": removed,
            "conflict_identity_hashes": [digest(value)[:16] for value in conflicts],
        }

