from __future__ import annotations

import ast
import hashlib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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


def _enclosing_function(node, parents):
    cur = node
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
    return None


def _method_calls(path: Path):
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    rows = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            ch = _chain(n.func)
            if ch:
                rows.append((_enclosing_function(n, parents), ".".join(ch), n.lineno))
    return text, rows


class M1C3ASnapshotReadTests(unittest.TestCase):
    def test_readiness_uses_provider_snapshot_not_legacy_full_tick(self):
        text, rows = _method_calls(ROOT / "core" / "service.py")
        self.assertEqual(
            sum(1 for fn, call, _ in rows if fn == "readiness_check" and call == "self.market_provider.get_snapshot"),
            1,
        )
        self.assertEqual(
            sum(1 for fn, call, _ in rows if fn == "readiness_check" and call == "self.market.full_tick"),
            0,
        )
        self.assertIn("self.market_provider.get_snapshot([s])[s]", text)

    def test_adapter_snapshot_remains_exact_legacy_delegate(self):
        path = ROOT / "core" / "providers" / "xtquant_market_data.py"
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "XtQuantMarketDataProvider"]
        self.assertEqual(len(classes), 1)
        methods = [n for n in classes[0].body if isinstance(n, ast.FunctionDef) and n.name == "get_snapshot"]
        self.assertEqual(len(methods), 1)
        fn = methods[0]
        self.assertEqual(len(fn.body), 1)
        self.assertIsInstance(fn.body[0], ast.Return)
        ret = fn.body[0].value
        self.assertIsInstance(ret, ast.DictComp)
        self.assertEqual(ast.unparse(ret.key), "symbol")
        self.assertEqual(ast.unparse(ret.value.func), "self._market.full_tick")
        self.assertEqual([ast.unparse(a) for a in ret.value.args], ["symbol"])
        self.assertEqual(ast.unparse(ret.generators[0].target), "symbol")
        self.assertEqual(ast.unparse(ret.generators[0].iter), "symbols")

    def test_execution_surface_is_frozen(self):
        path = ROOT / "core" / "execution.py"
        h = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(h, "f5657af60492fed878bc99b2c8b0d8abdbe06d982349f4650bddae664c988851")


if __name__ == "__main__":
    unittest.main()
