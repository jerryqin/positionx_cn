from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG = PROJECT_ROOT / "config" / "research" / "optimus_global_discovery_monitor_v072.json"

EXPECTED = {
    "v07_script": ("scripts/run_optimus_prospective_monitor_v07.py", "ab8e7d190d75ebcc56dd6debaa0f55d01f6957f6e930ce064072c175166bea8d"),
    "v07_config": ("config/research/optimus_prospective_monitor_v07.json", "d8b8020006db4722eb3ff9c468302771e8740df2f59b75c72b5486387cb28e2c"),
    "v071_script": ("scripts/run_optimus_prospective_intake_v071.py", "0e0a707030b960f7f1e5530577cf1449071c29b0023b11fc13977d4b0d4d45df"),
    "v071_config": ("config/research/optimus_prospective_intake_v071.json", "efd4570054eb2de576639ab9f264970744204f550814fc22d29ecc5d77309f31"),
}
CONFIG_SHA256 = "0e522b7b57df4694bf75e8c911cc6603c17931e3611059eb4a6ae42da2d8aff4"

def sha256_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda:f.read(1024*1024), b""):
            h.update(b)
    return h.hexdigest()

def load_config():
    if not CONFIG.exists():
        raise RuntimeError(f"CONFIG_MISSING {CONFIG}")
    got=sha256_file(CONFIG)
    if got!=CONFIG_SHA256:
        raise RuntimeError(f"CONFIG_SHA_MISMATCH actual={got} expected={CONFIG_SHA256}")
    return json.loads(CONFIG.read_text(encoding="utf-8"))

def check():
    cfg=load_config()
    actual={}
    for name,(rel,expected) in EXPECTED.items():
        p=PROJECT_ROOT/rel
        if not p.exists():
            raise RuntimeError(f"DEPENDENCY_MISSING {name} {p}")
        got=sha256_file(p)
        actual[name]=got
        if got!=expected:
            raise RuntimeError(f"DEPENDENCY_SHA_MISMATCH {name} actual={got} expected={expected}")
    b=cfg["baseline"]
    if b["candidate_count"]!=24 or b["active_canonical"]!=22 or b["active_seed"]!=0:
        raise RuntimeError("POST_EXPANSION_BASELINE_SHAPE_MISMATCH")
    if b.get("discovery_only")!=2 or b.get("provisional_evidence")!=2:
        raise RuntimeError("POST_EXPANSION_DISCOVERY_PARTITION_MISMATCH")
    if b["canonical_fact_count"]!=25 or b["materialized_fact_evidence_count"]!=25:
        raise RuntimeError("FROZEN_CANONICAL_FACT_BASELINE_MISMATCH")
    if b["v071_first_controlled_capture_proposal_count"]!=0 or b["v071_first_controlled_capture_query_errors"]!=0:
        raise RuntimeError("V071_FIRST_CAPTURE_NOT_CLEAN")
    return {
        "status":"PASS_OPTIMUS_GLOBAL_DISCOVERY_MONITOR_V0_7_2_CHECK",
        "monitor_id":cfg["monitor_id"],
        "config_sha256":CONFIG_SHA256,
        "baseline_registry_sha256":b["registry_sha256"],
        "candidate_count":24,
        "active_canonical":22,
        "discovery_only":2,
        "provisional_evidence":2,
        "active_seed":0,
        "canonical_fact_count":25,
        "v071_first_capture_manifest_payload_sha256":b["v071_first_controlled_capture_manifest_payload_sha256"],
        "dependency_sha256":actual,
        "next":"FREEZE_V0_7_2_GLOBAL_DISCOVERY_CONTRACT",
        "mutation":False,
        "broker":False,
        "orders":False,
        "production_db":False,
    }

def plan():
    cfg=load_config()
    return {
        "status":"PASS_OPTIMUS_GLOBAL_DISCOVERY_MONITOR_V0_7_2_PLAN",
        "monitor_id":cfg["monitor_id"],
        "mode":cfg["mode"],
        "lanes":cfg["lanes"],
        "evidence_contract":cfg["evidence_contract"],
        "discovery_state_machine":cfg["discovery_state_machine"],
        "theme_gate_state_machine":cfg["theme_gate_state_machine"],
        "implementation_phases":cfg["implementation_phases"],
        "safety":cfg["safety"],
        "next_phase":"V0.7.2A SOURCE REGISTRY + NORMALIZED EXTERNAL INBOX",
        "mutation":False,
    }

def status():
    cfg=load_config()
    return {
        "status":"ARMED_OPTIMUS_GLOBAL_DISCOVERY_MONITOR_V0_7_2_PLAN_ONLY",
        "monitor_id":cfg["monitor_id"],
        "contract_frozen":True,
        "theme_official_lane_implemented":False,
        "supplier_global_lane_implemented":False,
        "new_candidate_discovery_implemented":False,
        "theme_stage_mutation":False,
        "registry_mutation":False,
        "canonicalization_mutation":False,
    }

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("command", choices=["check","plan","status"])
    a=ap.parse_args()
    try:
        out={"check":check,"plan":plan,"status":status}[a.command]()
        print(json.dumps(out,ensure_ascii=False,indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({
            "status":f"FAIL_OPTIMUS_GLOBAL_DISCOVERY_MONITOR_V0_7_2_{a.command.upper()}",
            "error":repr(exc),
            "mutation":False,
            "broker":False,
            "orders":False,
            "production_db":False,
        },ensure_ascii=False,indent=2))
        return 2

if __name__=="__main__":
    raise SystemExit(main())
