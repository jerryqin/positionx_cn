"""Disposable local model fixtures; no SDK, network or production writes."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from core import p0_rq_input as rq
from research.research_store import ResearchStore
from scripts import run_postclose_local_candidate as local
from scripts import run_deep_oversold_csi500_forward as deep
from scripts import run_ret120_csi500_forward as ret
from scripts import validate_postclose_local_v1 as v1


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=local.ROOT / 'reports')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        guard = local.OfflineBoundary(self.root)
        sys.meta_path.insert(0, guard)
        self.addCleanup(sys.meta_path.remove, guard)
        self.day = '20261026'
        self.dates = local.sessions(local.ROOT / 'config/ops/cn_exchange_calendar_2026.json', self.day)
        self.artifacts = {'qualification_sha256': 'FAKE', 'design_sha256': 'FAKE', 'semantics_sha256': 'FAKE'}

    def ledger(self, kind='deep', cash=1e6):
        module = deep if kind == 'deep' else ret
        folder = self.root / kind
        folder.mkdir()
        p = folder / 'model.duckdb'
        state = deep.initial_state(deep.POLICY) if kind == 'deep' else ret.initial_state(ret.POLICY, self.artifacts)
        state.update(last_processed_date='20261023', base_cash=cash, shadow_cash=cash,
                     base_equity=cash, shadow_equity=cash, status='ISOLATED_MODEL_REPLAY_NOT_FORWARD')
        fingerprint = module.POLICY.fingerprint() if kind == 'deep' else module.POLICY.fingerprint(self.artifacts)
        with ResearchStore(p) as store:
            if kind == 'ret120':
                ret.ensure_ret120_tables(store)
            store.save_state(module.POLICY.strategy_id, fingerprint, state)
            for s in ['000001.SZ', '000002.SZ']:
                if kind == 'deep':
                    store.con.execute("INSERT INTO forward_signals(strategy_id,symbol,signal_date,ret20,status) VALUES (?,?,?,?,?)",
                                      [module.POLICY.strategy_id, s, '20261023', -.30, 'PENDING_ENTRY'])
                else:
                    store.con.execute("INSERT INTO ret120_forward_signals(strategy_id,symbol,signal_date,ret120,quintile,status) VALUES (?,?,?,?,?,?)",
                                      [module.POLICY.strategy_id, s, '20261023', .30, 'Q5', 'PENDING_ENTRY'])
        pd.DataFrame({'symbol': ['000001.SZ', '000002.SZ']}).to_csv(folder / 'plan.csv', index=False)
        return {'segment_db': str(p)}

    def test_parent_hashes_cursor_and_duplicate_gap(self):
        dp, rp = self.ledger(), self.ledger('ret120')
        local.write_json(self.root / 'report.json', {'business_date': '20261023', 'mode': 'ISOLATED_LOCAL_MODEL_REPLAY', 'forward_acceptance': False})
        names = ['report.json', 'deep/model.duckdb', 'deep/plan.csv', 'ret120/model.duckdb', 'ret120/plan.csv']
        local.write_json(self.root / 'artifacts_manifest.json', {'files': [{'path': n, 'sha256': rq.sha(self.root / n)} for n in names]})
        got_dp, got_rp, proofs = local.previous_profiles(self.root, self.day, self.dates, dp, rp)
        self.assertEqual(got_dp['segment_db'], dp['segment_db'])
        self.assertEqual(len(proofs), 5)
        for d in ('20261023', '20261027'):
            dates = local.sessions(local.ROOT / 'config/ops/cn_exchange_calendar_2026.json', d)
            with self.assertRaisesRegex(ValueError, 'MODEL_LEDGER_GAP'):
                local.previous_profiles(self.root, d, dates, dp, rp)
        (self.root / 'deep/plan.csv').write_text('ALTERED', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'PARENT_ARTIFACT_HASH_MISMATCH'):
            local.previous_profiles(self.root, self.day, self.dates, dp, rp)

    def test_deep_one_suspended_plan_does_not_block_other_entry(self):
        profile = self.ledger()
        before = rq.sha(profile['segment_db'])
        source = v1.SettlementSample(self.day, self.dates, suspended=['000001.SZ'])
        result, _ = local.run_model('deep', source, profile, {'000905.SH': set()},
                                    v1.sample_details(['000001.SZ', '000002.SZ'], ['000001.SZ']),
                                    self.dates, self.day, self.root / 'next', settlement_only=True)
        self.assertEqual(result['data_kind'], 'SYNTHETIC_SAMPLE_NOT_MARKET')
        self.assertEqual(result['state']['active_positions'], 1)
        self.assertEqual(result['state']['blocked_entries'], 1)
        self.assertEqual(before, rq.sha(profile['segment_db']))
        with local.read_db(self.root / 'next/model.duckdb') as con:
            self.assertEqual(con.execute('SELECT count(*) FROM forward_sessions').fetchone()[0], 0)

    def test_missing_bar_preserves_source_and_copied_cursor(self):
        profile = self.ledger()
        before = rq.sha(profile['segment_db'])
        source = v1.SettlementSample(self.day, self.dates, missing=['000002.SZ'])
        with self.assertRaisesRegex(ValueError, 'BUSY_POSITION_HISTORY_MISSING'):
            local.run_model('deep', source, profile, {'000905.SH': set()}, v1.sample_details(['000001.SZ', '000002.SZ']),
                            self.dates, self.day, self.root / 'next', settlement_only=True)
        self.assertEqual(before, rq.sha(profile['segment_db']))
        with local.read_db(self.root / 'next/model.duckdb') as con:
            state = json.loads(con.execute('SELECT state_json FROM forward_state').fetchone()[0])
            self.assertEqual(state['last_processed_date'], '20261023')
            self.assertEqual(con.execute('SELECT count(*) FROM forward_positions').fetchone()[0], 0)

    def test_cash_and_minimum_lot_no_forced_deep_entry(self):
        profile = self.ledger(cash=500.0)
        result, _ = local.run_model('deep', v1.SettlementSample(self.day, self.dates), profile,
                                    {'000905.SH': set()}, v1.sample_details(['000001.SZ', '000002.SZ']),
                                    self.dates, self.day, self.root / 'next', settlement_only=True)
        self.assertEqual(result['state']['active_positions'], 0)
        self.assertEqual(result['state']['base_cash'], 500.0)

    def test_ret_fractional_units_and_low_cash_cost(self):
        profile = self.ledger('ret120', cash=500.0)
        with patch.object(ret, 'verify_frozen_prerequisites', return_value=self.artifacts):
            result, _ = local.run_model('ret120', v1.SettlementSample(self.day, self.dates), profile,
                                       {'000905.SH': set()}, v1.sample_details(['000001.SZ', '000002.SZ']),
                                       self.dates, self.day, self.root / 'next', settlement_only=True)
        positions = pd.read_csv(self.root / 'next/positions.csv')
        self.assertEqual(len(positions), 2)
        self.assertAlmostEqual(positions.entry_notional.sum(), 10.0)
        self.assertAlmostEqual(positions.entry_fee.sum(), .01)
        self.assertAlmostEqual(result['state']['base_cash'], 489.99)
        self.assertTrue((positions.shares < 2).all())
        self.assertTrue(positions.data_kind.eq('SYNTHETIC_SAMPLE_NOT_MARKET').all())

    def test_real_data_cannot_use_sample_only_shortcut(self):
        source = v1.SettlementSample(self.day, self.dates)
        source.synthetic_sample = False
        with self.assertRaisesRegex(ValueError, 'SYNTHETIC_TEST_ONLY'):
            local.run_model('deep', source, {}, {}, {}, self.dates, self.day, self.root / 'next', settlement_only=True)

    def test_freeze_time_and_hash_must_precede_event(self):
        plan = self.root / 'plan.csv'
        plan.write_text('SAMPLE', encoding='utf-8')
        proof = {'plan_sha256': rq.sha(plan), 'frozen_at': '2026-10-23T16:00:00+08:00'}
        local.require_plan_before_event(plan, proof, '2026-10-26T09:30:00+08:00')
        proof['frozen_at'] = '2026-10-26T09:31:00+08:00'
        with self.assertRaisesRegex(ValueError, 'PLAN_NOT_FROZEN'):
            local.require_plan_before_event(plan, proof, '2026-10-26T09:30:00+08:00')
        proof['frozen_at'] = '2026-10-23T16:00:00+08:00'
        plan.write_text('ALTERED', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'FROZEN_PLAN_HASH'):
            local.require_plan_before_event(plan, proof, '2026-10-26T09:30:00+08:00')

    def test_funnel_requires_every_symbol_exact_date_and_history(self):
        from scripts import run_ret120_execution_funnel_v01 as funnel
        dates = pd.bdate_range(end='2026-10-26', periods=65)
        bars = pd.DataFrame({'symbol': 'A', 'trade_date': dates, 'open': 10., 'high': 10., 'low': 10.,
                             'close': 10., 'pre_close': 10., 'amount': 10000., 'volume': 1000.})
        config = {'paths': {'market_catalog': 'FAKE_ONLY', 'market_table': 'market.daily_bars_v1'}}
        with patch.object(funnel, 'load_market', return_value=(bars, pd.DataFrame())):
            good = local.funnel_market_dependencies(['A'], self.day, config)
            missing = local.funnel_market_dependencies(['A', 'B'], self.day, config)
        self.assertEqual(good['status'], 'READY_EXACT_FROZEN_MARKET_INPUT')
        self.assertEqual(missing['status'], 'BLOCKED_STRUCTURE_MARKET_INPUT')
        self.assertEqual(missing['exact_target_count'], 1)
        self.assertEqual(missing['with_any_history'], 1)
        bars = bars.iloc[:-1]
        with patch.object(funnel, 'load_market', return_value=(bars, pd.DataFrame())):
            stale = local.funnel_market_dependencies(['A'], self.day, config)
        self.assertEqual(stale['exact_target_count'], 0)

    def test_report_utf8_and_preserves_original_question_mark_evidence(self):
        original = self.root / 'old.md'
        original.write_bytes(b'???????\n')
        digest = rq.sha(original)
        state = {'pending_signals': 1, 'active_positions': 0, 'completed_trades': 0,
                 'base_equity': 1e6, 'total_return_base': 0.0}
        result = {'verified_at': '2026-10-26T12:00:00+08:00',
                  'accepted_result': {'strategies': {k: {'state': state} for k in ('deep', 'ret120')}},
                  'assets': {'target_input': {'error': 'MISSING'}, 'funnel': {'catalog_last_date': '2026-10-15',
                       'dependency': {'candidate_count': 2, 'exact_target_count': 0, 'with_any_history': 1, 'history_65_count': 1}}},
                  'samples': {'strategies': {}}, 'actual_next_session': {'status': 'BLOCKED'},
                  'old_report': {'sha256': digest}, 'source_hashes_unchanged': True, 'boundary_violations': []}
        v1.write_v1_report(self.root / 'new.md', result)
        text = (self.root / 'new.md').read_bytes().decode('utf-8', errors='strict')
        self.assertIn('真实账户', text)
        self.assertIn('模型计划', text)
        self.assertNotIn('????', text)
        self.assertNotIn('\ufffd', text)
        self.assertEqual(rq.sha(original), digest)


if __name__ == '__main__':
    unittest.main()
