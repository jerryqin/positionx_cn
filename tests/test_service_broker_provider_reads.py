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
        got=[]
        for n in ast.walk(self.tree):
            if isinstance(n,ast.Call):
                c=call_name(n.func)
                if c.startswith("self.broker_provider."):
                    got.append((enclosing_method(self.tree,n.lineno),c))
        self.assertEqual(sorted(got),sorted([
            ("sync_broker","self.broker_provider.get_account_snapshot"),
            ("sync_broker","self.broker_provider.get_positions"),
            ("sync_broker","self.broker_provider.get_orders"),
            ("sync_broker","self.broker_provider.get_trades"),
            ("monitor_orders","self.broker_provider.get_orders"),
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

    def test_active_external_orders_remains_legacy_semantic_bridge(self):
        calls=[
            call_name(n.func) for n in ast.walk(self.tree)
            if isinstance(n,ast.Call) and call_name(n.func)=="self.broker.active_external_orders"
        ]
        self.assertEqual(calls,["self.broker.active_external_orders"])

    def test_execution_engine_still_uses_legacy_broker_and_market(self):
        calls=[
            n for n in ast.walk(self.tree)
            if isinstance(n,ast.Call) and call_name(n.func).split(".")[-1]=="ExecutionEngine"
        ]
        self.assertEqual(len(calls),2)
        for c in calls:
            args=[unparse(a) for a in c.args]
            self.assertEqual(args[:3],["self.db","self.broker","self.market"])

if __name__=="__main__":
    unittest.main()
