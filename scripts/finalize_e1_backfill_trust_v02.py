
from __future__ import annotations
import argparse, json
from datetime import datetime
from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/opportunity/e1_identity_qmt_verification_v0_2.yaml"

def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def run(cfg):
    out=Path(cfg["output_dir"])
    ip=out/"e1_identity_freeze_v0_2_summary.json"
    qp=out/"e1_qmt_independent_price_verification_v0_2_summary.json"
    if not ip.exists() or not qp.exists():
        raise FileNotFoundError("identity and qmt summaries are both required")

    identity=json.loads(ip.read_text(encoding="utf-8"))
    qmt=json.loads(qp.read_text(encoding="utf-8"))
    iq=identity.get("coverage_on_frozen_shadow_union",{})
    qs=qmt.get("summary",{})

    checks={
        "identity_frozen":identity.get("identity_frozen") is True,
        "identity_shadow_mapping_ge_95":float(iq.get("next_bar_possible_rate",0))>=0.95,
        "qmt_independent_price_verification":qs.get("accepted") is True,
        "e1_outcomes_not_read_identity":identity.get("e1_outcomes_read") is False,
        "e1_outcomes_not_read_qmt":qmt.get("safety",{}).get("e1_outcomes_read") is False,
    }
    accepted=all(checks.values())
    result={
        "status":"PASS_E1_BACKFILL_TRUST_FINALIZE_V0_2_RUN",
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "checks":checks,
        "accepted_for_e1_research":accepted,
        "canonical_promotion_allowed":False,
        "verdict":"ACCEPT_BACKFILL_FOR_E1_RESEARCH_ONLY" if accepted else "HOLD_BACKFILL_FOR_E1_RESEARCH",
        "next_action":"If accepted, patch E1 runner to consume the frozen identity panel plus frozen shadow union; keep canonical market promotion blocked." if accepted else "Resolve failed identity/QMT trust gates before E1 rerun."
    }
    sp=out/"e1_backfill_trust_finalize_v0_2_summary.json"
    sp.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0

def main():
    p=argparse.ArgumentParser(); p.add_argument("command",choices=["run"]); p.parse_args()
    cfg=load_cfg()
    try: raise SystemExit(run(cfg))
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_E1_BACKFILL_TRUST_FINALIZE_V0_2","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2))
        raise SystemExit(3)

if __name__=="__main__": main()
