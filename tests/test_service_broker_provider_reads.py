from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SERVICE=ROOT/"core/service.py"

def call_name(node):
    if isinstance(node,ast.Name):
        return node.id
    if isinstance(node,ast.Attribute):
        base=call_name(node.value)
        return (base+"."+node.attr) if base else node.attr
    return ""

def unparse(node):
    return ast.unparse(node)

def enclosing_method(tree, lineno):
    best=None
    for n in ast.walk(tree):
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)):
            end=getattr(n,"end_lineno",n.lineno)
            if n.lineno <= lineno <= end:
                span=end-n.lineno
                if best is None or span<best[0]:
                    best=(span,n.name)
    return best[1] if best else None

class ServiceBrokerProviderReadTests(unittest.TestCase):
    def setUp(self):
        self.text=SERVICE.read_text(encoding="utf-8")
        self.tree=ast.parse(self.text)

    def test_exact_provider_reads_present(self):
        import ast
        from pathlib import Path

        service = Path(__file__).resolve().parents[1] / "core" / "service.py"
        source = service.read_text(encoding="utf-8")
        tree = ast.parse(source)

        def chain(node):
            parts = []
            cur = node
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
                return tuple(reversed(parts))
            return None

        wanted = {"get_account_snapshot", "get_positions", "get_orders", "get_trades", "get_active_external_orders"}
        got = []
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for call in [n for n in ast.walk(fn) if isinstance(n, ast.Call)]:
                c = chain(call.func)
                if c and c[:2] == ("self", "broker_provider") and c[-1] in wanted:
                    got.append((fn.name, ".".join(c)))
        self.assertEqual(sorted(got), sorted([
            ("monitor_orders", "self.broker_provider.get_orders"),
            ("reconcile", "self.broker_provider.get_active_external_orders"),
            ("sync_broker", "self.broker_provider.get_account_snapshot"),
            ("sync_broker", "self.broker_provider.get_orders"),
            ("sync_broker", "self.broker_provider.get_positions"),
            ("sync_broker", "self.broker_provider.get_trades"),
        ]))

    def test_legacy_read_calls_removed_from_service(self):
        legacy={
            "self.broker.query_account",
            "self.broker.query_positions",
            "self.broker.query_orders",
            "self.broker.query_trades",
        }
        got=[]
        for n in ast.walk(self.tree):
            if isinstance(n,ast.Call) and call_name(n.func) in legacy:
                got.append((n.lineno,call_name(n.func)))
        self.assertEqual(got,[])

    def test_cancel_remains_legacy_execution_path(self):
        calls=[
            call_name(n.func) for n in ast.walk(self.tree)
            if isinstance(n,ast.Call) and call_name(n.func)=="self.broker.cancel"
        ]
        self.assertEqual(calls,["self.broker.cancel"])

    def test_active_external_orders_uses_provider_semantic_bridge(self):
        import ast
        from pathlib import Path

        service = Path(__file__).resolve().parents[1] / "core" / "service.py"
        source = service.read_text(encoding="utf-8")
        tree = ast.parse(source)
        funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "reconcile"]
        self.assertEqual(len(funcs), 1)

        def chain(node):
            parts = []
            cur = node
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
                return tuple(reversed(parts))
            return None

        calls = []
        for call in [n for n in ast.walk(funcs[0]) if isinstance(n, ast.Call)]:
            c = chain(call.func)
            if c in (("self", "broker", "active_external_orders"), ("self", "broker_provider", "get_active_external_orders")):
                calls.append(".".join(c))
        self.assertEqual(calls, ["self.broker_provider.get_active_external_orders"])

if __name__=="__main__":
    unittest.main()
