# Build evidence

Evidence captured on 2026-11-11 against Hermes Agent upstream commit `5df966a97a2d5fe37a309266ea97acb0c856942d` (release line 0.23.1).

## Reproducible checks

```bash
PYTHONWARNINGS=error python3 -m unittest discover -s tests -v
# 13 tests, OK

PYTHONWARNINGS=error \
PYTHONPATH=/Users/kratos/Documents/Codex/2026-11-11/hermes-agent-catalog-submission \
python3 -m unittest discover -s tests -v
# 13 tests, OK against the real MemoryProvider contract

python3 -m compileall -q .
python3 -m py_compile __init__.py cli.py memory_ferry/*.py tests/*.py
# exit 0

uv run --project /Users/kratos/Documents/Codex/2026-11-11/hermes-agent-catalog-submission \
  python /Users/kratos/Documents/Codex/2026-11-11/hermes-agent-catalog-submission/hermes \
  plugins validate "$PWD" --install-deps
# Validation passed

uv run --project /Users/kratos/Documents/Codex/2026-11-11/hermes-agent-catalog-submission \
  python /Users/kratos/Documents/Codex/2026-11-11/hermes-agent-catalog-submission/hermes \
  plugins doctor "$PWD" --ci
# OK: runtime discovery, manifest parsing, import, and registration passed
```

`ruff` was not installed in the isolated upstream environment, so no ruff result is claimed.

## Clean-profile proof

A temporary profile activated the plugin through the current Hermes memory loader. The real CLI completed:

```text
memory status    -> installed, available, active
preflight        -> SUPPORTED
plan             -> 3 CREATE, 1 BLOCKED_POLICY
approve          -> PLAN_READY with exact digest
run              -> 3 IMPORTED, 1 BLOCKED_POLICY
verify           -> SUCCEEDED / PASS, 3 deterministic queries at overlap 1.0
report           -> mode-0600 standalone HTML
provider load    -> available; status count 3; real search hit 1; prefetch hit true
rollback         -> ROLLED_BACK, 3 removed, 0 conflicts
source hashes    -> unchanged before and after rollback
```

The test profile was `/private/tmp/memory-ferry-e2e.qVhAQx`; it is disposable evidence, not product state.

## Honest boundary

Submission validation is green for the implemented built-in-memory to local-provider route. Arbitrary external-provider migration, remote deployment, catalog publication, production security audit, and semantic-equivalence proof are not implemented or claimed.
