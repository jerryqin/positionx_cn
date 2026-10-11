
from __future__ import annotations
import argparse, hashlib, json, re
from datetime import datetime
from pathlib import Path
import pandas as pd, yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/data/cninfo_commercial_query_expansion_plan_v0_2.yaml"

def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def safe_text(v):
    if v is None: return ""
    try:
        if pd.isna(v): return ""
    except Exception:
        pass
    return str(v)

def norm_title(v):
    s=re.sub(r"</?em>","",safe_text(v),flags=re.I)
    return re.sub(r"\s+","",s).strip()

def deterministic_rank(v):
    return hashlib.sha256(safe_text(v).encode("utf-8")).hexdigest()

def flatten_candidate_terms(cfg):
    rows=[]
    existing=set(cfg["existing_queries"])
    for group,terms in cfg["candidate_query_terms"].items():
        for term in terms:
            # An exact duplicate of an existing query is never an expansion.
            if term not in existing:
                rows.append((group,term))
    return rows

def has_any(title, terms):
    t=norm_title(title)
    return any(term in t for term in terms)

def greedy_cover(df, cfg):
    terms=flatten_candidate_terms(cfg)
    min_support=int(cfg["selection"]["min_support"])
    max_terms=int(cfg["selection"]["max_terms"])
    target=float(cfg["selection"]["target_gap_coverage"])

    universe=set(df.index)
    masks={}
    group_of={}
    for group,term in terms:
        hit=set(df.index[df["title_norm"].str.contains(re.escape(term),regex=True,na=False)])
        if len(hit)>=min_support:
            masks[term]=hit
            group_of[term]=group

    uncovered=set(universe)
    selected=[]
    while uncovered and len(selected)<max_terms:
        best=None; best_gain=set()
        for term,hit in masks.items():
            if term in {x["term"] for x in selected}: continue
            gain=hit & uncovered
            if len(gain)>len(best_gain):
                best=term; best_gain=gain
            elif len(gain)==len(best_gain) and gain and best is not None and term<best:
                best=term; best_gain=gain
        if not best or not best_gain:
            break
        covered_after=len(universe)-(len(uncovered)-len(best_gain))
        selected.append({
            "rank":len(selected)+1,
            "group":group_of[best],
            "term":best,
            "incremental_gap_rows":len(best_gain),
            "cumulative_gap_rows":covered_after,
            "cumulative_gap_coverage":covered_after/len(universe) if universe else 0.0,
            "total_title_support":len(masks[best]),
        })
        uncovered-=best_gain
        if universe and (len(universe)-len(uncovered))/len(universe)>=target:
            break
    return selected,uncovered,masks

def check(cfg):
    p=Path(cfg["input"]["gap_rows"])
    out={
        "status":"PASS_CNINFO_QUERY_EXPANSION_PLAN_V0_2_CHECK" if p.exists() else "BLOCKED_CNINFO_QUERY_EXPANSION_PLAN_V0_2_CHECK",
        "gap_rows":str(p),
        "exists":p.exists(),
        "network":False,
        "market_outcomes":False,
        "eva_role":"gap discovery only",
    }
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0 if p.exists() else 3

def run(cfg):
    p=Path(cfg["input"]["gap_rows"])
    if not p.exists(): raise FileNotFoundError(p)
    x=pd.read_csv(p)
    if "announcement_id" not in x.columns or "title" not in x.columns:
        raise RuntimeError("gap csv requires announcement_id and title")

    x["title_norm"]=x["title"].map(norm_title)
    existing=cfg["existing_queries"]
    x["contains_existing_query"]=x["title_norm"].map(lambda t:has_any(t,existing))

    oldmiss=x[x["contains_existing_query"]].copy()
    vocab=x[~x["contains_existing_query"]].copy()

    selected,uncovered,masks=greedy_cover(vocab,cfg)
    selected_terms=[s["term"] for s in selected]
    vocab["covered_by_selected_expansion"]=vocab["title_norm"].map(lambda t:has_any(t,selected_terms))

    # Candidate-term support table, independent of greedy selection.
    support=[]
    group_map={term:group for group,term in flatten_candidate_terms(cfg)}
    for term,hit in sorted(masks.items(), key=lambda kv:(-len(kv[1]),kv[0])):
        support.append({
            "group":group_map[term],
            "term":term,
            "gap_title_support":len(hit),
            "gap_title_support_rate":len(hit)/len(vocab) if len(vocab) else 0.0,
            "selected":term in selected_terms,
        })

    # Deterministic samples for later official-source diagnosis / manual review.
    oldmiss["_rank"]=oldmiss["announcement_id"].map(deterministic_rank)
    oldsample=oldmiss.sort_values("_rank").head(int(cfg["diagnostic_sample"]["existing_keyword_miss_max"])).drop(columns=["_rank"])

    unmatched=vocab[~vocab["covered_by_selected_expansion"]].copy()
    unmatched["_rank"]=unmatched["announcement_id"].map(deterministic_rank)
    unmatched_sample=unmatched.sort_values("_rank").head(int(cfg["diagnostic_sample"]["unmatched_title_max"])).drop(columns=["_rank"])

    out=Path(cfg["output_dir"]); out.mkdir(parents=True,exist_ok=True)
    pd.DataFrame(selected).to_csv(out/"recommended_query_expansion_terms_v0_2.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(support).to_csv(out/"candidate_term_support_v0_2.csv",index=False,encoding="utf-8-sig")
    oldsample.to_csv(out/"existing_keyword_miss_official_diagnostic_sample_v0_2.csv",index=False,encoding="utf-8-sig")
    unmatched_sample.to_csv(out/"unmatched_gap_title_sample_v0_2.csv",index=False,encoding="utf-8-sig")
    vocab.to_csv(out/"vocabulary_gap_with_coverage_v0_2.csv",index=False,encoding="utf-8-sig")

    covered=int(vocab["covered_by_selected_expansion"].sum()) if len(vocab) else 0
    result={
        "status":"PASS_CNINFO_COMMERCIAL_QUERY_EXPANSION_PLAN_V0_2_RUN",
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "plan_id":cfg["plan_id"],
        "input":{
            "gap_rows":int(len(x)),
            "existing_keyword_miss_rows":int(len(oldmiss)),
            "vocabulary_gap_rows":int(len(vocab)),
        },
        "selection":{
            "method":"greedy set cover over a frozen domain-specific candidate-term lexicon; no market outcomes",
            "min_support":int(cfg["selection"]["min_support"]),
            "max_terms":int(cfg["selection"]["max_terms"]),
            "target_gap_coverage":float(cfg["selection"]["target_gap_coverage"]),
            "selected_term_count":int(len(selected)),
            "selected_terms":selected,
            "vocabulary_gap_rows_covered":covered,
            "vocabulary_gap_coverage":covered/len(vocab) if len(vocab) else None,
            "vocabulary_gap_rows_unmatched":int(len(vocab)-covered),
        },
        "existing_keyword_miss":{
            "rows":int(len(oldmiss)),
            "interpretation":"These are not solved by adding vocabulary. Use the deterministic sample to diagnose CNINFO query/search behavior against official sources."
        },
        "decision":{
            "auto_fetch":False,
            "auto_import_eva":False,
            "next":"Review/freeze the recommended terms. Then run a first-party CNINFO Query Expansion acquisition only for the new terms; separately diagnose the existing-keyword misses."
        },
        "artifacts":{
            "recommended_terms":str(out/"recommended_query_expansion_terms_v0_2.csv"),
            "term_support":str(out/"candidate_term_support_v0_2.csv"),
            "existing_keyword_miss_sample":str(out/"existing_keyword_miss_official_diagnostic_sample_v0_2.csv"),
            "unmatched_gap_sample":str(out/"unmatched_gap_title_sample_v0_2.csv"),
        },
        "safety":cfg["safety"],
    }
    (out/"cninfo_commercial_query_expansion_plan_v0_2_summary.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False,indent=2))
    print(f"OUTPUT: {out}")
    return 0

def main():
    p=argparse.ArgumentParser(); p.add_argument("command",choices=["check","run"])
    a=p.parse_args(); cfg=load_cfg()
    try: raise SystemExit(check(cfg) if a.command=="check" else run(cfg))
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_CNINFO_QUERY_EXPANSION_PLAN_V0_2","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2))
        raise SystemExit(3)

if __name__=="__main__": main()
