from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from memory_ferry.migration import MigrationError, MigrationService
from memory_ferry.models import Policy


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.memory_dir = self.home / "memories"
        self.memory_dir.mkdir()
        (self.memory_dir / "MEMORY.md").write_text(
            "# Deployments\nUse blue-green releases for payments.\n\nKeep a rollback window."
            "\n§\n"
            "# Reliability\nRetries must be idempotent.\n",
            encoding="utf-8",
        )
        (self.memory_dir / "USER.md").write_text(
            "# Preferences\nPrefer concise evidence-led reports."
            "\n§\n"
            "# Private\nAPI key should never be copied.\n",
            encoding="utf-8",
        )
        self.service = MigrationService(self.home)

    def tearDown(self):
        self.temp.cleanup()

    def _approved(self):
        plan = self.service.create_plan()
        self.service.approve(plan["migration_id"], plan["plan_digest"])
        return plan

    def test_preflight_is_real_and_scope_is_narrow(self):
        # A legacy/root-level decoy must not be treated as active Hermes memory.
        (self.home / "MEMORY.md").write_text("ROOT DECOY", encoding="utf-8")
        result = self.service.preflight()
        self.assertEqual("SUPPORTED", result["status"])
        self.assertEqual("builtin-memory-to-memory-ferry-local-provider", result["scope"])
        self.assertTrue(result["destination"]["immutable_marker_lookup"])
        self.assertFalse(result["source"]["delete"])
        self.assertEqual(
            [
                str((self.memory_dir / "MEMORY.md").resolve()),
                str((self.memory_dir / "USER.md").resolve()),
            ],
            [item["path"] for item in result["source"]["files"]],
        )

        records = self.service.source.records()
        self.assertEqual(4, len(records))
        deployment = next(record for record in records if "blue-green" in record.content)
        self.assertEqual(
            "# Deployments\nUse blue-green releases for payments.\n\nKeep a rollback window.",
            deployment.content,
        )
        self.assertEqual("memories/MEMORY.md", deployment.source_path)
        self.assertEqual((1, 4), (deployment.start_line, deployment.end_line))
        self.assertEqual("Deployments", deployment.section)
        self.assertNotIn("ROOT DECOY", "\n".join(record.content for record in records))
        self.assertTrue(all("§" not in record.content for record in records))

        # Mirror Hermes's exact parser edge cases without adding alternate split rules.
        with tempfile.TemporaryDirectory() as directory:
            edge_home = Path(directory)
            edge_memory = edge_home / "memories"
            edge_memory.mkdir()
            (edge_memory / "MEMORY.md").write_text(
                "First paragraph.\n\n§\n\nSecond paragraph with a bare § character.",
                encoding="utf-8",
            )
            (edge_memory / "USER.md").write_text(
                "One whole file.\n\nStill the same entry.", encoding="utf-8"
            )
            edge_records = MigrationService(edge_home).source.records()
            self.assertEqual(
                [
                    "First paragraph.",
                    "Second paragraph with a bare § character.",
                    "One whole file.\n\nStill the same entry.",
                ],
                [record.content for record in edge_records],
            )

    def test_plan_blocks_sensitive_record_and_exports_canonical_jsonl(self):
        plan = self.service.create_plan()
        self.assertEqual("APPROVAL_REQUIRED", plan["state"])
        self.assertEqual(3, plan["counts"]["CREATE"])
        self.assertEqual(1, plan["counts"]["BLOCKED_POLICY"])
        lines = Path(plan["canonical_export"]).read_text(encoding="utf-8").splitlines()
        self.assertEqual(4, len(lines))
        first = json.loads(lines[0])
        self.assertIn("source_identity", first)
        self.assertIn("unsupported_fields", first)

    def test_source_mutation_invalidates_approval(self):
        plan = self.service.create_plan()
        with (self.memory_dir / "MEMORY.md").open("a", encoding="utf-8") as handle:
            handle.write("\nA late change.\n")
        with self.assertRaisesRegex(MigrationError, "source snapshot changed"):
            self.service.approve(plan["migration_id"], plan["plan_digest"])

    def test_import_is_idempotent_parity_passes_and_rollback_is_isolated(self):
        manual = self.service.store.remember("Unrelated manual memory", ["manual"])
        plan = self._approved()
        run = self.service.run(plan["migration_id"])
        self.assertEqual("VERIFYING", run["state"])
        self.assertEqual(3, run["outcomes"]["IMPORTED"])
        before = self.service.store.rows()
        self.assertEqual(4, len(before))

        # Reconcile every import operation again: the exact marker must prevent a second record.
        for item in self.service.store.plan_items(plan["migration_id"]):
            if item["decision"] == "CREATE":
                self.service._reconcile_or_create(plan["migration_id"], item)
        self.assertEqual(4, len(self.service.store.rows()))

        verified = self.service.verify(plan["migration_id"], ["blue green", "idempotent retries", "concise reports"])
        self.assertEqual("SUCCEEDED", verified["state"])
        self.assertEqual("PASS", verified["report"]["status"])

        rolled = self.service.rollback(plan["migration_id"])
        self.assertEqual("ROLLED_BACK", rolled["state"])
        remaining = self.service.store.rows()
        self.assertEqual([manual["provider_record_id"]], [row["provider_record_id"] for row in remaining])
        self.assertTrue((self.memory_dir / "MEMORY.md").exists())
        self.assertIn("blue-green", (self.memory_dir / "MEMORY.md").read_text(encoding="utf-8"))

    def test_missing_destination_evidence_is_incomplete_not_success(self):
        plan = self._approved()
        self.service.run(plan["migration_id"])
        row = next(
            row
            for row in self.service.store.rows()
            if row["migration_id"] == plan["migration_id"] and "blue-green" in row["content"]
        )
        with self.service.store.transaction() as conn:
            conn.execute("DELETE FROM records WHERE provider_record_id=?", (row["provider_record_id"],))
        result = self.service.verify(plan["migration_id"], ["blue green"])
        self.assertEqual("INCOMPLETE", result["state"])
        self.assertEqual("INCOMPLETE", result["report"]["status"])

        resumed = self.service.run(plan["migration_id"])
        self.assertEqual("VERIFYING", resumed["state"])
        self.assertEqual(1, resumed["outcomes"]["RECREATED"])
        self.assertEqual(3, len([item for item in self.service.store.rows() if item["migration_id"] == plan["migration_id"]]))
        verified = self.service.verify(plan["migration_id"], ["blue green"])
        self.assertEqual("SUCCEEDED", verified["state"])

    def test_resume_fences_rebound_provider_record_identity(self):
        plan = self._approved()
        self.service.run(plan["migration_id"])
        row = next(row for row in self.service.store.rows() if row["migration_id"] == plan["migration_id"])
        with self.service.store.transaction() as conn:
            conn.execute("UPDATE records SET marker=? WHERE provider_record_id=?", ("mf:tampered:marker", row["provider_record_id"]))
            conn.execute("UPDATE migrations SET state='INCOMPLETE' WHERE migration_id=?", (plan["migration_id"],))
        with self.assertRaisesRegex(MigrationError, "provider record identity conflict"):
            self.service.run(plan["migration_id"])
        operation = next(
            item
            for item in self.service.store.operations(plan["migration_id"])
            if item["provider_record_id"] == row["provider_record_id"]
        )
        self.assertEqual("CONFLICT", operation["state"])
        self.assertEqual("JOURNAL_PROVIDER_ID_REBOUND", operation["error_code"])

    def test_explicit_sensitive_opt_in_is_bound_into_plan(self):
        plan = self.service.create_plan(Policy(allow_sensitive=True))
        self.assertEqual(4, plan["counts"]["CREATE"])
        migration = self.service.store.migration(plan["migration_id"])
        self.assertTrue(json.loads(migration["policy_json"])["allow_sensitive"])

    def test_default_verify_uses_only_comparable_record_content(self):
        plan = self._approved()
        self.service.run(plan["migration_id"])
        verified = self.service.verify(plan["migration_id"])
        self.assertEqual("SUCCEEDED", verified["state"])
        self.assertEqual("PASS", verified["report"]["status"])

    def test_exact_manual_duplicate_is_validated_as_destination_evidence(self):
        duplicate = self.service.store.remember(
            "# Deployments\nUse blue-green releases for payments.\n\nKeep a rollback window.",
            ["manual"],
        )
        plan = self._approved()
        self.assertEqual(1, plan["counts"]["EXACT_DUPLICATE"])
        run = self.service.run(plan["migration_id"])
        self.assertEqual(1, run["outcomes"]["EXACT_DUPLICATE"])
        verified = self.service.verify(plan["migration_id"], ["blue green"])
        self.assertEqual("SUCCEEDED", verified["state"])
        self.assertEqual([], verified["report"]["missing_destination_identity_hashes"])

        self.service.store.forget_manual(duplicate["provider_record_id"])
        incomplete = self.service.verify(plan["migration_id"], ["blue green"])
        self.assertEqual("INCOMPLETE", incomplete["state"])
        self.assertEqual(1, len(incomplete["report"]["missing_destination_identity_hashes"]))

    def test_approved_plan_tampering_is_fenced_before_import(self):
        mutations = {
            "canonical_json": json.dumps({"content": "UNAPPROVED"}),
            "decision": "BLOCKED_POLICY",
            "reason": "tampered-reason",
            "marker": "mf:v1:tampered",
            "content_hash": "0" * 64,
        }
        for column, value in mutations.items():
            with self.subTest(column=column):
                with tempfile.TemporaryDirectory() as directory:
                    home = Path(directory)
                    (home / "memories").mkdir()
                    (home / "memories" / "MEMORY.md").write_text(
                        "# Safe\nApproved content.\n", encoding="utf-8"
                    )
                    service = MigrationService(home)
                    plan = service.create_plan()
                    service.approve(plan["migration_id"], plan["plan_digest"])
                    with service.store.transaction() as conn:
                        conn.execute(
                            f"UPDATE plan_records SET {column}=? WHERE migration_id=?",
                            (value, plan["migration_id"]),
                        )
                    with self.assertRaisesRegex(MigrationError, "plan integrity check failed"):
                        service.run(plan["migration_id"])
                    self.assertEqual([], service.store.rows())
                    self.assertEqual("PLAN_INTEGRITY_FAILED", service.store.migration(plan["migration_id"])["state"])

    def test_blocked_secret_cannot_be_flipped_to_create_after_approval(self):
        plan = self._approved()
        self.assertEqual(1, plan["counts"]["BLOCKED_POLICY"])
        with self.service.store.transaction() as conn:
            conn.execute(
                "UPDATE plan_records SET decision='CREATE' WHERE migration_id=? AND decision='BLOCKED_POLICY'",
                (plan["migration_id"],),
            )
        with self.assertRaisesRegex(MigrationError, "plan integrity check failed"):
            self.service.run(plan["migration_id"])
        self.assertEqual([], self.service.store.rows())


if __name__ == "__main__":
    unittest.main()

