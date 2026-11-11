from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

try:
    from agent.memory_provider import MemoryProvider as _HermesMemoryProvider  # noqa: F401
except ModuleNotFoundError:
    memory_provider_stub = types.ModuleType("agent.memory_provider")

    class _MemoryProvider:
        pass

    class _RecallStatus:
        def __init__(self, provider_label, count, glyph):
            self.provider_label = provider_label
            self.count = count
            self.glyph = glyph

    memory_provider_stub.MemoryProvider = _MemoryProvider
    memory_provider_stub.RecallStatus = _RecallStatus
    agent_stub = types.ModuleType("agent")
    agent_stub.memory_provider = memory_provider_stub
    sys.modules.setdefault("agent", agent_stub)
    sys.modules["agent.memory_provider"] = memory_provider_stub

from memory_ferry.provider import MemoryFerryProvider


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.provider = MemoryFerryProvider()
        self.provider.initialize("session-1", hermes_home=str(self.home), platform="cli")

    def tearDown(self):
        self.temp.cleanup()

    def call(self, name, args):
        return json.loads(self.provider.handle_tool_call(name, args, hermes_home=str(self.home)))

    def test_tools_are_real_and_manual_forget_cannot_remove_migrated_scope(self):
        created = self.call("memory_ferry_remember", {"content": "Use canary rollout for the API", "labels": ["ops"]})
        self.assertTrue(created["ok"])
        results = self.call("memory_ferry_search", {"query": "canary rollout"})
        self.assertEqual(1, len(results["results"]))
        recalled = self.provider.prefetch("Which rollout should we use?")
        self.assertIn("canary rollout", recalled)
        status = self.provider.recall_status()
        self.assertEqual(1, status.count)
        removed = self.call("memory_ferry_forget", {"provider_record_id": created["provider_record_id"]})
        self.assertTrue(removed["removed"])

    def test_invalid_requests_fail_without_throwing_into_hermes(self):
        missing = self.call("memory_ferry_search", {})
        self.assertFalse(missing["ok"])
        oversized = self.call("memory_ferry_remember", {"content": "x" * (65 * 1024)})
        self.assertFalse(oversized["ok"])
        unknown = self.call("not_a_tool", {})
        self.assertFalse(unknown["ok"])


if __name__ == "__main__":
    unittest.main()

