"""Research scope isolation with owned temporary seals and synthetic observations.

No SDK/provider calls, credentials, Broker, task changes, production files or
formal ledger writes. Frozen feature mathematics is exercised unchanged.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import duckdb
import numpy as np
import pandas as pd

from core import p0_rq_input as rq
from research.forward_deep_oversold import ret20_at_close
from research.forward_ret120 import build_daily_snapshot


DAY='20261028'
NOW=pd.Timestamp('2026-10-28T23:00:00+08:00')
SIGNALS='DAILY_SIGNALS_V1'


def save(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


class DailyResearchTickScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.asset=self.root/'assets'
        evidence=self.root/'synthetic_contract.json'
        save(evidence,dict(synthetic_contract=True))
        self.config=dict(enabled=True,asset_root=str(self.asset),no_fill_semantics_accepted=True,
            strategy_contracts={name:dict(accepted=True,representation=representation,
                evidence_file=str(evidence),evidence_sha256=rq.sha(evidence),
                execution_price_basis_accepted=True,suspension_semantics_accepted=True)
                for name,representation in [('front','QMT_FRONT_AFFINE'),
                    ('back_ratio','RET120_FROZEN_CLOSE_PRE_CLOSE')]})
        calendar=json.loads((rq.ROOT/'config/ops/cn_exchange_calendar_2026.json').read_text(encoding='utf-8'))
        self.days=[d for d in pd.date_range(calendar['valid_from'],DAY)
            if d.weekday()<5 and not any(r['start']<=d.strftime('%Y%m%d')<=r['end'] for r in calendar['closed_ranges'])][-121:]
        self.full=self.make_input('full',partial=False)
        self.partial=self.make_input('signals',partial=True,missing_tick=True)

    def make_input(self,name,*,partial,missing_tick=False,flags=None,asset=None):
        out=(asset or self.asset)/('research_versions' if partial else 'versions')/name
        out.mkdir(parents=True)
        raw=pd.DataFrame(dict(order_book_id='000001.XSHE',date=self.days,
            open=2.5,high=2.6,low=2.4,close=2.5,prev_close=2.5,
            volume=1000.,total_turnover=2500.,num_trades=100.,limit_up=2.75,limit_down=2.25))
        status=pd.DataFrame(dict(order_book_id='000001.XSHE',date=self.days,
            status_suspended=False,price_tick=np.nan if missing_tick else .01,
            status_available_at=[d.tz_localize(rq.CST).replace(hour=16).isoformat() for d in self.days],
            tick_available_at=[None if missing_tick else d.tz_localize(rq.CST).replace(hour=16).isoformat() for d in self.days]))
        actions=pd.DataFrame([dict(order_book_id='000001.XSHE',ex_date=pd.Timestamp(DAY),
            cash_per_share=.172,share_multiplier=1.,rights_per_share=0.,rights_price=0.,
            available_at='2026-10-27T23:59:59+08:00',observed_at='2026-10-28T16:30:00+08:00')])
        db=out/'daily.duckdb'
        with duckdb.connect(str(db),config={'temp_directory':''}) as con:
            con.register('raw_fixture',raw);con.register('status_fixture',status);con.register('action_fixture',actions)
            con.execute('CREATE TABLE raw_daily AS SELECT * FROM raw_fixture')
            con.execute('CREATE TABLE execution_status AS SELECT * FROM status_fixture')
            con.execute('CREATE TABLE corporate_actions AS SELECT * FROM action_fixture')
            con.execute('CREATE TABLE suspension(order_book_id VARCHAR,date DATE,is_suspended BOOLEAN)')
            con.execute('CREATE TABLE factors(order_book_id VARCHAR,ex_date DATE,ex_cum_factor DOUBLE)')
        meta=dict(source='RQDATA',asof='2026-10-28',business_date='2026-10-28',
            availability='2026-10-28T17:00:00+08:00',database='daily.duckdb',database_sha256=rq.sha(db),
            raw_adjustment='none',volume_unit='SHARES',version=name,quality_status='PASS',engineering_only=False,
            corporate_action_coverage_complete=True,corporate_action_symbols=['000001.XSHE'],
            corporate_action_coverage_start=self.days[0].date().isoformat(),corporate_action_coverage_end='2026-10-28')
        if partial:
            meta.update(qualification_scope=SIGNALS,quality_status='DAILY_SIGNALS_PASS_V1',
                engineering_only=True,full_p0_pass=False,execution_qualified=False)
        meta.update(flags or {})
        save(out/'source_metadata.json',meta)
        save(out/'manifest.json',dict(files=[dict(path=path.name,sha256=rq.sha(path))
            for path in [db,out/'source_metadata.json']]))
        save(out/'COMPLETE.json',dict(manifest_sha256=rq.sha(out/'manifest.json')))
        return out/'manifest.json'

    def reader(self,manifest,*,scope='P0_COMPLETE'):
        return rq.DailyInput(manifest,self.config,now=NOW,scope=scope)

    def histories(self,reader,adjustment):
        return reader.histories(['000001.SZ'],DAY,120 if adjustment=='front' else 121,
            adjustment,120 if adjustment=='front' else 121,no_fill=adjustment=='back_ratio')['000001.SZ']

    def test_default_selector_and_legacy_lineage_remain_full_only(self):
        with patch.object(rq,'profile',return_value=self.config),patch.object(rq,'clock',return_value=NOW):
            full=rq.open_inputs(DAY)
            signals=rq.open_inputs(DAY,scope=SIGNALS)
        self.assertEqual(full.root,self.full.parent)
        self.assertEqual(signals.root,self.partial.parent)
        self.assertEqual(full.lineage('back_ratio'),dict(source='RQDATA',adjustment='RET120_FROZEN_CLOSE_PRE_CLOSE',
            requested_adjustment='back_ratio',unit='SHARES',asof='2026-10-28',
            availability='2026-10-28T17:00:00+08:00',version='full'))
        self.assertEqual(signals.lineage('back_ratio')['scope'],SIGNALS)
        self.assertEqual(signals.lineage('back_ratio')['qualification_scope'],SIGNALS)

    def test_default_reader_rejects_partial_and_default_selector_cannot_find_it(self):
        with self.assertRaisesRegex(rq.InputBlocked,'DAILY_SIGNALS_SCOPE_OR_QUALIFICATION_INVALID'):
            self.reader(self.partial)
        only=self.root/'only_research'
        manifest=self.make_input('only',partial=True,missing_tick=True,asset=only)
        config=dict(self.config,asset_root=str(only))
        with patch.object(rq,'profile',return_value=config),patch.object(rq,'clock',return_value=NOW):
            with self.assertRaisesRegex(rq.InputBlocked,'EXACT_DATE_VERSION_MISSING'):
                rq.open_inputs(DAY)
            self.assertEqual(rq.open_inputs(DAY,scope=SIGNALS).root,manifest.parent)

    def test_partial_metadata_flags_require_exact_scope_and_booleans(self):
        for number,(name,value) in enumerate([('quality_status','PASS'),('engineering_only',False),
                ('full_p0_pass',True),('execution_qualified',True),('full_p0_pass',0),
                ('qualification_scope','P0_COMPLETE')]):
            with self.subTest(field=name,value=value):
                bad=self.make_input('invalid_'+str(number),partial=True,flags={name:value})
                with self.assertRaises(rq.InputBlocked):
                    self.reader(bad,scope=SIGNALS)
        with self.assertRaisesRegex(rq.InputBlocked,'QUALIFICATION_SCOPE_MISMATCH'):
            self.reader(self.full,scope=SIGNALS)

    def test_affine_and_deep_ret20_are_unchanged_when_tick_is_unknown(self):
        full=self.histories(self.reader(self.full),'front')
        signals=self.histories(self.reader(self.partial,scope=SIGNALS),'front')
        for column in ('open','high','low','close','research_factor'):
            np.testing.assert_array_equal(full[column].to_numpy(),signals[column].to_numpy())
        self.assertAlmostEqual(signals.iloc[0].close,2.328,places=14)
        self.assertNotEqual(signals.iloc[0].close,2.33)
        self.assertEqual(ret20_at_close(full,DAY),ret20_at_close(signals,DAY))
        self.assertEqual(signals.attrs['scope'],SIGNALS)
        self.assertTrue(signals.price_tick.isna().all())
        self.assertTrue(signals.tick_available_at.isna().all())

    def test_real_observation_ret120_and_frozen_ranking_do_not_require_tick(self):
        full=self.histories(self.reader(self.full),'back_ratio')
        signals=self.histories(self.reader(self.partial,scope=SIGNALS),'back_ratio')
        self.assertEqual(len(full),121);self.assertEqual(len(signals),121)
        self.assertTrue(all(rq.real_observation(row,NOW) for _,row in signals.iterrows()))
        for column in ('open','high','low','close','research_factor','factor_valid'):
            np.testing.assert_array_equal(full[column].to_numpy(),signals[column].to_numpy())
        members=[f'{600000+n:06}.SH' for n in range(500)]
        before=build_daily_snapshot({s:full for s in members},members,DAY)
        after=build_daily_snapshot({s:signals for s in members},members,DAY)
        pd.testing.assert_frame_equal(before,after)
        self.assertEqual(int(after.feature_valid.sum()),500)
        self.assertEqual(int(after.selected_q5.sum()),100)

    def test_partial_scope_never_enters_execution_even_with_a_known_tick(self):
        known=self.histories(self.reader(self.full),'front')
        for key in ('scope','qualification_scope'):
            with self.subTest(key=key):
                flagged=known.copy();flagged.attrs[key]=SIGNALS
                with self.assertRaisesRegex(rq.InputBlocked,'EXECUTION_DAILY_SIGNALS_SCOPE_FORBIDDEN'):
                    rq.execution_bar(flagged,DAY,decision=NOW)
        unknown=self.histories(self.reader(self.partial,scope=SIGNALS),'front')
        with self.assertRaisesRegex(rq.InputBlocked,'EXECUTION_DAILY_SIGNALS_SCOPE_FORBIDDEN'):
            rq.execution_bar(unknown,DAY,decision=NOW)

    def test_full_execution_retains_exact_day_tick_gate(self):
        frame=self.histories(self.reader(self.full),'front')
        self.assertEqual(rq.execution_bar(frame,DAY,decision=NOW)['price_tick'],.01)
        for tick,timestamp in [(np.nan,'2026-10-28T16:00:00+08:00'),
                (.01,None),(.01,'2026-10-27T16:00:00+08:00'),(0.,'2026-10-28T16:00:00+08:00')]:
            with self.subTest(tick=tick,time=timestamp):
                broken=frame.copy();broken.loc[DAY,'price_tick']=tick;broken.loc[DAY,'tick_available_at']=timestamp
                with self.assertRaisesRegex(rq.InputBlocked,'EXECUTION_EXACT_DATE_TICK_MISSING'):
                    rq.execution_bar(broken,DAY,decision=NOW)


if __name__=='__main__':unittest.main()
