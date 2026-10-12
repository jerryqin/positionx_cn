from __future__ import annotations

import ast
import hashlib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def class_method(path: Path, class_name: str, method_name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name]
    assert len(classes) == 1
    methods = [n for n in classes[0].body if isinstance(n, ast.FunctionDef) and n.name == method_name]
    assert len(methods) == 1
    return methods[0]


def call_chains(path: Path):
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    parents = {}
    for p in ast.walk(tree):
        for c in ast.iter_child_nodes(p):
            parents[c] = p

    def chain(node):
        parts = []
        cur = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            return ".".join(reversed(parts))
        return None

    def enclosing(node):
        cur = node
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur.name
        return None

    rows = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            ch = chain(n.func)
            if ch:
                rows.append((enclosing(n), ch, ast.get_source_segment(text, n) or ""))
    return rows


class M1C3BSemanticBridgeTests(unittest.TestCase):
    def test_neutral_provider_exposes_semantic_bridges(self):
        path = ROOT / "core" / "providers" / "market_data.py"
        for name in ("history", "has_completed_daily_bar", "is_live_session"):
            class_method(path, "MarketDataProvider", name)

    def test_xtquant_adapter_delegates_semantic_bridges(self):
        path = ROOT / "core" / "providers" / "xtquant_market_data.py"
        expected = {
            "history": "self._market.history",
            "has_completed_daily_bar": "self._market.has_completed_daily_bar",
            "is_live_session": "self._market.is_live_session",
        }
        for name, delegate in expected.items():
            fn = class_method(path, "XtQuantMarketDataProvider", name)
            self.assertEqual(len(fn.body), 1)
            self.assertIsInstance(fn.body[0], ast.Return)
            self.assertEqual(ast.unparse(fn.body[0].value.func), delegate)

    def test_service_active_runtime_reads_use_provider(self):
        rows = call_chains(ROOT / "core" / "service.py")
        self.assertEqual(sum(ch == "self.market_provider.history" for _, ch, _ in rows), 1)
        self.assertEqual(sum(ch == "self.market.history" for _, ch, _ in rows), 0)
        self.assertEqual(sum(ch == "self.market_provider.is_live_session" for _, ch, _ in rows), 3)
        self.assertEqual(sum(ch == "self.market.is_live_session" for _, ch, _ in rows), 0)

    def test_scheduler_active_runtime_reads_use_provider(self):
        rows = call_chains(ROOT / "core" / "scheduler.py")
        self.assertEqual(sum(ch == "self.service.market_provider.is_live_session" for _, ch, _ in rows), 1)
        self.assertEqual(sum(ch == "self.service.market.is_live_session" for _, ch, _ in rows), 0)
        self.assertEqual(sum(ch == "self.service.market_provider.has_completed_daily_bar" for _, ch, _ in rows), 1)
        self.assertEqual(sum(ch == "self.service.market.has_completed_daily_bar" for _, ch, _ in rows), 0)

    def test_lifecycle_refresh_remains_legacy_and_unmigrated(self):
        rows = call_chains(ROOT / "core" / "service.py")
        self.assertEqual(sum(ch == "self.market.refresh_completed_daily_bar" for _, ch, _ in rows), 1)
        self.assertEqual(sum(ch == "self.market_provider.refresh_completed_daily_bar" for _, ch, _ in rows), 0)

    def test_m1c4_execution_surface_remains_frozen(self):
        path = ROOT / "core" / "execution.py"
        self.assertEqual(
            hashlib.sha256(path.read_bytes()).hexdigest(),
            "f5657af60492fed878bc99b2c8b0d8abdbe06d982349f4650bddae664c988851",
        )


if __name__ == "__main__":
    unittest.main()
