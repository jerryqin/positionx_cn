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

class ServiceProviderCompositionTests(unittest.TestCase):
    def setUp(self):
        self.text=SERVICE.read_text(encoding="utf-8")
        self.tree=ast.parse(self.text)

    def test_relative_provider_factory_import_present(self):
        matches=[
            n for n in self.tree.body
            if isinstance(n,ast.ImportFrom)
            and n.level==1
            and n.module=="providers.factory"
            and any(a.name=="build_legacy_xtquant_bundle" for a in n.names)
        ]
        self.assertEqual(len(matches),1)

    def test_provider_bundle_built_exactly_once(self):
        calls=[
            n for n in ast.walk(self.tree)
            if isinstance(n,ast.Call) and call_name(n.func)=="build_legacy_xtquant_bundle"
        ]
        self.assertEqual(len(calls),1)
        kw={k.arg:unparse(k.value) for k in calls[0].keywords if k.arg}
        self.assertEqual(kw,{"market_service":"self.market","broker":"self.broker"})

    def test_provider_aliases_are_additive(self):
        got={}
        for n in ast.walk(self.tree):
            if isinstance(n,ast.Assign) and len(n.targets)==1:
                t=unparse(n.targets[0])
                if t in {"self.providers","self.market_provider","self.broker_provider","self.execution_provider"}:
                    got[t]=unparse(n.value)
        self.assertEqual(got,{
            "self.providers":"build_legacy_xtquant_bundle(market_service=self.market, broker=self.broker)",
            "self.market_provider":"self.providers.market_data",
            "self.broker_provider":"self.providers.broker",
            "self.execution_provider":"self.providers.execution",
        })

    def test_service_has_no_direct_external_xtquant_import(self):
        bad=[]
        for n in ast.walk(self.tree):
            if isinstance(n,ast.Import):
                bad.extend(a.name for a in n.names if a.name.startswith("xtquant"))
            elif isinstance(n,ast.ImportFrom):
                if n.level==0 and (n.module or "").startswith("xtquant"):
                    bad.append(n.module or "")
        self.assertEqual(bad,[])

if __name__=="__main__":
    unittest.main()
