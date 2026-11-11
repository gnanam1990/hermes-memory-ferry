"""Convention-based `hermes memory-ferry` CLI surface."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .memory_ferry.migration import MigrationError, MigrationService
from .memory_ferry.models import Policy
from .memory_ferry.report import generate_report


def _home(args) -> Path:
    override = getattr(args, "hermes_home", None)
    if override:
        return Path(override).expanduser().resolve()
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home()).resolve()
    except Exception:
        return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser().resolve()


def _emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _command(args) -> None:
    try:
        service = MigrationService(_home(args))
        action = args.memory_ferry_action
        if action == "preflight":
            result = service.preflight()
        elif action == "plan":
            targets = tuple(part.strip() for part in args.source.split(",") if part.strip())
            result = service.create_plan(Policy(allow_sensitive=args.allow_sensitive, include_targets=targets))
        elif action == "approve":
            result = service.approve(args.migration_id, args.plan_digest)
        elif action in {"run", "resume"}:
            result = service.run(args.migration_id)
        elif action == "verify":
            result = service.verify(args.migration_id, args.query or None)
        elif action == "rollback":
            result = service.rollback(args.migration_id)
        elif action == "status":
            result = service.store.status(args.migration_id)
        elif action == "report":
            result = {"report": str(generate_report(service.store, args.migration_id, Path(args.output).resolve() if args.output else None))}
        else:
            raise MigrationError(f"unknown action: {action}")
        _emit(result)
        if result.get("status") == "UNSUPPORTED" or result.get("state") in {"INCOMPLETE", "ROLLBACK_CONFLICT"}:
            raise SystemExit(2)
    except (MigrationError, ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(2)


def register_cli(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hermes-home", help="Explicit profile home (testing/admin use).")
    subs = parser.add_subparsers(dest="memory_ferry_action", required=True)
    subs.add_parser("preflight", help="Probe the real built-in source and local destination contracts.")
    plan = subs.add_parser("plan", help="Create canonical export and immutable dry-run plan.")
    plan.add_argument("--source", default="memory,user", help="Comma-separated built-in targets: memory,user")
    plan.add_argument("--allow-sensitive", action="store_true", help="Explicitly include records matching secret-like patterns.")
    approve = subs.add_parser("approve", help="Approve one exact plan digest.")
    approve.add_argument("migration_id")
    approve.add_argument("plan_digest")
    for name in ("run", "resume", "rollback"):
        child = subs.add_parser(name, help=f"{name.title()} a migration safely.")
        child.add_argument("migration_id")
    verify = subs.add_parser("verify", help="Run deterministic retrieval-parity queries.")
    verify.add_argument("migration_id")
    verify.add_argument("--query", action="append", help="Query corpus item; repeat for multiple queries.")
    status = subs.add_parser("status", help="Show redacted durable state.")
    status.add_argument("migration_id", nargs="?")
    report = subs.add_parser("report", help="Generate an accessible local HTML evidence report.")
    report.add_argument("migration_id")
    report.add_argument("--output")
    parser.set_defaults(func=_command)

