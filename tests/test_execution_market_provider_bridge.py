from __future__ import annotations

import ast
import hashlib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXECUTION_SHA256 = "f5657af60492fed878bc99b2c8b0d8abdbe06d982349f4650bddae664c988851"
BRIDGES = (
    "ensure_tradable",
    "full_tick",
    "is_tick_fresh",
    "tick_price",
    "price_limits",
    "price_tick",
    "min_limit_order_volume",
)


def class_method(path: Path, class_name: str, method_name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name]
    assert len(classes) == 1
    methods = [n for n in classes[0].body if isinstance(n, ast.FunctionDef) and n.name == method_name]
    assert len(methods) == 1
    return methods[0]


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


def execution_engine_injections(path: Path):
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    rows = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        fn = chain(n.func)
        if not (fn and fn.split(".")[-1] == "ExecutionEngine"):
            continue
        values = [ast.unparse(a) for a in n.args]
        values += [ast.unparse(k.value) for k in n.keywords]
        rows.append((n.lineno, values))
    return rows


class M1C4ExecutionProviderBridgeTests(unittest.TestCase):
    def test_neutral_provider_exposes_execution_semantic_bridges(self):
        path = ROOT / "core" / "providers" / "market_data.py"
        for name in BRIDGES:
            class_method(path, "MarketDataProvider", name)

    def test_xtquant_adapter_bridges_are_exact_legacy_delegates(self):
        path = ROOT / "core" / "providers" / "xtquant_market_data.py"
        for name in BRIDGES:
            fn = class_method(path, "XtQuantMarketDataProvider", name)
            self.assertEqual(len(fn.body), 1, name)
            self.assertIsInstance(fn.body[0], ast.Return, name)
            call = fn.body[0].value
            self.assertIsInstance(call, ast.Call, name)
            self.assertEqual(ast.unparse(call.func), f"self._market.{name}", name)

    def test_execution_engine_keeps_legacy_broker_but_uses_market_provider(self):
        rows = execution_engine_injections(ROOT / "core" / "service.py")
        provider_rows = [(line, values) for line, values in rows if "self.market_provider" in values]
        self.assertGreaterEqual(len(provider_rows), 1)
        for line, values in provider_rows:
            self.assertIn("self.broker", values, line)
            self.assertNotIn("self.market", values, line)
        legacy_market = sum(v == "self.market" for _, values in rows for v in values)
        self.assertEqual(legacy_market, 0)

    def test_execution_logic_is_byte_frozen(self):
        path = ROOT / "core" / "execution.py"
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), EXECUTION_SHA256)


if __name__ == "__main__":
    unittest.main()
