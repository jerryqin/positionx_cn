"""Read exact sealed raw inputs through frozen Funnel feature/Gate functions; no trading."""
from __future__ import annotations
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
import pandas as pd
from core import p0_rq_input as rq
from scripts import run_postclose_local_candidate as local
from scripts import run_ret120_universe_prior_forward_v10 as prior
from scripts import run_ret120_execution_funnel_v01 as funnel


def verify_unit(binding):
    if not binding:
        return False
    b = local.read_json(binding)
    valid = (b.get('source') == 'RQDATA' and b.get('field') == 'total_turnover'
            and b.get('unit') == 'CNY' and Path(b.get('evidence_path', '')).is_file()
            and rq.sha(b['evidence_path']) == b.get('evidence_sha256'))
    if not valid:return False
    e=local.read_json(b['evidence_path'])
    return (e.get('status')=='PASS_INDEPENDENT_CNY_AMOUNT_COMPATIBILITY'
            and e.get('unit')=='CNY' and e.get('conversion')==1.0
            and e.get('reference_unit_proof_manifest_verified') is True
            and rq.sha(e['reference_unit_proof'])==e.get('reference_unit_proof_sha256'))


def features_for(frame, day, parameters):
    required = ['open','high','low','close','pre_close','amount','volume']
    x = frame.sort_values('trade_date')
    start = pd.Timestamp(day)-pd.Timedelta(days=260)
    x = x.loc[pd.to_datetime(x.trade_date).between(start, pd.Timestamp(day))]
    if (len(x) < 65 or pd.Timestamp(x.trade_date.max()) != pd.Timestamp(day)
            or x.trade_date.duplicated().any()
            or not np.isfinite(x[required].to_numpy(dtype=float)).all()
            or (x[['open','high','low','close','pre_close']] <= 0).any().any()
            or (x[['amount','volume']] < 0).any().any()):
        raise ValueError('FUNNEL_EXACT_SYMBOL_WINDOW_INVALID')
    eng = funnel.load_engine()
    f = eng.breakout_retest_features(x, parameters)
    if not f.get('history_ok') or not math.isfinite(f['adv20_amount']):
        raise ValueError('FUNNEL_FROZEN_FEATURE_INVALID')
    if not np.isclose(f['adv20_amount'], x.amount.tail(20).mean(), rtol=1e-12):
        raise ValueError('FUNNEL_ADV20_BINDING_FAILED')
    f.update(eng.entry_condition_state(f.get('raw_close'), f.get('open_valid_low'), f.get('open_valid_high')))
    return f


def run(database, day, output, membership_database=None, amount_unit_binding=None):
    out = Path(output).resolve()
    if not (out.is_relative_to(ROOT/'reports') or out.is_relative_to(Path('D:/positionx_data').resolve())):
        raise ValueError('OUTPUT_OUTSIDE_AUTHORIZED_DATA_ROOTS')
    database = Path(database).resolve()
    digest = rq.sha(database)
    if (out/'summary.json').exists():
        old = local.read_json(out/'summary.json')
        if old['input_sha256'] != digest or old['target_date'] != day:
            raise ValueError('IMMUTABLE_FUNNEL_COLLISION')
        if verify_unit(amount_unit_binding):
            old=dict(old,status='PASS_STRUCTURE_ADV20_INPUTS',amount_unit='CNY',
                     amount_unit_binding=str(amount_unit_binding),
                     feature_sha256=rq.sha(out/'features.parquet'),
                     candidate_sha256=rq.sha(out/'candidates.parquet'))
            if not (out/'qualified_summary.json').exists():local.write_json(out/'qualified_summary.json',old)
        return dict(old, reused=True)
    with local.read_db(database) as c:
        bars = c.execute('SELECT * FROM raw_daily ORDER BY order_book_id,date').df()
        full = set(c.execute('SELECT order_book_id FROM identity_observed_today').df().order_book_id.map(rq.qmt_id))
    with local.read_db(membership_database or database) as c:
        mem = c.execute('SELECT * FROM postclose_membership').df()
    if not pd.to_datetime(mem.effective_date).dt.strftime('%Y%m%d').eq(day).all():
        raise ValueError('EXACT_DAY_MEMBERSHIP_REFERENCE_REQUIRED')
    rows=[]
    for obid,g in bars.groupby('order_book_id',sort=False):
        g=g.copy();g['raw_close']=g.close
        g=rq.frozen_ret120_prices(g)
        g=g.loc[g.volume.gt(0)&g.num_trades.gt(0)]
        if len(g)<121 or not g.tail(121).factor_valid.all():continue
        rows.extend(dict(symbol=rq.qmt_id(obid),trade_date=r.date,close=r.close) for r in g.tail(121).itertuples())
    sets={name:set(mem.loc[mem.index_code.eq(code),'symbol']) for name,code in
          [('CSI300','000300.SH'),('CSI500','000905.SH'),('CSI1000','000852.SH')]}
    sets['EX_CSI1800']=full-set.union(*sets.values())
    cfg=prior.cfg()
    panel,coverage=prior.tier_coverage(prior.load_scanner(cfg),sets,
        prior.compute_ret120(pd.DataFrame(rows),day),cfg,fail=False)
    if not all(v['pass'] for v in coverage.values()):raise ValueError('FROZEN_FUNNEL_TIER_COVERAGE_FAILED')
    panel=prior.apply_rules(panel)
    candidates=panel.loc[panel.momentum_candidate|panel.mean_reversion_candidate].copy()
    fc=funnel.C();features=[]
    bars['symbol']=bars.order_book_id.map(rq.qmt_id)
    bars=bars.rename(columns={'date':'trade_date','prev_close':'pre_close','total_turnover':'amount'})
    for symbol,g in bars.loc[bars.symbol.isin(candidates.symbol)].groupby('symbol',sort=False):
        f=features_for(g,day,fc['structure_proxy']);f['symbol']=symbol;features.append(f)
    feature=pd.DataFrame(features)
    if len(feature)!=len(candidates):raise ValueError('FUNNEL_CANDIDATE_FEATURE_COVERAGE_FAILED')
    qualified=verify_unit(amount_unit_binding)
    report=dict(status='PASS_STRUCTURE_ADV20_INPUTS' if qualified else 'BLOCKED_AMOUNT_UNIT_BINDING',
        target_date=day,input_database=str(database),input_sha256=digest,coverage=coverage,
        candidate_count=len(candidates),feature_count=len(feature),
        adv20_finite=int(np.isfinite(feature.adv20_amount).sum()),
        adv20_min=float(feature.adv20_amount.min()),adv20_max=float(feature.adv20_amount.max()),
        setup_counts=feature.setup_state.value_counts().to_dict(),
        gate_counts=feature.entry_condition_state.value_counts().to_dict(),
        amount_unit='CNY' if qualified else 'RQ_VENDOR_ORIGINAL_UNQUALIFIED',
        amount_unit_binding=str(amount_unit_binding) if amount_unit_binding else None,
        forward_status='HISTORY_ONLY_PENDING_TRUE_PREOPEN_PIT',exit_rule='UNQUALIFIED_SEPARATE_FROM_DATA_SUPPLY')
    if rq.sha(database)!=digest:raise ValueError('SOURCE_CHANGED_DURING_FUNNEL_CHECK')
    out.mkdir(parents=True,exist_ok=False)
    candidates.to_parquet(out/'candidates.parquet',index=False)
    feature.to_parquet(out/'features.parquet',index=False)
    local.write_json(out/'summary.json',report)
    return report
