"""Direct new replay bindings only; disposable local data, no SDK or services."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from unittest.mock import Mock

import duckdb
import pandas as pd

from scripts import run_postclose_local_candidate as local
from scripts import run_deep_oversold_csi500_forward as deep
from scripts import qualify_postclose_funnel as qualify
from scripts import run_ret120_universe_prior_forward_v10 as prior


class ReplayBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=local.ROOT / 'reports')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_empty_seed_does_not_invent_session_or_inherit_plan(self):
        dest = self.root / 'model.duckdb'
        state, proof = local.seed_research_ledger(dest, deep, None,
                                                 ['20261026', '20261027'], '20261027')
        self.assertEqual(state['last_processed_date'], '20261026')
        self.assertEqual(proof['inherited_positions'], [])
        self.assertFalse(proof['forward_acceptance'])
        with local.read_db(dest) as con:
            for table in ('forward_sessions', 'forward_positions', 'forward_signals', 'forward_trades'):
                self.assertEqual(con.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
        with self.assertRaisesRegex(ValueError, 'NEW_ISOLATED_RESEARCH_SEED_REQUIRED'):
            local.seed_research_ledger(dest, deep, None, ['20261026', '20261027'], '20261027')

    def test_seed_cannot_target_production_or_settle_inherited_plans(self):
        with self.assertRaisesRegex(ValueError, 'NEW_ISOLATED_RESEARCH_SEED_REQUIRED'):
            local.seed_research_ledger(local.ROOT / 'data/never.duckdb', deep, None,
                                      ['20261026', '20261027'], '20261027')
        with self.assertRaisesRegex(ValueError, 'RESEARCH_SEED_CANNOT_SETTLE'):
            local.run_model('deep', SimpleNamespace(synthetic_sample=True), {}, {}, {}, ['20261026', '20261027'],
                            '20261027', self.root / 'never', fresh_research=True, settlement_only=True)
        self.assertFalse((self.root / 'never').exists())

    def test_unknown_vendor_type_does_not_become_name_inferred_ST(self):
        path = self.root / 'identity.duckdb'
        identity = pd.DataFrame([dict(order_book_id='000001.XSHE', symbol='*ST_name',
                                      listed_date='2000-01-01', special_type='Other', round_lot=100)])
        status = pd.DataFrame([dict(order_book_id='000001.XSHE', date=pd.Timestamp('2026-10-27'),
                                    status_suspended=False)])
        with duckdb.connect(str(path)) as con:
            con.execute('CREATE TABLE identity_observed_today AS SELECT * FROM identity')
            con.execute('CREATE TABLE execution_status AS SELECT * FROM status')
        detail = local.local_details(SimpleNamespace(db=path, meta={'business_date':'2026-10-27'}), '20261027')['000001.SZ']
        self.assertIsNone(detail['InstrumentName'])
        self.assertEqual(detail['RawInstrumentName'], '*ST_name')
        self.assertEqual(detail['STClassification'], 'UNKNOWN')
        self.assertEqual(detail['InstrumentStatus'], 0)

    def test_funnel_rejects_swapped_or_absent_preopen_proof(self):
        sets = {'000905.SH': {'000001.SZ'}}
        proof = dict(path='actual.parquet', sha256='A', manifest=str(self.root/'pit.json'), manifest_sha256='B')
        with patch.object(local, 'membership', return_value=(sets, proof)):
            self.assertEqual(qualify.bound_preopen_sets(sets, proof, '20261027')[0], sets)
            with self.assertRaisesRegex(ValueError, 'PIT_BINDING_MISMATCH'):
                qualify.bound_preopen_sets(sets, dict(proof, sha256='SWAPPED'), '20261027')
        with self.assertRaisesRegex(ValueError, 'ACTUAL_PREOPEN_PIT_BINDING_REQUIRED'):
            qualify.bound_preopen_sets(sets, None, '20261027')

    def test_funnel_does_not_recreate_unobserved_rows_from_positive_volume(self):
        path = self.root / 'source.duckdb'
        with duckdb.connect(str(path)) as con:
            con.execute("CREATE TABLE raw_daily AS SELECT '000001.XSHE' order_book_id, DATE '2026-10-27' date, 1000 volume, 10 num_trades")
            con.execute("CREATE TABLE identity_observed_today AS SELECT '000001.XSHE' order_book_id, 'Normal' special_type")
        source = SimpleNamespace(db=path, meta={'business_date':'2026-10-27'}, histories=Mock(return_value={}))
        sets = {'000300.SH': {'000001.SZ'}, '000905.SH': set(), '000852.SH': set()}
        proof = {'manifest': 'VERIFIED_FAKE_ONLY'}
        with patch.object(qualify, 'bound_preopen_sets', return_value=(sets, proof)), \
                patch.object(prior, 'load_scanner', return_value=None), \
                patch.object(prior, 'compute_ret120', return_value=pd.DataFrame()) as compute, \
                patch.object(prior, 'tier_coverage', return_value=(pd.DataFrame(), {'CSI300': {'pass':False}})):
            with self.assertRaisesRegex(ValueError, 'FROZEN_FUNNEL_TIER_COVERAGE_FAILED'):
                qualify.run(path, '20261027', self.root/'funnel', source=source,
                            membership_sets=sets, membership_proof=proof)
        self.assertTrue(source.histories.call_args.kwargs['no_fill'])
        self.assertTrue(compute.call_args.args[0].empty)
        self.assertFalse((self.root/'funnel').exists())

    def test_missing_funnel_amount_proof_preserves_core_computation(self):
        for name in ('manifest.json','COMPLETE.json','source_metadata.json','pit.parquet','pit.json','source.duckdb'):
            (self.root/name).write_text('FAKE_ONLY', encoding='utf-8')
        source = SimpleNamespace(root=self.root, db=self.root/'source.duckdb',
                                 meta={'quality_status':'PASS','engineering_only':False},
                                 lineage=lambda adjustment: {'source':'FAKE_ONLY'})
        profile = {'segment_db':str(source.db), 'calendar':str(local.ROOT/'config/ops/cn_exchange_calendar_2026.json')}
        pit = {'path':str(self.root/'pit.parquet'), 'manifest':str(self.root/'pit.json')}
        from core import p0_rq_input as rq
        pit['sha256'], pit['manifest_sha256'] = rq.sha(pit['path']), rq.sha(pit['manifest'])
        curve = pd.DataFrame({'trade_date':['20261027'],'equity':[1e6], 'cash':[1e6], 'gross_market_value':[0.]})
        original_read = local.read_json
        def read(path):
            return profile if Path(path).name in ('p0_core_recovery_v01.json','deep_forward_segment_v01.json') else original_read(path)
        with patch.object(local,'output_path',return_value=self.root/'output'), \
                patch.object(local.OfflineBoundary,'install'), \
                patch.object(local,'read_json',side_effect=read), \
                patch.object(rq,'open_inputs',return_value=source), \
                patch.object(local,'membership',return_value=({},pit)), \
                patch.object(local,'local_details',return_value={}), \
                patch.object(local,'historical_runs',return_value={'runs':[]}), \
                patch.object(local,'run_model',return_value=({'status':'MODEL_REPLAY_COMPLETE'},curve)) as model:
            result = local.run('20261027',self.root/'output',amount_unit_binding=self.root/'missing_binding.json')
        self.assertEqual([c.args[0] for c in model.call_args_list], ['deep','ret120'])
        self.assertNotIn('input_error',result)
        self.assertEqual(result['strategies']['funnel']['status'],'BLOCKED')
        self.assertEqual(result['portfolio']['latest']['portfolio_equity'],1e6)


if __name__ == '__main__':
    unittest.main()
