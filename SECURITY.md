# Security model

## Trust boundaries

Built-in memory text, headings, Unicode, query text, CLI arguments, and SQLite contents are untrusted data. They are never executed as instructions or shell content. HTML report fields are escaped. Before import, the durable plan is re-bound to its approved digest and local plan artifact, and every canonical record is checked against its durable identity columns. A local attacker able to rewrite both the database and the `0600` artifact remains inside the host trust boundary; this plugin does not provide signed approval receipts.

## Source preservation

The source adapter opens `MEMORY.md` and `USER.md` read-only. No supported command writes or deletes them.

## Destination ownership

Migrated rows carry an immutable marker, migration ID, source identity, and `created_by_migration=1`. Rollback requires all four to agree. Content similarity is never ownership evidence.

## Secrets

The plugin has no credential requirement and reads no `.env`, Hermes auth state, browser profile, keychain, vendor CLI token file, or external credential store. Secret-like source text is blocked from becoming provider memory and from recall by default. It is still copied verbatim into the profile-local `0600` dry-run plan and canonical-export artifacts and the SQLite `plan_records` journal for exact operator review. Explicit inclusion is an operator decision and may expose snippets to the configured model provider during recall.

## Resource limits

- 64 KiB maximum canonical record size
- 10-result maximum model tool search
- bounded default query corpus and retrieval limit
- SQLite busy timeout and immediate write transactions
- profile-local storage only

## Known limits

- Regex sensitivity detection cannot find every secret.
- Local filesystem compromise can expose plaintext memory artifacts.
- Deterministic token overlap is not semantic equivalence.
- A disk or filesystem failure during SQLite commit may still need operator investigation; no result is promoted from unknown evidence.
- The implementation has not received an independent production security audit.

