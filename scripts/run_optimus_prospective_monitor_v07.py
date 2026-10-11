from __future__ import annotations
import argparse, hashlib, json, subprocess, sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG = PROJECT_ROOT / "config" / "research" / "optimus_prospective_monitor_v07.json"
REGISTRY_SCRIPT = PROJECT_ROOT / "scripts" / "run_optimus_candidate_registry_v03.py"
CANON_SCRIPT = PROJECT_ROOT / "scripts" / "run_optimus_canonicalization_v05.py"

CONFIG_SHA256 = "d8b8020006db4722eb3ff9c468302771e8740df2f59b75c72b5486387cb28e2c"
EXPECTED_REGISTRY_SCRIPT_SHA256 = "3955c6deb314bee4bf6821ae9cc0d2f09c606d0fbd695f34877afa3d4b782884"
EXPECTED_CANON_SCRIPT_SHA256 = "3e07d79ad34b5171618a59af50b3efda7b1836b1c9d697b76fcabbc63c988a1f"
EXPECTED_REGISTRY_SHA256 = "d51bbc34dc0731667cd7c2991b11cb954fd3747c9b1c9c6c6154f40cbbc9ccaa"
EXPECTED_BATCH_ID = "CB_7e2476e590e0bbe674ac8848"
EXPECTED_PLAN_SHA256 = "748c1ca8206ea355c2b42252817ec407d23ceca22ef2903c182e500733bfe4dc"
EXPECTED_RESULT_SHA256 = "e79e591e4e0db9d1efbd379c74a4891042d2d57b9b613660913d11ed335965ba"

def sha256_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda:f.read(1024*1024), b""):
            h.update(b)
    return h.hexdigest()

def run_json(script: Path, command: str) -> dict[str, Any]:
    p=subprocess.run([sys.executable, str(script), command], cwd=PROJECT_ROOT,
                     capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"COMMAND_FAILED {script.name} {command} rc={p.returncode} stderr={p.stderr}")
    try:
        return json.loads(p.stdout)
    except Exception as e:
        raise RuntimeError(f"NON_JSON_OUTPUT {script.name} {command}: {e!r} stdout={p.stdout[:1000]!r}")

def load_config() -> dict[str,Any]:
    if not CONFIG.exists():
        raise RuntimeError(f"CONFIG_MISSING {CONFIG}")
    got=sha256_file(CONFIG)
    if got!=CONFIG_SHA256:
        raise RuntimeError(f"CONFIG_SHA_MISMATCH actual={got} expected={CONFIG_SHA256}")
    return json.loads(CONFIG.read_text(encoding="utf-8"))

def baseline_check() -> dict[str,Any]:
    cfg=load_config()
    if not REGISTRY_SCRIPT.exists() or not CANON_SCRIPT.exists():
        raise RuntimeError("FROZEN_DEPENDENCY_MISSING")
    rs=sha256_file(REGISTRY_SCRIPT)
    cs=sha256_file(CANON_SCRIPT)
    if rs!=EXPECTED_REGISTRY_SCRIPT_SHA256:
        raise RuntimeError(f"REGISTRY_SCRIPT_SHA_MISMATCH actual={rs} expected={EXPECTED_REGISTRY_SCRIPT_SHA256}")
    if cs!=EXPECTED_CANON_SCRIPT_SHA256:
        raise RuntimeError(f"CANON_SCRIPT_SHA_MISMATCH actual={cs} expected={EXPECTED_CANON_SCRIPT_SHA256}")

    reg=run_json(REGISTRY_SCRIPT,"status")
    can=run_json(CANON_SCRIPT,"status")
    m=reg.get("maintenance_counts") or {}
    lc=reg.get("lifecycle_counts") or {}
    latest=can.get("latest_batch") or {}

    errors=[]
    checks = [
        (reg.get("registry_sha256")==EXPECTED_REGISTRY_SHA256,
         f"registry_sha={reg.get('registry_sha256')}"),
        (reg.get("candidate_count")==24, f"candidate_count={reg.get('candidate_count')}"),
        (lc.get("ACTIVE_CANONICAL")==22, f"active_canonical={lc.get('ACTIVE_CANONICAL')}"),
        (int(lc.get("ACTIVE_SEED") or 0)==0, f"active_seed={lc.get('ACTIVE_SEED')}"),
        (int(lc.get("DISCOVERY_ONLY") or 0)==2, f"discovery_lifecycle={lc.get('DISCOVERY_ONLY')}"),
        (reg.get("provisional_evidence_count")==2, f"provisional={reg.get('provisional_evidence_count')}"),
        (reg.get("canonical_candidates")==22, f"canonical_candidates={reg.get('canonical_candidates')}"),
        (reg.get("needs_review_candidates")==2, f"needs_review={reg.get('needs_review_candidates')}"),
        (m.get("seed_audit_debt")==0, f"seed_debt={m.get('seed_audit_debt')}"),
        (m.get("stale_canonical")==0, f"stale={m.get('stale_canonical')}"),
        (m.get("discovery_only")==2, f"discovery={m.get('discovery_only')}"),
        (m.get("provisional_evidence")==2, f"provisional_maintenance={m.get('provisional_evidence')}"),
        (can.get("fact_count")==25, f"fact_count={can.get('fact_count')}"),
        (can.get("canonical_symbol_count")==22, f"canonical_symbols={can.get('canonical_symbol_count')}"),
        (can.get("materialized_fact_evidence_count")==25,
         f"materialized={can.get('materialized_fact_evidence_count')}"),
        (latest.get("batch_id")==EXPECTED_BATCH_ID, f"latest_batch={latest.get('batch_id')}"),
        (latest.get("plan_sha256")==EXPECTED_PLAN_SHA256, f"latest_plan_sha={latest.get('plan_sha256')}"),
        (latest.get("result_manifest_sha256")==EXPECTED_RESULT_SHA256,
         f"latest_result_sha={latest.get('result_manifest_sha256')}"),
    ]
    universe_symbols={str(x["symbol"]).upper().strip() for x in cfg.get("universe",[])}
    discovery_symbols=set(cfg.get("registry_partition",{}).get("discovery_only_symbols",[]))
    if len(universe_symbols)!=22:
        errors.append(f"monitor_universe_count={len(universe_symbols)}")
    if universe_symbols & discovery_symbols:
        errors.append(f"discovery_symbols_leaked_into_current22={sorted(universe_symbols & discovery_symbols)}")
    if discovery_symbols!={"603416.SH","603809.SH"}:
        errors.append(f"discovery_symbol_set={sorted(discovery_symbols)}")

    for ok,msg in checks:
        if not ok: errors.append(msg)
    if errors:
        raise RuntimeError("BASELINE_DRIFT: " + "; ".join(errors))

    return {
        "status":"PASS_OPTIMUS_PROSPECTIVE_MONITOR_V0_7_BASELINE_CHECK",
        "monitor_id":cfg["monitor_id"],
        "baseline_registry_sha256":reg["registry_sha256"],
        "candidate_count":24,
        "monitor_universe_count":22,
        "active_canonical":22,
        "discovery_only":2,
        "provisional_evidence_count":2,
        "active_seed":0,
        "fact_count":25,
        "materialized_fact_evidence_count":25,
        "next":"V0_7_4_1_POST_LEISAI_CANONICALIZATION_STEADY_STATE",
        "mutation":False,
        "broker":False,
        "orders":False,
        "production_db":False,
    }

def plan() -> dict[str,Any]:
    cfg=load_config()
    return {
        "status":"PASS_OPTIMUS_PROSPECTIVE_MONITOR_V0_7_PLAN",
        "monitor_id":cfg["monitor_id"],
        "config_sha256":CONFIG_SHA256,
        "mode":cfg["mode"],
        "baseline":cfg["baseline"],
        "theme":cfg["theme"],
        "universe_count":len(cfg["universe"]),
        "signal_families":cfg["signal_families"],
        "decision_contract":cfg["decision_contract"],
        "output_states":cfg["output_states"],
        "safety":cfg["safety"],
        "next_phase":"V0.7.1 READ_ONLY DAILY INTAKE / PROPOSAL QUEUE",
        "mutation":False,
    }

def status() -> dict[str,Any]:
    cfg=load_config()
    return {
        "status":"ARMED_OPTIMUS_PROSPECTIVE_MONITOR_V0_7_PLAN_ONLY",
        "monitor_id":cfg["monitor_id"],
        "contract_frozen":True,
        "daily_intake_implemented":False,
        "proposal_queue_implemented":False,
        "theme_stage":cfg["theme"]["stage"],
        "auto_theme_stage_change":False,
        "mutation":False,
    }

def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument("command", choices=["check","plan","status"])
    a=ap.parse_args()
    try:
        out={"check":baseline_check,"plan":plan,"status":status}[a.command]()
        print(json.dumps(out,ensure_ascii=False,indent=2))
        return 0
    except Exception as e:
        print(json.dumps({
            "status":f"FAIL_OPTIMUS_PROSPECTIVE_MONITOR_V0_7_{a.command.upper()}",
            "error":repr(e),
            "mutation":False,
            "broker":False,
            "orders":False,
            "production_db":False,
        },ensure_ascii=False,indent=2))
        return 2

if __name__=="__main__":
    raise SystemExit(main())
