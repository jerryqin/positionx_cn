from __future__ import annotations

import ast
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PROJECT_ROOT / "gui" / "dashboard.py"
FUNCTION = "_refresh_view"


def _chain(node):
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return tuple(reversed(parts))
    return None


def _parents(root):
    out = {}
    for parent in ast.walk(root):
        for child in ast.iter_child_nodes(parent):
            out[child] = parent
    return out


def _ancestors(node, parents):
    cur = node
    while cur in parents:
        cur = parents[cur]
        yield cur


def _has_name(node, name):
    return any(isinstance(x, ast.Name) and x.id == name for x in ast.walk(node))


class DashboardBrokerProviderReadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = DASHBOARD.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)
        funcs = [n for n in ast.walk(cls.tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == FUNCTION]
        if len(funcs) != 1:
            raise AssertionError(f"expected exactly one {FUNCTION}, found {len(funcs)}")
        cls.fn = funcs[0]
        cls.parents = _parents(cls.fn)

    def _calls(self):
        rows = []
        for n in ast.walk(self.fn):
            if isinstance(n, ast.Call):
                chain = _chain(n.func)
                if chain:
                    rows.append((chain, n))
        return rows

    def test_legacy_direct_broker_reads_removed(self):
        chains = [c for c, _ in self._calls()]
        self.assertNotIn(("self", "service", "broker", "query_account"), chains)
        self.assertNotIn(("self", "service", "broker", "query_positions"), chains)

    def test_exact_provider_reads_present(self):
        chains = [c for c, _ in self._calls()]
        self.assertEqual(chains.count(("self", "service", "broker_provider", "get_account_snapshot")), 1)
        self.assertEqual(chains.count(("self", "service", "broker_provider", "get_positions")), 1)

    def test_provider_reads_share_qmt_guard_and_try(self):
        wanted = {
            ("self", "service", "broker_provider", "get_account_snapshot"),
            ("self", "service", "broker_provider", "get_positions"),
        }
        nodes = [n for c, n in self._calls() if c in wanted]
        self.assertEqual(len(nodes), 2)
        contexts = []
        for node in nodes:
            t = None
            i = None
            for anc in _ancestors(node, self.parents):
                if t is None and isinstance(anc, ast.Try):
                    t = anc
                if i is None and isinstance(anc, ast.If) and _has_name(anc.test, "qmt_connected"):
                    i = anc
            self.assertIsNotNone(t)
            self.assertIsNotNone(i)
            contexts.append((t, i))
        self.assertIs(contexts[0][0], contexts[1][0])
        self.assertIs(contexts[0][1], contexts[1][1])
        self.assertEqual(ast.unparse(contexts[0][1].test), "qmt_connected")
        self.assertGreaterEqual(len(contexts[0][0].handlers), 1)

    def test_no_other_service_broker_call_in_refresh_view(self):
        chains = [c for c, _ in self._calls()]
        legacy = [c for c in chains if len(c) >= 3 and c[:3] == ("self", "service", "broker")]
        self.assertEqual(legacy, [])


if __name__ == "__main__":
    unittest.main()
