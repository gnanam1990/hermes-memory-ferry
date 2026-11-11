# Catalog risk disclosures

- **Network:** none.
- **Files read outside plugin data:** the active profile's `MEMORY.md` and `USER.md`, only when the operator runs migration commands.
- **Files written:** SQLite database and migration/report artifacts under `$HERMES_HOME/plugin-data/memory-ferry/`.
- **Dry-run content:** plan and canonical-export artifacts and SQLite `plan_records` contain full source text, including records blocked from destination memory by the default secret-like-content policy. Artifacts are created with mode `0600`; the database stays inside the profile-local data directory, whose permissions the operator must protect.
- **Shell commands/subprocesses:** none.
- **Background processes:** none.
- **Credentials:** none read, stored, refreshed, or written.
- **Model disclosure:** provider recall/search snippets are inserted into Hermes model context and therefore sent to the operator's configured model provider.
- **Destructive behavior:** `rollback` deletes only records whose exact marker, migration ID, source identity, and ownership flag prove they came from the selected migration. `memory_ferry_forget` deletes only manual records.
- **Telemetry:** none.
- **External provider support:** none; this release migrates only built-in Markdown memory into the local Memory Ferry provider.

