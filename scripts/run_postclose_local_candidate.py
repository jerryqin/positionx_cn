"""Isolated local model replay; never a historical Forward acceptance or live task.

Reads sealed stock inputs and actual preopen PIT, copies research ledgers, and
reuses frozen calculation functions. No Scheduler or production activation.
"""
from __future__ import annotations

import argparse
import importlib.abc
import json
import os
from pathlib import Path
import shutil
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class OfflineBoundary(importlib.abc.MetaPathFinder):
    """Fail on SDK imports, network, subprocesses, and Python writes outside output."""

    blocked = ('xtquant', 'rqdatac', 'core.broker', 'core.qmt_runtime')

    def __init__(self, output):
        self.output = Path(output).resolve()
        self.violations = []

    def deny(self, event):
        self.violations.append(event)
        raise RuntimeError('OFFLINE_BOUNDARY:' + event)

    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == x or fullname.startswith(x + '.') for x in self.blocked):
            self.deny('import:' + fullname)

    def check_write(self, path):
        if isinstance(path, int):
            return
        p = Path(os.fsdecode(path)).resolve()
        if not p.is_relative_to(self.output):
            self.deny('write_outside_output:' + str(p))

    def audit(self, event, args):
        if event in ('socket.connect', 'socket.getaddrinfo', 'socket.bind', 'subprocess.Popen', 'os.system'):
            self.deny(event)
        if event == 'open':
            path, mode, flags = args
            if (mode and any(x in mode for x in 'wax+')) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                self.check_write(path)
        elif event in ('os.mkdir', 'os.remove', 'os.rmdir'):
            self.check_write(args[0])
        elif event in ('os.rename', 'os.replace'):
            self.check_write(args[0])
            self.check_write(args[1])

    def install(self):
        for name in sys.modules:
            self.find_spec(name)
        sys.meta_path.insert(0, self)
        sys.addaudithook(self.audit)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                   allow_nan=False, default=str) + '\n', encoding='utf-8')


def output_path(value):
    p = Path(value).absolute()
    # Resolve every ancestor before making anything; reject junction redirection.
    if p.resolve() != p or not p.resolve().is_relative_to((ROOT / 'reports').resolve()) or p == ROOT / 'reports':
        raise ValueError('OUTPUT_MUST_BE_NEW_DIRECTORY_UNDER_REPOSITORY_REPORTS')
    if p.exists():
        raise ValueError('OUTPUT_ALREADY_EXISTS')
    return p


def read_db(path):
    import duckdb
    p = Path(path).resolve()
    if not p.is_file():
        raise ValueError('SOURCE_DATABASE_MISSING:' + str(p))
    return duckdb.connect(str(p), read_only=True, config={
        'temp_directory': '', 'autoinstall_known_extensions': 'false',
        'autoload_known_extensions': 'false', 'threads': '1'})


def sessions(calendar_path, day):
    import pandas as pd
    c = read_json(calendar_path)
    if not c['valid_from'] <= day <= c['valid_to']:
        raise ValueError('EXCHANGE_CALENDAR_OUT_OF_RANGE')
    dates = [d.strftime('%Y%m%d') for d in pd.date_range(c['valid_from'], day)
             if d.weekday() < 5 and not any(r['start'] <= d.strftime('%Y%m%d') <= r['end'] for r in c['closed_ranges'])]
    if not dates or dates[-1] != day:
        raise ValueError('NOT_APPROVED_TRADING_DATE')
    return dates


def require_next_session(state, dates, day):
    last = state['last_processed_date']
    if last not in dates or [d for d in dates if d > last] != [day]:
        raise ValueError('MODEL_LEDGER_GAP_OR_ALREADY_PROCESSED')


def previous_profiles(previous_run, day, dates, deep_profile, ret_profile):
    """Continue an explicitly selected immutable model run, never a production cursor."""
    from core import p0_rq_input as rq
    root = Path(previous_run).resolve()
    if not root.is_relative_to((ROOT / 'reports').resolve()):
        raise ValueError('PARENT_MUST_BE_ISOLATED_REPOSITORY_RUN')
    report = read_json(root / 'report.json')
    if report.get('forward_acceptance') is not False or report.get('mode') != 'ISOLATED_LOCAL_MODEL_REPLAY':
        raise ValueError('PARENT_IS_NOT_NOT_FORWARD_MODEL_RUN')
    require_next_session({'last_processed_date': report['business_date']}, dates, day)
    manifest = root / 'artifacts_manifest.json'
    entries = read_json(manifest)['files'] if manifest.is_file() else read_json(root / 'verification.json')['artifact_review']
    inventory = {x['path'].replace('\\', '/'): x['sha256'] for x in entries}
    proofs = []
    for name in ('report.json', 'deep/model.duckdb', 'deep/plan.csv', 'ret120/model.duckdb', 'ret120/plan.csv'):
        p = (root / name).resolve()
        if not p.is_relative_to(root) or name not in inventory or rq.sha(p) != inventory[name]:
            raise ValueError('PARENT_ARTIFACT_HASH_MISMATCH:' + name)
        proofs.append({'path': str(p), 'sha256': inventory[name]})
    profiles = []
    for kind, profile in (('deep', deep_profile), ('ret120', ret_profile)):
        with read_db(root / kind / 'model.duckdb') as con:
            rows = con.execute('SELECT state_json FROM forward_state').fetchall()
            if len(rows) != 1 or json.loads(rows[0][0]).get('last_processed_date') != report['business_date']:
                raise ValueError('PARENT_MODEL_CURSOR_MISMATCH')
        profiles.append(dict(profile, segment_db=str(root / kind / 'model.duckdb')))
    return *profiles, proofs


def plan_freeze(out, day):
    """An actual write-time seal. A NOT_FORWARD replay is never promoted by this seal."""
    import pandas as pd
    from core import p0_rq_input as rq
    calendar = ROOT / 'config/ops/cn_exchange_calendar_2026.json'
    dates = sessions(calendar, read_json(calendar)['valid_to'])
    later = [d for d in dates if d > day]
    proof = {'schema': 'POSTCLOSE_MODEL_PLAN_FREEZE_V1', 'signal_date': day,
             'frozen_at': rq.clock().isoformat(), 'plan_sha256': rq.sha(out / 'plan.csv'),
             'ledger_sha256': rq.sha(out / 'model.duckdb'), 'research_state': 'NOT_FORWARD',
             'next_session': later[0] if later else None, 'forward_qualified': False}
    if later:
        proof['next_execution_not_before'] = pd.Timestamp(later[0]).tz_localize(rq.CST).replace(hour=9, minute=30).isoformat()
    write_json(out / 'plan_freeze.json', proof)
    return proof


def require_plan_before_event(plan_path, proof, event_at):
    import pandas as pd
    from core import p0_rq_input as rq
    frozen, event = pd.Timestamp(proof['frozen_at']), pd.Timestamp(event_at)
    if frozen.tzinfo is None or event.tzinfo is None or frozen >= event:
        raise ValueError('PLAN_NOT_FROZEN_BEFORE_EXECUTION_EVENT')
    if rq.sha(plan_path) != proof['plan_sha256']:
        raise ValueError('FROZEN_PLAN_HASH_MISMATCH')


def membership(profile, day):
    import pandas as pd
    from core import p0_rq_input as rq
    from scripts.p0_core_recovery_v01 import validate_pit
    manifest_hash = validate_pit(profile, day)
    manifest_path = sorted(Path(profile['pit_root']).glob(day + 'T*_preopen.json'))[-1]
    manifest = read_json(manifest_path)
    path = Path(next(i['file_path'] for i in manifest['items'] if i['dataset'] == 'index_membership_000905.SH'))
    sets = rq.validate_preopen_membership(pd.read_parquet(path), day)
    return sets, {'path': str(path), 'sha256': rq.sha(path),
                  'manifest': str(manifest_path), 'manifest_sha256': manifest_hash}


def local_details(source, day):
    """Exact-day retained identity/status; never replace unknown data with zeros."""
    import pandas as pd
    from core import p0_rq_input as rq
    if source.meta.get('business_date') != pd.Timestamp(day).date().isoformat():
        raise ValueError('EXACT_DAY_IDENTITY_BINDING_MISSING')
    with read_db(source.db) as con:
        frame = con.execute('''SELECT i.*, e.status_suspended FROM identity_observed_today i
            LEFT JOIN execution_status e ON i.order_book_id=e.order_book_id AND e.date=?''',
                            [pd.Timestamp(day).date()]).df()
    if frame.order_book_id.duplicated().any():
        raise ValueError('DUPLICATE_EXACT_DAY_IDENTITY')
    result = {}
    for row in frame.to_dict('records'):
        symbol = rq.qmt_id(row['order_book_id'])
        name = row['symbol']
        if not isinstance(name, str) or '\ufffd' in name:
            name = None
        # Vendor special_type is retained factual ST evidence even if a name is damaged.
        if row['special_type'] in ('ST', 'StarST'):
            name = 'ST' if row['special_type'] == 'ST' else '*ST'
        result[symbol] = {
            'InstrumentName': name, 'OpenDate': str(row['listed_date']).replace('-', ''),
            'InstrumentStatus': None if pd.isna(row['status_suspended']) else int(row['status_suspended']),
            'MinLimitOrderVolume': row['round_lot'],
        }
    return result


def require_details(details, symbols):
    missing = sorted(set(symbols) - set(details))
    if missing or any(details[s]['InstrumentStatus'] is None for s in symbols):
        raise ValueError('EXACT_DAY_INSTRUMENT_STATUS_MISSING:' + ','.join(missing[:20]))


def copy_ledger(source_path, dest, strategy, policy_hash, dates, day):
    from core import p0_rq_input as rq
    with read_db(source_path) as con:
        row = con.execute('SELECT state_json,policy_hash FROM forward_state WHERE strategy_id=?', [strategy]).fetchone()
        if not row or row[1] != policy_hash:
            raise ValueError('SOURCE_POLICY_IDENTITY_MISMATCH')
        state = json.loads(row[0])
        require_next_session(state, dates, day)
    before = rq.sha(source_path)
    shutil.copyfile(source_path, dest)
    if before != rq.sha(dest) or before != rq.sha(source_path):
        raise ValueError('SOURCE_LEDGER_CHANGED_DURING_COPY')
    return state, {'path': str(source_path), 'sha256': before, 'copy': str(dest)}


def busy_symbols(store, strategy, table):
    return sorted({r[0] for r in store.con.execute(f'''SELECT symbol FROM forward_positions
        WHERE strategy_id=? UNION SELECT symbol FROM {table}
        WHERE strategy_id=? AND status='PENDING_ENTRY' ''', [strategy, strategy]).fetchall()})


def model_exports(store, module, out, day, table, data_kind='MODEL_RESEARCH_ONLY'):
    """Export evidence as model units; fractional research units remain labelled."""
    strategy = module.POLICY.strategy_id
    for name, query in {
        'signals': f'SELECT * FROM {table} WHERE strategy_id=? AND signal_date=\'{day}\' ORDER BY symbol',
        'positions': "SELECT * FROM forward_positions WHERE strategy_id=? AND ledger='BASE20' ORDER BY symbol",
        'exits': "SELECT * FROM forward_trades WHERE strategy_id=? AND ledger='BASE20' AND exit_date='" + day + "' ORDER BY symbol",
        'plan': f"SELECT * FROM {table} WHERE strategy_id=? AND status='PENDING_ENTRY' ORDER BY symbol",
    }.items():
        frame = store.con.execute(query, [strategy]).df()
        frame['data_kind'] = data_kind
        if name == 'plan':
            frame['action'] = 'MODEL_NEXT_SESSION_OPEN_ENTRY'
            frame['future_open_price'] = None
            frame['future_entry_units'] = None
            frame['target_weight_ceiling'] = module.POLICY.target_weight
            frame['exit_offset_from_signal'] = module.POLICY.exit_offset_sessions_from_signal
            frame['sizing'] = 'ROUND_DOWN_MIN_LOT' if table == 'forward_signals' else 'FRACTIONAL_RESEARCH_ONLY'
        frame.to_csv(out / (name + '.csv'), index=False, encoding='utf-8-sig')
    return store.con.execute("SELECT trade_date,equity,cash,gross_market_value FROM forward_equity WHERE strategy_id=? AND ledger='BASE20' ORDER BY trade_date", [strategy]).df()


def run_model(kind, source, profile, sets, details, dates, day, out, *, settlement_only=False):
    import pandas as pd
    from core import p0_rq_input as rq
    from research.research_store import ResearchStore
    from scripts import run_deep_oversold_csi500_forward as deep
    from scripts import run_ret120_csi500_forward as ret
    module = deep if kind == 'deep' else ret
    policy = module.POLICY
    sample = getattr(source, 'synthetic_sample', False) is True
    if settlement_only and not sample:
        raise ValueError('SETTLEMENT_ONLY_IS_SYNTHETIC_TEST_ONLY')
    data_kind = 'SYNTHETIC_SAMPLE_NOT_MARKET' if sample else 'MODEL_RESEARCH_ONLY'
    artifacts = ret.verify_frozen_prerequisites() if kind == 'ret120' else None
    fingerprint = policy.fingerprint(artifacts) if artifacts else policy.fingerprint()
    if kind == 'ret120' and not rq.enabled():
        raise ValueError('RETAINED_RQ_CONTRACT_NOT_ENABLED')
    out.mkdir()
    state, ledger_proof = copy_ledger(profile['segment_db'], out / 'model.duckdb', policy.strategy_id, fingerprint, dates, day)
    if artifacts and state.get('artifact_hashes') != artifacts:
        raise ValueError('FROZEN_ARTIFACT_DRIFT')
    with ResearchStore(out / 'model.duckdb') as store:
        if kind == 'ret120':
            ret.ensure_ret120_tables(store)
        table = 'forward_signals' if kind == 'deep' else 'ret120_forward_signals'
        busy = busy_symbols(store, policy.strategy_id, table)
        members = sorted(sets['000905.SH'])
        symbols = sorted(set(busy) if settlement_only else set(members) | set(busy))
        require_details(details, symbols)
        if kind == 'deep':
            histories = source.histories(symbols, day, 120, 'front', 120)
        else:
            histories = source.histories(symbols, day, 320, 'back_ratio', 121, allow_incomplete=True)
        if any(s not in histories or day not in histories[s].index for s in busy):
            raise ValueError('BUSY_POSITION_HISTORY_MISSING')
        # Execution validation uses exact raw status/ticks. Deep front T is raw;
        # no adjusted/raw lot or limit conversion is inferred.
        input_details = dict(details)
        if kind == 'deep':
            held = {p['symbol']: p for p in deep.get_positions(store, 'BASE20')}
            for s in busy:
                bar = rq.execution_bar(histories[s], day)
                if abs(bar['factor'] - 1.0) > 1e-10:
                    raise ValueError('DEEP_EXACT_PRICE_BASIS_MISSING:' + s)
                if not bar['tradable']:
                    if s in held and deep.session_distance(dates, held[s]['signal_date'], day) >= policy.exit_offset_sessions_from_signal:
                        raise ValueError('DEEP_DUE_EXIT_REAL_CLOSE_MISSING:' + s)
                    histories[s] = histories[s].copy()
                    histories[s].loc[day, 'open'] = float('nan')
                d = dict(details[s], UpStopPrice=bar['up_stop_price'], PriceTick=bar['price_tick'])
                if pd.isna(d['MinLimitOrderVolume']) or d['MinLimitOrderVolume'] <= 0:
                    raise ValueError('DEEP_MINIMUM_LOT_MISSING:' + s)
                input_details[s] = d
        snapshot = None
        if kind == 'ret120' and not settlement_only:
            snapshot = ret.build_daily_snapshot(rq.feature_histories({s: histories[s] for s in symbols if s in histories}), members, day, policy.feature_min_valid)
        store.con.execute('BEGIN TRANSACTION')
        try:
            if kind == 'deep':
                deep.process_entries(store, histories, day, state, instrument_details=input_details, market_sessions=dates)
                deep.process_exits_and_mark(store, histories, day, state, market_sessions=dates)
                signal_diag = {'new': 0, 'not_emitted': 'SETTLEMENT_SAMPLE_ONLY'} if settlement_only else {'new': deep.detect_signals(store, histories, members, day, state)}
                exit_diag = {'rule': 'S20_CLOSE'}
            else:
                entry_diag = ret.process_entries(store, None, histories, day, dates, state, instrument_details=input_details)
                exit_diag = ret.process_exits_and_mark(store, histories, day, dates, state)
                if settlement_only:
                    signal_diag = {'new': 0, 'not_emitted': 'SETTLEMENT_SAMPLE_ONLY'}
                else:
                    ret.persist_snapshot(store, snapshot, 'LOCAL_SEALED_REPLAY_NOT_FORWARD')
                    signal_diag = ret.detect_signals(store, None, snapshot, day, dates, instrument_details=input_details)
                signal_diag['entry'] = entry_diag
            state['last_processed_date'] = day
            state['status'] = 'SYNTHETIC_SAMPLE_NOT_FORWARD' if sample else 'ISOLATED_MODEL_REPLAY_NOT_FORWARD'
            module.state_counts(store, state)
            store.save_state(policy.strategy_id, fingerprint, state)
            # Never create an accepted historical forward_sessions row.
            store.con.execute('CREATE TABLE IF NOT EXISTS postclose_model_sessions(trade_date VARCHAR PRIMARY KEY, provenance VARCHAR)')
            store.con.execute('INSERT INTO postclose_model_sessions VALUES (?,?)', [day, json.dumps(source.lineage(policy.dividend_type))])
            store.con.execute('COMMIT')
        except BaseException:
            store.con.execute('ROLLBACK')
            raise
        curve = model_exports(store, module, out, day, table, data_kind)
        write_json(out / 'state.json', state)
    summary = {'status': 'SAMPLE_SETTLEMENT_COMPLETE' if sample else 'MODEL_REPLAY_COMPLETE', 'strategy_id': policy.strategy_id,
               'policy_hash': fingerprint, 'ledger_source': ledger_proof,
               'state': state, 'signals': signal_diag, 'exit': exit_diag,
               'data_kind': data_kind, 'forward_acceptance': False,
               'benchmark_close': None, 'benchmark_comparison_status': 'MISSING_LOCAL_BENCHMARK',
               'execution_units': 'LOT_ROUNDED_RESEARCH' if kind == 'deep' else 'FRACTIONAL_RESEARCH_UNITS'}
    if snapshot is not None:
        summary['snapshot'] = {'universe_count': len(snapshot), 'valid_feature_count': int(snapshot.feature_valid.sum()),
                                'q5_count': int(snapshot.selected_q5.sum())}
    summary['plan_freeze'] = plan_freeze(out, day)
    write_json(out / 'summary.json', summary)
    return summary, curve


def funnel_market_dependencies(candidates, day, config=None):
    """Check the frozen structure input, by symbol and exact date, before calling it."""
    import pandas as pd
    from scripts import run_ret120_execution_funnel_v01 as funnel
    c = config or funnel.C()
    bars, _ = funnel.load_market(c, list(candidates), str(pd.Timestamp(day).date()))
    fields = ['open', 'high', 'low', 'close', 'pre_close', 'amount', 'volume']
    records = []
    for symbol in sorted(set(candidates)):
        g = bars.loc[bars.symbol.eq(symbol)].sort_values('trade_date')
        exact = not g.empty and pd.Timestamp(g.trade_date.max()).date() == pd.Timestamp(day).date()
        bad = []
        if len(g):
            import numpy as np
            window = g.tail(65)
            if not np.isfinite(window[fields].to_numpy(dtype=float)).all():
                bad.append('NONFINITE_INPUT')
            if (window[['open', 'high', 'low', 'close', 'pre_close']] <= 0).any().any():
                bad.append('INVALID_PRICE_OR_PRE_CLOSE')
            if (window.amount < 0).any() or (window.volume < 0).any():
                bad.append('NEGATIVE_AMOUNT_OR_VOLUME')
        records.append({'symbol': symbol, 'rows': len(g), 'exact_target': exact,
                        'last_date': str(g.trade_date.max()) if len(g) else None,
                        'history_65': len(g) >= 65, 'invalid_fields': ','.join(bad)})
    ready = all(x['exact_target'] and x['history_65'] and not x['invalid_fields'] for x in records)
    return {'status': 'READY_EXACT_FROZEN_MARKET_INPUT' if ready else 'BLOCKED_STRUCTURE_MARKET_INPUT',
            'catalog': c['paths']['market_catalog'], 'table': c['paths']['market_table'],
            'required_date': str(pd.Timestamp(day).date()), 'required_fields': fields,
            'price_basis': 'RAW_NONE; frozen synth_adjust(close/pre_close)',
            'amount_basis': 'EXISTING_CATALOG_VENDOR_ORIGINAL_NO_CONVERSION',
            'volume_basis': 'RETAINED_NOT_USED_BY_STRUCTURE_ENGINE',
            'candidate_count': len(records), 'exact_target_count': sum(x['exact_target'] for x in records),
            'with_any_history': sum(x['rows'] > 0 for x in records),
            'history_65_count': sum(x['history_65'] for x in records), 'symbols': records}


def run_funnel(source, sets, day, out):
    import pandas as pd
    from scripts import run_ret120_universe_prior_forward_v10 as prior
    config = prior.cfg()
    scanner = prior.load_scanner(config)
    # Full universe is the retained identity inventory, including invalid histories.
    with read_db(source.db) as con:
        from core import p0_rq_input as rq
        full = sorted({rq.qmt_id(r[0]) for r in con.execute('SELECT order_book_id FROM security_identity').fetchall()})
    if len(full) < config['full_market']['min_symbol_count']:
        raise ValueError('FUNNEL_FULL_MARKET_TOO_SMALL')
    frames = {}
    for offset in range(0, len(full), 200):
        frames.update(source.histories(full[offset:offset + 200], day, 320, 'back_ratio', 121,
                                       no_fill=True, allow_incomplete=True))
    csi1800 = set().union(*sets.values())
    if not csi1800.issubset(full):
        raise ValueError('FUNNEL_MEMBERSHIP_OUTSIDE_FULL_UNIVERSE')
    tiers = {'CSI300': sets['000300.SH'], 'CSI500': sets['000905.SH'],
             'CSI1000': sets['000852.SH'], 'EX_CSI1800': set(full) - csi1800}
    close = pd.DataFrame([{'trade_date': pd.Timestamp(d), 'symbol': s, 'close': float(r.close)}
                          for s, f in frames.items() for d, r in f.iterrows()],
                         columns=['trade_date', 'symbol', 'close'])
    panel, coverage = prior.tier_coverage(scanner, tiers, prior.compute_ret120(close, day), config, fail=False)
    out.mkdir()
    panel.to_csv(out / 'coverage_panel.csv', index=False, encoding='utf-8-sig')
    bad = {k: v for k, v in coverage.items() if not v['pass']}
    result = {'status': 'BLOCKED_FEATURE_COVERAGE' if bad else 'PRIOR_RESEARCH_COMPLETE',
              'coverage': coverage, 'full_market_count': len(full),
              'exit_qualification': 'UNQUALIFIED_NO_MODEL_HOLDINGS_OR_EXITS_EMITTED',
              'data_kind': 'INDEPENDENT_RESEARCH_ONLY', 'forward_acceptance': False}
    if not bad:
        panel = prior.apply_rules(panel)
        panel.to_csv(out / 'universe_aware_prior.csv', index=False, encoding='utf-8-sig')
        result['candidate_counts'] = {'momentum': int(panel.momentum_candidate.sum()),
                                      'mean_reversion': int(panel.mean_reversion_candidate.sum())}
        # Retained catalog used by the frozen structure/risk preview must be current.
        # Do not silently substitute another source or assume its turnover units.
        from scripts import run_ret120_execution_funnel_v01 as funnel
        fc = funnel.C()
        candidates = panel.loc[panel.momentum_candidate | panel.mean_reversion_candidate, 'symbol']
        dependency = funnel_market_dependencies(candidates, day, fc)
        write_json(out / 'structure_dependencies.json', dependency)
        result['structure_preview'] = {k: v for k, v in dependency.items() if k != 'symbols'}
        if dependency['status'] == 'READY_EXACT_FROZEN_MARKET_INPUT':
            # Reuse the complete frozen funnel against this isolated prior snapshot.
            snapshot_root = out / 'snapshots'
            snapdir = snapshot_root / day / 'local_replay'
            snapdir.mkdir(parents=True)
            panel.to_parquet(snapdir / 'ret120_universe_prior_snapshot.parquet', index=False)
            fc['paths']['snapshot_root'] = str(snapshot_root)
            fc['paths']['output_root'] = str(out / 'structure')
            rc = funnel.run(fc, str(pd.Timestamp(day).date()), 1_000_000.0)
            result['structure_preview'] = {'status': 'RESEARCH_PREVIEW_COMPLETE' if rc == 0 else 'FAILED'}
    write_json(out / 'summary.json', result)
    return result


def historical_runs(day):
    registry = read_json(ROOT / 'config/ops/daily_ops_task_registry_v1.json')
    paths = sorted((Path(registry['ops_storage']['run_root']) / day).glob('*.json'))
    selected = {'BROKER_RECONCILE_PRECLOSE', 'A_SHARE_DAILY_UPDATE', 'DEEP_FORWARD',
                'RET120_FORWARD', 'PORTFOLIO_SHADOW', 'BROKER_RECONCILE_FINAL', 'DAILY_REPORT'}
    result = []
    for p in paths:
        row = read_json(p)
        if row.get('task_id') in selected:
            from core import p0_rq_input as rq
            result.append({k: row.get(k) for k in ('task_id', 'status', 'actual_started_at', 'error_summary')}
                          | {'path': str(p), 'sha256': rq.sha(p)})
    if not paths:
        return {'status': 'NO_LOCAL_RUN_RECORDS_FOUND', 'runs': []}
    return {'status': 'READ_ONLY_HISTORICAL_SNAPSHOT', 'runs': result}


def run(day, output, previous_run=None):
    import pandas as pd
    from core import p0_rq_input as rq
    out = output_path(output)
    guard = OfflineBoundary(out)
    guard.install()
    out.mkdir(parents=True)
    result = {'schema': 'POSITIONX_LOCAL_POSTCLOSE_CANDIDATE_V1', 'business_date': day,
              'generated_at': rq.clock().isoformat(), 'mode': 'ISOLATED_LOCAL_MODEL_REPLAY',
              'real_account': {'status': 'UNKNOWN_NOT_QUERIED', 'cash': None, 'positions': None, 'fills': None},
              'forward_acceptance': False, 'strategies': {}}
    proofs = []
    curves = {}
    try:
        day = pd.Timestamp(day).strftime('%Y%m%d')
        result['business_date'] = day
        ret_profile = read_json(ROOT / 'config/ops/p0_core_recovery_v01.json')
        deep_profile = read_json(ROOT / 'config/ops/deep_forward_segment_v01.json')
        dates = sessions(ret_profile['calendar'], day)
        if previous_run:
            deep_profile, ret_profile, parent_proofs = previous_profiles(previous_run, day, dates, deep_profile, ret_profile)
            proofs.extend(parent_proofs)
            result['parent_run'] = str(Path(previous_run).resolve())
        source = rq.open_inputs(day)
        if source.meta.get('quality_status') != 'PASS' or source.meta.get('engineering_only') is not False:
            raise ValueError('LOCAL_ASSET_NOT_QUALIFIED')
        result['input'] = source.lineage('back_ratio')
        sets, result['pit'] = membership(ret_profile, day)
        proofs += [{'path': str(source.root / name), 'sha256': rq.sha(source.root / name)}
                   for name in ('manifest.json', 'COMPLETE.json', 'source_metadata.json')]
        proofs.append({'path': str(source.db), 'sha256': rq.sha(source.db)})
        proofs += [{'path': result['pit']['path'], 'sha256': result['pit']['sha256']},
                   {'path': result['pit']['manifest'], 'sha256': result['pit']['manifest_sha256']}]
        for p in (ret_profile['segment_db'], deep_profile['segment_db']):
            proofs.append({'path': str(p), 'sha256': rq.sha(p)})
        for p in (Path(__file__), ROOT / 'scripts/run_deep_oversold_csi500_forward.py',
                  ROOT / 'scripts/run_ret120_csi500_forward.py', ROOT / 'core/p0_rq_input.py',
                  ROOT / 'config/ops/cn_exchange_calendar_2026.json',
                  ROOT / 'config/ops/p0_rq_input_v01.json',
                  ROOT / 'config/ops/deep_forward_segment_v01.json',
                  ROOT / 'config/ops/p0_core_recovery_v01.json',
                  ROOT / 'config/research/deep_oversold_csi500_forward_v1_1.yaml',
                  ROOT / 'config/research/ret120_csi500_forward_v1_0.yaml'):
            proofs.append({'path': str(p), 'sha256': rq.sha(p)})
        result['historical_ops'] = historical_runs(day)
        proofs += [{'path': x['path'], 'sha256': x['sha256']} for x in result['historical_ops']['runs']]
        details = local_details(source, day)
        print('LOCAL_INPUT_AND_PIT_VERIFIED; running Deep', flush=True)
        try:
            result['strategies']['deep'], curves['deep'] = run_model('deep', source, deep_profile, sets, details, dates, day, out / 'deep')
        except (Exception, SystemExit) as e:
            result['strategies']['deep'] = {'status': 'BLOCKED', 'error': str(e)}
        print('Running RET120 from local CSI500 histories; no download or SDK', flush=True)
        try:
            result['strategies']['ret120'], curves['ret120'] = run_model('ret120', source, ret_profile, sets, details, dates, day, out / 'ret120')
        except (Exception, SystemExit) as e:
            result['strategies']['ret120'] = {'status': 'BLOCKED', 'error': str(e)}
        try:
            print('Running independent Funnel local coverage; no refresh', flush=True)
            result['strategies']['funnel'] = run_funnel(source, sets, day, out / 'funnel')
        except (Exception, SystemExit) as e:
            result['strategies']['funnel'] = {'status': 'BLOCKED', 'error': str(e), 'exit_qualification': 'UNQUALIFIED'}
        if len(curves) == 2:
            from research.strategy_portfolio import StrategyPortfolioPolicy, combine_initial_capital_sleeves
            p = StrategyPortfolioPolicy()
            curve = combine_initial_capital_sleeves(curves['deep'], curves['ret120'], p.deep_initial_weight, p.ret120_initial_weight)
            curve.to_csv(out / 'model_portfolio.csv', index=False, encoding='utf-8-sig')
            result['portfolio'] = {'status': 'ISOLATED_MODEL_PORTFOLIO', 'policy_hash': p.fingerprint(),
                                   'latest': curve.iloc[-1].to_dict(), 'baseline': 'FIRST_COMMON_ISOLATED_MODEL_SESSION'}
    except (Exception, SystemExit) as e:
        result['input_error'] = str(e)
    result['source_files'] = [{**p, 'unchanged': rq.sha(p['path']) == p['sha256']} for p in proofs]
    result['boundary'] = {'violations': guard.violations,
                           'blocked_sdk_modules': list(guard.blocked),
                           'source_hashes_unchanged': all(p['unchanged'] for p in result['source_files'])}
    result['status'] = 'PARTIAL_RESEARCH_CANDIDATE'
    if result.get('input_error') or any(s.get('status') == 'BLOCKED' for s in result['strategies'].values()):
        result['status'] = 'BLOCKED_OUTPUTS_PRESENT'
    if guard.violations or not result['boundary']['source_hashes_unchanged']:
        result['status'] = 'BOUNDARY_VALIDATION_FAILED'
    write_json(out / 'report.json', result)
    write_report(out / 'report.md', result)
    write_json(out / 'artifacts_manifest.json', {'schema': 'POSTCLOSE_LOCAL_ARTIFACTS_V1',
               'files': [{'path': p.relative_to(out).as_posix(), 'sha256': rq.sha(p)}
                         for p in sorted(out.rglob('*')) if p.is_file()]})
    return result


def write_report(path, result):
    lines = ['# PositionX-CN 盘后研究日报', '',
             f"数据日期：{result['business_date']}；生成时间：{result['generated_at']}。",
             f"状态：{result['status']}。本次为隔离模型复算，未形成历史 Forward / Shadow Ops 验收。", '',
             '真实账户、现金、持仓与成交：未知，未查询；所有模型数据仅属研究。', '',
             '| 策略 | 运行结果 | 模型持仓 | 待入场计划 | 已退出交易累计 |',
             '|---|---|---:|---:|---:|']
    for name in ('deep', 'ret120', 'funnel'):
        item = result['strategies'].get(name, {'status': 'NOT_RUN'})
        st = item.get('state', {})
        lines.append(f"| {name} | {item['status']} | {st.get('active_positions', '未输出')} | {st.get('pending_signals', '未输出')} | {st.get('completed_trades', '未输出')} |")
    lines += ['', 'Deep 使用冻结 ret20≤-25%、S1 开盘、S20 收盘、整手向下取整；RET120 使用 CSI500 Q5、S1 开盘、S120 收盘及有效收盘延期规则，碎股仅为研究单位。两者保留 20/50bp 往返成本及原容量限制。',
              '组合沿用 50% / 50% 初始资本分袖，不再平衡；Funnel 独立研究，退出未资格化，不生成该策略模型持仓与退出。', '',
              '影响本次输出的缺项：', '',
              '- 封版股票输入没有 CSI500 指数行情：指数比较与原 Forward session 验收未输出；交易日距离使用已有批准的本地交易日历。']
    f = result['strategies'].get('funnel', {})
    if f.get('structure_preview', {}).get('status') == 'BLOCKED_STALE_MARKET_CATALOG':
        v = f['structure_preview']
        lines.append(f"- Funnel 结构及风险预览的原市场目录只到 {v['catalog_latest']}，需要 {v['required_date']}；未替换数据源、未生成 PREPARE_MANUAL。")
    for name, item in result['strategies'].items():
        if 'error' in item:
            lines.append(f"- {name}：{item['error']}")
        if item.get('status') == 'BLOCKED_FEATURE_COVERAGE':
            lines.append('- Funnel 分层覆盖未达冻结门槛：' + json.dumps(item['coverage'], ensure_ascii=False))
    if f.get('candidate_counts'):
        lines += ['', 'Funnel 独立研究候选：' + json.dumps(f['candidate_counts'], ensure_ascii=False),
                  '结构预览：' + json.dumps(f.get('structure_preview'), ensure_ascii=False)]
    if result.get('portfolio'):
        lines += ['', '模型组合与收益（不是实际账户净值）：',
                  json.dumps(result['portfolio']['latest'], ensure_ascii=False),
                  '模型收益按复制账本的模型权益相对其模型初始资本计算，不代表真实账户盈利。']
    if 'input_error' in result:
        lines.append('- 本地输入：' + result['input_error'])
    lines += ['', '历史运行状态（只读快照，未追改）：', '']
    for row in result.get('historical_ops', {}).get('runs', []):
        lines.append(f"- {row['task_id']} / {row['actual_started_at']} / {row['status']} / {row['error_summary']}")
    lines += ['', '隔离验证：', '',
              '- SDK 导入、网络、子进程与仓库外 Python 写入均受入口边界拦截；运行期违规记录：' + json.dumps(result['boundary']['violations'], ensure_ascii=False),
              '- 源输入、PIT、源账本和所引用历史记录哈希保持不变：' + str(result['boundary']['source_hashes_unchanged']),
              '- 生产数据库与 Scheduler 未应用改动；未进行 Git 写操作。', '',
              '各策略目录的 plan/signals/positions/exits.csv 与 model.duckdb 均是隔离模型产物。空退出文件表示该模型日没有退出，不能推断真实账户无成交。完整覆盖、来源、哈希与缺项见 report.json。']
    Path(path).write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', required=True, help='Exact local business date; never uses nearest older data')
    parser.add_argument('--output', required=True, help='New isolated directory under repository reports/')
    parser.add_argument('--previous-run', help='Explicit immutable local model run to continue; remains NOT_FORWARD')
    args = parser.parse_args()
    result = run(args.date, args.output, args.previous_run)
    print(json.dumps({'status': result['status'], 'report': str(Path(args.output).resolve() / 'report.md')}, ensure_ascii=False))
    return 3 if result['status'] in ('BLOCKED_OUTPUTS_PRESENT', 'BOUNDARY_VALIDATION_FAILED') else 0


if __name__ == '__main__':
    raise SystemExit(main())
