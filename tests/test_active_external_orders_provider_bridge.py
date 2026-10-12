from __future__ import annotations

import ast
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = PROJECT_ROOT / "core" / "providers" / "broker.py"
ADAPTER = PROJECT_ROOT / "core" / "providers" / "xtquant_broker.py"
SERVICE = PROJECT_ROOT / "core" / "service.py"
LEGACY = PROJECT_ROOT / "core" / "broker.py"


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


def _class(tree, name):
    rows = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name]
    assert len(rows) == 1, rows
    return rows[0]


def _method(cls, name):
    rows = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(rows) == 1, rows
    return rows[0]


class ActiveExternalOrdersProviderBridgeTests(unittest.TestCase):
    def test_neutral_contract_has_semantic_read_without_xtquant_import(self):
        source = CONTRACT.read_text(encoding="utf-8")
        tree = ast.parse(source)
        cls = _class(tree, "BrokerProvider")
        bridge = _method(cls, "get_active_external_orders")
        self.assertEqual([a.arg for a in bridge.args.args], ["self"])
        imports = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imports.extend(a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                imports.append(n.module)
        self.assertFalse(any(x == "xtquant" or x.startswith("xtquant.") for x in imports), imports)

    def test_xtquant_adapter_delegates_to_legacy_semantic_method(self):
        source = ADAPTER.read_text(encoding="utf-8")
        tree = ast.parse(source)
        cls = _class(tree, "XtQuantBrokerProvider")
        bridge = _method(cls, "get_active_external_orders")
        calls = [_chain(n.func) for n in ast.walk(bridge) if isinstance(n, ast.Call)]
        self.assertEqual(calls, [("self", "_broker", "active_external_orders")])
        self.assertEqual(len(bridge.body), 1)
        self.assertIsInstance(bridge.body[0], ast.Return)

    def test_service_reconcile_consumes_provider_semantic_bridge(self):
        source = SERVICE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "reconcile"]
        self.assertEqual(len(funcs), 1)
        calls = [_chain(n.func) for n in ast.walk(funcs[0]) if isinstance(n, ast.Call)]
        self.assertEqual(calls.count(("self", "broker_provider", "get_active_external_orders")), 1)
        self.assertNotIn(("self", "broker", "active_external_orders"), calls)
        self.assertIn("if self.broker else []", ast.get_source_segment(source, funcs[0]) or "")
        self.assertIn("ok = len(external) == 0", ast.get_source_segment(source, funcs[0]) or "")

    def test_legacy_semantic_method_is_retained_with_fallback_and_prefix_filter(self):
        source = LEGACY.read_text(encoding="utf-8")
        tree = ast.parse(source)
        methods = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "active_external_orders"]
        self.assertEqual(len(methods), 1)
        seg = ast.get_source_segment(source, methods[0]) or ""
        self.assertIn("self.query_orders()", seg)
        self.assertIn("strategy_name", seg)
        self.assertIn("PositionX", seg)
        self.assertIn("xtconstant", seg)
        sets = []
        for n in ast.walk(methods[0]):
            if isinstance(n, ast.Set) and all(isinstance(e, ast.Constant) and isinstance(e.value, int) for e in n.elts):
                sets.append({e.value for e in n.elts})
        self.assertIn({48, 49, 50, 51, 52, 55}, sets)


if __name__ == "__main__":
    unittest.main()
