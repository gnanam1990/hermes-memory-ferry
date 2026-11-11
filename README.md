# Memory Ferry

Memory Ferry is a local Hermes memory provider that safely migrates the active profile's built-in `MEMORY.md` and `USER.md` records into a provenance-preserving SQLite store. It creates a canonical JSONL export, shows an immutable dry-run plan, requires an exact digest approval, imports with unique migration markers, evaluates deterministic retrieval parity, and rolls back only records proven to belong to that migration.

## Honest scope

Version 0.1.0 supports one real migration route:

```text
Hermes built-in MEMORY.md + USER.md
                 ↓
Memory Ferry local provider store
```

It does **not** claim arbitrary external-provider portability. Hermes exposes one active external memory provider at a time and does not expose a public bulk export API for every third-party provider. Memory Ferry never reads another provider's database, API token, keychain, or credential store.

## Why this is safer than copy/paste

- The source files are read-only and never rewritten.
- Every record keeps source file, line span, section, content hash, source mtime, and explicit unsupported metadata.
- Secret-like content is blocked from provider import and model recall unless the operator explicitly opts in.
- Approval binds source snapshot, destination identity, adapter version, policy, and every record decision.
- `UNIQUE(marker)` gives the destination a stable idempotency key.
- Import intent is durable before the mutation; a restart reconciles by exact marker before creating.
- Rollback requires marker, migration ID, source identity, and `created_by_migration` ownership proof.
- Parity reports missing or incomparable evidence as `INCOMPLETE`, never success.

## Requirements

- Hermes Agent **0.23.1 or later**
- Python 3.11 or later (Hermes supplies it)
- No network service, account, API key, or Python dependency

## Activate a local development checkout

Use a non-production profile first:

```bash
mkdir -p "$HERMES_HOME/plugins"
ln -s /absolute/path/to/hermes-memory-ferry-product \
  "$HERMES_HOME/plugins/memory-ferry"
hermes memory setup memory-ferry
```

The symlink is a development workflow, not a catalog install. After catalog acceptance, use `hermes plugins install memory-ferry --enable`. `hermes memory --help` is authoritative for the installed release.

## Operator workflow

### 1. Preflight

```bash
hermes memory-ferry preflight
```

The command returns `SUPPORTED` only when at least one built-in memory file is readable and the local destination contract is ready.

### 2. Create a dry-run plan

```bash
hermes memory-ferry plan
```

The output contains `migration_id`, `plan_digest`, counts, canonical JSONL path, and plan path. Full content stays in profile-local artifacts with mode `0600`; terminal output shows identities and counts rather than memory text.

Sensitive-looking records are blocked by default. Review the source yourself before using the explicit override:

```bash
hermes memory-ferry plan --allow-sensitive
```

The local `0600` plan and canonical-export artifacts, plus the SQLite `plan_records` journal, contain the complete source text, including policy-blocked records, so the operator can audit the exact proposed migration. The default block prevents those records from becoming provider memory or model recall results; it does not redact local planning evidence. Protect the profile directory accordingly.

### 3. Approve the exact plan

```bash
hermes memory-ferry approve MIGRATION_ID PLAN_DIGEST
```

Approval fails if the source snapshot changed after planning.

### 4. Import and verify

```bash
hermes memory-ferry run MIGRATION_ID
hermes memory-ferry verify MIGRATION_ID \
  --query "deployment preferences" \
  --query "project decisions"
```

`verify` exits non-zero when evidence is incomplete or retrieval drift is observed.

### 5. Generate the evidence UI

```bash
hermes memory-ferry report MIGRATION_ID
```

Open the emitted local HTML file. It is responsive, keyboard-readable, reduced-motion aware, and derived from the authoritative SQLite journal. It never embeds memory content.

### 6. Roll back only the imported scope

```bash
hermes memory-ferry rollback MIGRATION_ID
```

Rollback never touches `MEMORY.md`, `USER.md`, manual Memory Ferry records, or records from another migration.

## Agent tools

When `memory.provider: memory-ferry` is active, Hermes receives:

- `memory_ferry_search` — deterministic local retrieval with provenance-aware snippets
- `memory_ferry_status` — redacted record/migration status
- `memory_ferry_remember` — add a manual local record
- `memory_ferry_forget` — remove only a manual record; migrated records require operator rollback

The provider also returns up to five locally ranked snippets through normal memory prefetch.

## Local state

All data stays under the active profile:

```text
$HERMES_HOME/plugin-data/memory-ferry/
├── memory-ferry.sqlite3
└── artifacts/
    ├── <migration>.canonical.jsonl
    ├── <migration>.plan.json
    └── <migration>.report.html
```

Back up the profile before production use. SQLite WAL files may exist while Hermes is running.

## Validation and tests

```bash
PYTHONWARNINGS=error python3 -m unittest discover -s tests -v
hermes plugins validate /absolute/path/to/hermes-memory-ferry-product --install-deps
hermes plugins doctor /absolute/path/to/hermes-memory-ferry-product --ci
```

The test suite uses temporary real files and real SQLite transactions. It does not call a mock remote provider because no remote provider is part of the supported route.

## Privacy and disclosures

Memory text is stored locally in SQLite and canonical JSONL. Search and prefetch return snippets into the model context, which means those snippets reach the configured model provider. The plugin reads only the active profile's `MEMORY.md` and `USER.md`; writes only its own `plugin-data/memory-ferry` directory; makes no network calls; spawns no shell processes; has no telemetry; stores no credentials; and has no background process.

See [SECURITY.md](SECURITY.md) and [DISCLOSURES.md](DISCLOSURES.md).

