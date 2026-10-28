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


def bound_preopen_sets(sets, proof, day):
    if not proof or not proof.get('manifest'):
        raise ValueError('ACTUAL_PREOPEN_PIT_BINDING_REQUIRED')
    actual, current = local.membership({'pit_root': str(Path(proof['manifest']).parent)}, day)
    if actual != sets or any(current[k] != proof.get(k) for k in ('path', 'sha256', 'manifest', 'manifest_sha256')):
        raise ValueError('FUNNEL_PREOPEN_PIT_BINDING_MISMATCH')
    return actual, current


def build_execution_preview(candidates, features, identity, suspension, day, nav, config,
                            *, rule_resolver=None):
    """Run frozen ranking/risk preview on this seal, preserving every candidate."""
    if rule_resolver is None:
        from core.exchange_rule_evidence import resolve_rule
        rule_resolver = resolve_rule
    if not math.isfinite(float(nav)) or float(nav) <= 0:
        raise ValueError('FUNNEL_PREVIEW_POSITIVE_CNY_NAV_REQUIRED')
    if (candidates.symbol.duplicated().any() or features.symbol.duplicated().any()
            or identity.order_book_id.duplicated().any()):
        raise ValueError('FUNNEL_PREVIEW_DUPLICATE_INPUT_KEY')
    if set(candidates.symbol) != set(features.symbol):
        raise ValueError('FUNNEL_PREVIEW_FEATURE_BINDING_MISMATCH')
    eng = funnel.load_engine()
    panel = candidates.merge(features, on='symbol', validate='one_to_one')
    panel['lane'] = np.where(panel.momentum_candidate.astype('boolean').fillna(False),
                             'MOMENTUM', 'MEAN_REVERSION')
    panel['nav'] = float(nav)
    records = {rq.qmt_id(row['order_book_id']): row for row in identity.to_dict('records')}
    ranked = []
    for lane in ('MOMENTUM', 'MEAN_REVERSION'):
        group = panel.loc[panel.lane.eq(lane)].copy()
        if not group.empty:
            ranked.append(eng.rank_panel(group, lane))
    if not ranked:
        panel['status'] = pd.Series(dtype='str')
        return panel, {'status': 'PASS_EXECUTION_PREVIEW_NO_CANDIDATES', 'candidate_count': 0,
                       'unknown_symbols': [], 'blocked_symbols': [], 'minimum_order_skips': [],
                       'decision_counts': {}, 'output_counts': {}, 'nav': float(nav),
                       'full_model_status': 'FUNNEL_FULL_MODEL_BLOCKED_EXIT_POLICY'}
    rows = pd.concat(ranked, ignore_index=True).to_dict('records')
    for row in rows:
        symbol = row['symbol']
        detail = records.get(symbol)
        row.update(preview_decision='NOT_READY_STRUCTURE', classification='UNKNOWN',
                   minimum_order_quantity=None, quantity_step=None, price_tick=None,
                   rule_provenance=None, preview_shares=None, preview_notional=None,
                   preview_weight=None, risk_budget_amount=None, executable_preview=False,
                   future_open_price=None, future_entry_units=None,
                   execution_status='CONDITIONAL_PREVIEW_NOT_A_MODEL_ENTRY',
                   price_reference='T_CLOSE_ONLY_RECALCULATE_AT_ACTUAL_NEXT_OPEN')
        if detail is None:
            row['preview_decision'] = 'BLOCKED_EXACT_DAY_IDENTITY_MISSING'
            continue
        special = detail.get('special_type')
        row['native_special_type'] = special
        row['classification'] = special if special in ('Normal', 'ST', 'StarST') else 'UNKNOWN'
        try:
            rule = rule_resolver(dict(detail, qmt_symbol=symbol), day)
            row.update(minimum_order_quantity=rule['minimum_order_quantity'],
                       quantity_step=rule['quantity_step'], price_tick=rule['price_tick'],
                       rule_provenance=json.dumps(rule, ensure_ascii=False, sort_keys=True, default=str))
        except Exception as exc:
            row['preview_decision'] = 'BLOCKED_TRADING_RULE_EVIDENCE'
            row['rule_error'] = str(exc)
            continue
        if row['classification'] == 'UNKNOWN':
            row['preview_decision'] = 'BLOCKED_UNKNOWN_SECURITY_CLASSIFICATION'
            continue
        suspended = suspension.get(symbol)
        if not isinstance(suspended, (bool, np.bool_)):
            row['preview_decision'] = 'BLOCKED_EXACT_DAY_SUSPENSION_UNKNOWN'
            continue
        if suspended:
            row['preview_decision'] = 'SKIP_SUSPENDED'
            continue
        ready = row['setup_state'] == 'CONFIRMED_RETEST_T'
        if row['lane'] == 'MEAN_REVERSION':
            ready = ready and pd.notna(row.get('trend_recovered')) and bool(row['trend_recovered'])
        if not ready:
            continue
        sizing = eng.preview_size(row.get('raw_close'), row.get('stop_raw'),
                                  row.get('adv20_amount'), float(nav), row['lane'], config['risk_preview'],
                                  minimum_order_quantity=row['minimum_order_quantity'],
                                  quantity_step=row['quantity_step'])
        row.update(sizing)
        if sizing['sizing_status'] != 'PASS_EXECUTION_QUANTITY_PREVIEW':
            row['preview_decision'] = sizing['sizing_status']
            continue
        row['executable_preview'] = True
        row['preview_decision'] = 'CONDITIONAL_NEXT_OPEN_ZONE_AND_HUMAN_APPROVAL'
    result = pd.DataFrame(rows)
    for field in ('minimum_order_quantity', 'quantity_step', 'preview_shares'):
        result[field] = pd.to_numeric(result[field], errors='raise').astype('Int64')
    result['status'] = 'POOL'
    for lane in ('MOMENTUM', 'MEAN_REVERSION'):
        group = result.loc[result.lane.eq(lane)]
        ready = group.loc[group.executable_preview]
        cap = config['selection_caps']['prepare_manual_momentum' if lane == 'MOMENTUM'
                                       else 'prepare_manual_mean_reversion']
        prepared = set(ready.head(cap).symbol)
        ready_symbols = set(ready.head(config['selection_caps']['ready_per_lane']).symbol) - prepared
        watched = set(group.loc[~group.symbol.isin(prepared | ready_symbols)]
                      .head(config['selection_caps']['watch_per_lane']).symbol)
        result.loc[result.symbol.isin(watched), 'status'] = 'WATCH'
        result.loc[result.symbol.isin(ready_symbols), 'status'] = 'READY'
        result.loc[result.symbol.isin(prepared), 'status'] = 'PREPARE_MANUAL'
    blocked = result.preview_decision.str.startswith('BLOCKED_')
    summary = {'status': 'PARTIAL_EXECUTION_PREVIEW' if blocked.any() else 'PASS_EXECUTION_PREVIEW',
               'candidate_count': len(result), 'nav': float(nav),
               'nav_semantics': 'PREVIEW_ASSUMED_CNY_NAV_NOT_ACCOUNT_EQUITY',
               'ranked_all_candidates_before_execution_constraints': True,
               'decision_counts': {str(k): int(v) for k, v in result.preview_decision.value_counts().items()},
               'unknown_symbols': sorted(result.loc[result.classification.eq('UNKNOWN'), 'symbol']),
               'blocked_symbols': result.loc[blocked, ['symbol', 'preview_decision']].to_dict('records'),
               'minimum_order_skips': result.loc[result.preview_decision.eq('SKIP_MIN_ORDER_RISK'),
                   ['symbol', 'minimum_order_quantity', 'quantity_step', 'sizing_limit']].to_dict('records')
                   if 'sizing_limit' in result else [],
               'output_counts': result.groupby(['lane', 'status']).size().unstack(fill_value=0).to_dict(orient='index'),
               'future_open_and_quantity': 'UNKNOWN_NOT_EXECUTED',
               'full_model_status': 'FUNNEL_FULL_MODEL_BLOCKED_EXIT_POLICY'}
    return result, summary


def run(database, day, output, membership_database=None, amount_unit_binding=None, *,
        source=None, membership_sets=None, membership_proof=None, execution_preview=False, nav=1_000_000.0):
    out = Path(output).resolve()
    if not (out.is_relative_to(ROOT/'reports') or out.is_relative_to(Path('D:/positionx_data').resolve())):
        raise ValueError('OUTPUT_OUTSIDE_AUTHORIZED_DATA_ROOTS')
    database = Path(database).resolve()
    digest = rq.sha(database)
    pit = None
    if membership_sets is not None:
        membership_sets, pit = bound_preopen_sets(membership_sets, membership_proof, day)
    if source is not None and (Path(source.db).resolve() != database
            or source.meta.get('business_date') != pd.Timestamp(day).date().isoformat()):
        raise ValueError('FUNNEL_SOURCE_VERSION_OR_DATE_MISMATCH')
    if execution_preview and (source is None or pit is None or not verify_unit(amount_unit_binding)):
        raise ValueError('FUNNEL_EXECUTION_PREVIEW_REQUIRES_SEALED_SOURCE_PIT_AND_CNY_BINDING')
    if (out/'summary.json').exists():
        old = local.read_json(out/'summary.json')
        if old['input_sha256'] != digest or old['target_date'] != day or old.get('actual_preopen_pit') != pit:
            raise ValueError('IMMUTABLE_FUNNEL_COLLISION')
        if execution_preview and ('execution_preview' not in old
                or old['execution_preview'].get('nav') != float(nav)
                or old['execution_preview'].get('configuration_sha256') != rq.sha(funnel.CFG)
                or old['execution_preview'].get('amount_unit_binding_sha256') != rq.sha(amount_unit_binding)):
            raise ValueError('IMMUTABLE_FUNNEL_PREVIEW_SCOPE_OR_NAV_MISMATCH_USE_NEW_OUTPUT')
        if verify_unit(amount_unit_binding):
            old=dict(old,status='PASS_STRUCTURE_ADV20_INPUTS',amount_unit='CNY',
                     amount_unit_binding=str(amount_unit_binding),
                     feature_sha256=rq.sha(out/'features.parquet'),
                     candidate_sha256=rq.sha(out/'candidates.parquet'))
            if not (out/'qualified_summary.json').exists():local.write_json(out/'qualified_summary.json',old)
        return dict(old, reused=True)
    with local.read_db(database) as c:
        bars = c.execute('SELECT * FROM raw_daily ORDER BY order_book_id,date').df()
        identity = c.execute('SELECT * FROM identity_observed_today').df()
        full = set(identity.order_book_id.map(rq.qmt_id))
        suspension = (c.execute('SELECT order_book_id,status_suspended FROM execution_status WHERE date=?',
                               [pd.Timestamp(day).date()]).df() if execution_preview else None)
    if membership_sets is None:
        with local.read_db(membership_database or database) as c:
            mem = c.execute('SELECT * FROM postclose_membership').df()
        if not pd.to_datetime(mem.effective_date).dt.strftime('%Y%m%d').eq(day).all():
            raise ValueError('EXACT_DAY_MEMBERSHIP_REFERENCE_REQUIRED')
        membership_sets = {code:set(mem.loc[mem.index_code.eq(code),'symbol'])
                           for code in ('000300.SH','000905.SH','000852.SH')}
    if not set.union(*membership_sets.values()).issubset(full):
        raise ValueError('FUNNEL_PIT_OUTSIDE_CURRENT_IDENTITY')
    rows=[]
    if source is not None:
        frames = {}
        symbols = sorted(full)
        for offset in range(0, len(symbols), 200):
            frames.update(source.histories(symbols[offset:offset + 200], day, 320, 'back_ratio', 121,
                                           no_fill=True, allow_incomplete=True))
        rows = [dict(symbol=s, trade_date=pd.Timestamp(d), close=r.close)
                for s,g in frames.items() for d,r in g.tail(121).iterrows()]
    else:
        for obid,g in bars.groupby('order_book_id',sort=False):
            g=g.copy();g['raw_close']=g.close
            g=rq.frozen_ret120_prices(g)
            g=g.loc[g.volume.gt(0)&g.num_trades.gt(0)]
            if len(g)<121 or not g.tail(121).factor_valid.all():continue
            rows.extend(dict(symbol=rq.qmt_id(obid),trade_date=r.date,close=r.close) for r in g.tail(121).itertuples())
    sets={name:membership_sets[code] for name,code in
          [('CSI300','000300.SH'),('CSI500','000905.SH'),('CSI1000','000852.SH')]}
    sets['EX_CSI1800']=full-set.union(*sets.values())
    cfg=prior.cfg()
    panel,coverage=prior.tier_coverage(prior.load_scanner(cfg),sets,
        prior.compute_ret120(pd.DataFrame(rows, columns=['symbol','trade_date','close']),day),cfg,fail=False)
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
    unknown = identity.loc[~identity.special_type.isin(['Normal','ST','StarST']), ['order_book_id','special_type']]
    unknown_candidates = sorted(set(unknown.order_book_id.map(rq.qmt_id)) & set(candidates.symbol))
    report=dict(status='PASS_STRUCTURE_ADV20_INPUTS' if qualified else 'BLOCKED_AMOUNT_UNIT_BINDING',
        target_date=day,input_database=str(database),input_sha256=digest,coverage=coverage,
        actual_preopen_pit=pit,
        membership_semantics='ACTUAL_PREOPEN_PIT' if pit else 'POSTCLOSE_REFERENCE_NOT_PIT',
        prior_observation_semantics='DAILY_INPUT_EXPLICIT_STATUS_NO_FILL' if source is not None else 'LEGACY_VOLUME_TRADE_COUNT_ONLY',
        security_status_qualification=dict(status='BLOCKED_UNKNOWN_SPECIAL_TYPE' if len(unknown) else 'KNOWN_VENDOR_CLASSIFICATIONS',
            unknown_special_type=unknown.to_dict('records'),
            affected_candidate_symbols=unknown_candidates,
            name_does_not_determine_ST=True, no_delisting_or_resumption_time_inferred=True),
        candidate_count=len(candidates),feature_count=len(feature),
        adv20_finite=int(np.isfinite(feature.adv20_amount).sum()),
        adv20_min=float(feature.adv20_amount.min()),adv20_max=float(feature.adv20_amount.max()),
        setup_counts=feature.setup_state.value_counts().to_dict(),
        gate_counts=feature.entry_condition_state.value_counts().to_dict(),
        amount_unit='CNY' if qualified else 'RQ_VENDOR_ORIGINAL_UNQUALIFIED',
        amount_unit_binding=str(amount_unit_binding) if amount_unit_binding else None,
        forward_status='ISOLATED_RESEARCH_NOT_FORWARD' if pit else 'HISTORY_ONLY_PENDING_TRUE_PREOPEN_PIT',exit_rule='UNQUALIFIED_SEPARATE_FROM_DATA_SUPPLY')
    preview = None
    if execution_preview:
        if suspension.order_book_id.duplicated().any():
            raise ValueError('FUNNEL_PREVIEW_DUPLICATE_EXACT_DAY_STATUS')
        status = {rq.qmt_id(r['order_book_id']): r['status_suspended'] for r in suspension.to_dict('records')}
        preview, report['execution_preview'] = build_execution_preview(candidates, feature, identity,
                                                                       status, day, nav, fc)
        report['execution_preview']['amount_unit_binding_sha256'] = rq.sha(amount_unit_binding)
        report['execution_preview']['risk_parameters'] = fc['risk_preview']
        report['execution_preview']['next_open_policy'] = fc['next_open_policy']
        report['execution_preview']['configuration_sha256'] = rq.sha(funnel.CFG)
        report['full_model_status'] = 'FUNNEL_FULL_MODEL_BLOCKED_EXIT_POLICY'
        report['input_version'] = source.meta['version']
    if rq.sha(database)!=digest:raise ValueError('SOURCE_CHANGED_DURING_FUNNEL_CHECK')
    out.mkdir(parents=True,exist_ok=False)
    candidates.to_parquet(out/'candidates.parquet',index=False)
    feature.to_parquet(out/'features.parquet',index=False)
    if preview is not None:
        preview.to_parquet(out/'execution_preview.parquet',index=False)
        preview.to_csv(out/'execution_preview.csv',index=False,encoding='utf-8-sig')
        preview.loc[preview.status.isin(['PREPARE_MANUAL','READY','WATCH'])].to_csv(
            out/'action_sheet.csv',index=False,encoding='utf-8-sig')
    local.write_json(out/'summary.json',report)
    return report
