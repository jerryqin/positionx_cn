"""Explicit daily research publication; owned DuckDB/files, no network or tasks.

The full P0 validator stays unchanged by default. These checks call the actual
producer, admission functions, sealed reader and publisher in temporary roots.
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
from scripts import collect_p0_rq_close_v01 as producer
from scripts import postclose_data_admission_v1 as admission


DAY='20261028'
SCOPE='DAILY_SIGNALS_V1'
NOW=pd.Timestamp('2026-10-28T20:00:00+08:00')
OBSERVED='2026-10-28T19:00:00+08:00'
IDS=['600000.XSHG','000001.XSHE']


def rows(*,known_ticks=False):
    bars=pd.DataFrame(dict(order_book_id=IDS,date=pd.Timestamp('2026-10-28'),
        open=10.,high=11.,low=9.,close=10.5,prev_close=10.,volume=1000.,
        total_turnover=10500.,limit_up=11.,limit_down=9.,num_trades=10.,
        source_version='SYNTHETIC_OWNED_TEST'))
    status=pd.DataFrame(dict(order_book_id=IDS,date=pd.Timestamp('2026-10-28'),
        status_suspended=False,status_available_at=OBSERVED,
        price_tick=[.01 if known_ticks else None,.01],
        tick_available_at=[OBSERVED if known_ticks else None,OBSERVED]))
    identity=pd.DataFrame(dict(order_book_id=IDS,symbol=['Fake first','Fake second'],
        listed_date='1991-01-01',de_listed_date='0000-00-00',special_type='Normal',
        round_lot=100,status='Active',type='CS'))
    return bars,status,identity


class DailyResearchPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name).resolve()
        self.asset=self.root/'asset'
        self.asset.mkdir()
        self.counter=0
        for patcher in (patch.object(producer,'ASSET_ROOT',self.asset),
                        patch.object(rq,'clock',return_value=NOW)):
            patcher.start();self.addCleanup(patcher.stop)

    def seal(self,*,scope=SCOPE,known_ticks=False,change=None):
        self.counter+=1
        work=self.root/f'candidate_{self.counter}'
        work.mkdir()
        bars,status,identity=rows(known_ticks=known_ticks)
        if change is not None:change(bars,status,identity)
        db=work/'daily.duckdb'
        with duckdb.connect(str(db),config={'temp_directory':''}) as con:
            for name,frame in [('raw_daily',bars),('execution_status',status),('identity_observed_today',identity)]:
                con.register('incoming',frame)
                con.execute('CREATE TABLE '+name+' AS SELECT * FROM incoming')
        database_hash=rq.sha(db)
        version='asof_'+DAY+'_'+database_hash[:12]
        research=scope==SCOPE
        meta=dict(source='RQDATA',database='daily.duckdb',database_sha256=database_hash,
            version=version,asof='2026-10-28',business_date='2026-10-28',availability=OBSERVED,
            raw_adjustment='none',volume_unit='SHARES',
            qualification_scope=scope,quality_status='DAILY_SIGNALS_PASS_V1' if research else 'PASS',
            engineering_only=research,full_p0_pass=not research,execution_qualified=not research)
        producer.write_json(work/'source_metadata.json',meta)
        for name in ('supplier_ready.json','supplier_ready_after.json'):
            producer.write_json(work/name,dict(status='PASS',business_date='2026-10-28',available_at=OBSERVED))
        files=[work/name for name in ('daily.duckdb','source_metadata.json','supplier_ready.json','supplier_ready_after.json')]
        producer.write_json(work/'manifest.json',dict(version=version,
            files=[dict(path=p.name,sha256=rq.sha(p)) for p in files]))
        producer.write_json(work/'COMPLETE.json',dict(manifest_sha256=rq.sha(work/'manifest.json')))
        return work

    def reader(self,work,*,scope=SCOPE):
        return rq.DailyInput(work/'manifest.json',{'enabled':False},scope=scope)

    def test_default_full_tick_gate_still_blocks_while_research_keeps_unknown(self):
        bars,status,_=rows()
        before=status.copy(deep=True)
        with self.assertRaisesRegex(rq.InputBlocked,'INCREMENT_VALUE_QUALITY_FAILED'):
            producer.validate_day(bars,status,IDS,DAY)
        result=producer.validate_day(bars,status,IDS,DAY,require_tick=False)
        self.assertEqual(result['status'],'DAILY_SIGNALS_PASS_V1')
        self.assertEqual(result['qualification_scope'],SCOPE)
        self.assertEqual(result['unknown_tick_rows'],1)
        self.assertFalse(result['full_p0_pass'])
        self.assertFalse(result['execution_qualified'])
        pd.testing.assert_frame_equal(status,before)
        self.assertTrue(pd.isna(status.loc[0,'price_tick']))
        self.assertIsNone(status.loc[0,'tick_available_at'])

    def test_research_mode_keeps_raw_status_and_known_tick_checks(self):
        mutations=[('bad_ohlc',lambda b,s,i:b.__setitem__('high',8.)),
                   ('negative_volume',lambda b,s,i:b.__setitem__('volume',-1.)),
                   ('nonfinite_close',lambda b,s,i:b.__setitem__('close',np.nan)),
                   ('unknown_status',lambda b,s,i:s.__setitem__('status_suspended',None)),
                   ('suspension_trade_conflict',lambda b,s,i:s.__setitem__('status_suspended',True)),
                   ('wrong_date',lambda b,s,i:s.__setitem__('date',pd.Timestamp('2026-10-27'))),
                   ('invalid_known_tick',lambda b,s,i:s.__setitem__('price_tick',-1.))]
        for name,change in mutations:
            with self.subTest(name=name):
                bars,status,identity=rows();change(bars,status,identity)
                with self.assertRaises(rq.InputBlocked):
                    producer.validate_day(bars,status,IDS,DAY,require_tick=False)

    def test_actual_research_audit_passes_unknown_tick_without_p0_admission(self):
        work=self.seal();reader=self.reader(work)
        result=admission.audit_daily_research_asset(reader,DAY)
        self.assertEqual(result['qualification_scope'],SCOPE)
        self.assertEqual(result['quality']['status'],'DAILY_SIGNALS_PASS_V1')
        self.assertEqual(result['quality']['unknown_tick_rows'],1)
        self.assertFalse(result['full_p0_pass'])
        self.assertFalse(result['execution_qualified'])
        with self.assertRaisesRegex(ValueError,'STOCK_ASSET_NOT_QUALIFIED'):
            admission.audit_stock_asset(reader,DAY)
        with self.assertRaisesRegex(rq.InputBlocked,'SCOPE'):
            self.reader(work,scope='P0_COMPLETE')

    def test_actual_research_audit_rejects_status_time_and_identity_defects(self):
        mutations=[('future_status',lambda b,s,i:s.__setitem__('status_available_at','2026-10-29T08:00:00+08:00')),
                   ('wrong_status_day',lambda b,s,i:s.__setitem__('status_available_at','2026-10-27T19:00:00+08:00')),
                   ('unknown_name',lambda b,s,i:i.__setitem__('symbol',None)),
                   ('invalid_lot',lambda b,s,i:i.__setitem__('round_lot',0))]
        for name,change in mutations:
            with self.subTest(name=name):
                reader=self.reader(self.seal(change=change))
                with self.assertRaises(ValueError):
                    admission.audit_daily_research_asset(reader,DAY)

    def test_explicit_research_publisher_and_default_p0_use_separate_namespaces(self):
        research=self.seal()
        with self.assertRaisesRegex(rq.InputBlocked,'SCOPE'):
            producer.publish(research,self.asset)
        published=Path(producer.publish(research,self.asset,scope=SCOPE))
        self.assertEqual(published.parent,self.asset/'research_versions')
        self.assertEqual(self.reader(published).meta['quality_status'],'DAILY_SIGNALS_PASS_V1')
        full=self.seal(scope='P0_COMPLETE',known_ticks=True)
        with self.assertRaisesRegex(rq.InputBlocked,'SCOPE'):
            producer.publish(full,self.asset,scope=SCOPE)
        normal=Path(producer.publish(full,self.asset))
        self.assertEqual(normal.parent,self.asset/'versions')
        with patch.object(rq,'profile',return_value=dict(enabled=True,asset_root=str(self.asset))):
            self.assertEqual(rq.open_inputs(DAY).root,normal)
            self.assertEqual(rq.open_inputs(DAY,scope=SCOPE).root,published)

    def test_research_publication_audits_real_rows_before_creating_visible_version(self):
        changes=[lambda b,s,i:b.__setitem__('close',-1.),
                 lambda b,s,i:s.__setitem__('status_suspended',None)]
        for change in changes:
            work=self.seal(change=change)
            with self.assertRaises(rq.InputBlocked):
                producer.publish(work,self.asset,scope=SCOPE)
        self.assertFalse((self.asset/'research_versions').exists())
        self.assertFalse((self.asset/'versions').exists())

    def test_research_repeat_is_idempotent_and_unknown_tick_is_preserved(self):
        work=self.seal()
        published=Path(producer.publish(work,self.asset,scope=SCOPE))
        before={p.name:p.read_bytes() for p in published.iterdir() if p.is_file()}
        self.assertEqual(Path(producer.publish(work,self.asset,scope=SCOPE)),published)
        self.assertTrue(all((published/name).read_bytes()==body for name,body in before.items()))
        with duckdb.connect(str(published/'daily.duckdb'),read_only=True,config={'temp_directory':''}) as con:
            row=con.execute('SELECT price_tick,tick_available_at FROM execution_status WHERE order_book_id=?',[IDS[0]]).fetchone()
        self.assertEqual(row,(None,None))
        self.assertFalse((self.asset/'versions').exists())

    def test_interrupted_staging_does_not_replace_old_research_or_become_visible(self):
        old=Path(producer.publish(self.seal(),self.asset,scope=SCOPE))
        before={p.name:p.read_bytes() for p in old.iterdir() if p.is_file()}
        new=self.seal(change=lambda b,s,i:b.__setitem__('close',10.6))
        copy=producer.shutil.copyfile
        def fail_at_complete(source,destination):
            if Path(source).name=='COMPLETE.json':raise OSError('SYNTHETIC_STAGING_INTERRUPTION')
            return copy(source,destination)
        with patch.object(producer.shutil,'copyfile',side_effect=fail_at_complete):
            with self.assertRaisesRegex(OSError,'SYNTHETIC_STAGING_INTERRUPTION'):
                producer.publish(new,self.asset,scope=SCOPE)
        self.assertEqual(list((self.asset/'research_versions').iterdir()),[old])
        self.assertTrue(all((old/name).read_bytes()==body for name,body in before.items()))
        pending=list((self.asset/'publication_staging').iterdir())
        self.assertEqual(len(pending),1)
        self.assertFalse((pending[0]/'COMPLETE.json').exists())
        with patch.object(rq,'profile',return_value=dict(enabled=True,asset_root=str(self.asset))):
            self.assertEqual(rq.open_inputs(DAY,scope=SCOPE).root,old)
            with self.assertRaisesRegex(rq.InputBlocked,'RQ_EXACT_DATE_VERSION_MISSING'):
                rq.open_inputs(DAY)


if __name__=='__main__':
    unittest.main()
