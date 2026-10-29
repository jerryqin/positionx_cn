"""Synthetic past dates, fake HTTPS, and owned temporary files only.

No live credentials, requests, Broker/QMT, Scheduler changes or production writes.
Exercise the actual identity/PIT validators, registration and RET120 reads.
"""
from contextlib import ExitStack, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from core import p0_rq_input as rq, rqdata_https as https, rqdata_identity as identity
from scripts import prepare_daily_data as prep, run_data_only_daily as daily


DAY='20261028'


def transport_error(kind='gaierror'):
    return https.HTTPSTransportError('HTTPS_TRANSPORT_'+kind,
        retryable=kind=='IncompleteRead',details=dict(error_type=kind,failure_stage='READ_BODY'))


class PreopenRecoveryTests(unittest.TestCase):
    def setUp(self):
        stack=ExitStack();self.addCleanup(stack.close)
        self.root=Path(stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.ops=self.root/'ops'
        self.production=self.root/'production_pit'
        self.profile=dict(enabled=True,pit_root=str(self.production))
        daily.local.write_json(self.root/'config/ops/p0_core_recovery_v01.json',
                               dict(pit_root=str(self.production/'manifests')))
        for module in (daily,prep,identity):
            stack.enter_context(patch.object(module,'ROOT',self.root))
        stack.enter_context(patch.object(daily,'OPS',self.ops))
        stack.enter_context(patch.object(rq,'profile',return_value=self.profile))
        self.clock=stack.enter_context(patch.object(rq,'clock'))
        self.at('0820')
        stack.enter_context(patch.dict(https.os.environ,{'RQDATA_LICENSE':'FAKE_TEST_LICENSE'},clear=True))
        stack.enter_context(patch('socket.getaddrinfo',side_effect=AssertionError('REAL_DNS_FORBIDDEN')))
        stack.enter_context(patch('socket.socket.connect',side_effect=AssertionError('REAL_NETWORK_FORBIDDEN')))
        self.providers=[];self.calls=[];self.outcomes=[];self.late=False;self.incomplete=False
        frame=pd.DataFrame(dict(order_book_id=[f'{600000+n:06}.XSHG' for n in range(3000)],
            symbol='Fake test identity',listed_date='1991-01-01',de_listed_date='0000-00-00',
            special_type='Normal',round_lot=100,status='Active',type='CS',exchange='XSHG'))
        self.ids=frame.order_book_id.tolist();self.body=frame.to_csv(index=False).encode()
        self.factory=stack.enter_context(patch.object(daily,'RQDataHTTPSProvider',side_effect=self.provider))

    def at(self,slot):
        self.clock.return_value=pd.Timestamp('2026-10-28T'+slot[:2]+':'+slot[2:]+':00+08:00')

    def provider(self,**kwargs):
        outcome=self.outcomes.pop(0) if self.outcomes else None
        def transport(path,payload,token):
            self.calls.append((path,payload.get('method')))
            if path=='/auth':
                return (401,b'Unauthorized') if outcome=='auth' else (200,b'FAKE_MEMORY_TOKEN')
            method=payload['method']
            if method=='all_instruments':
                if isinstance(outcome,Exception):
                    p._response_metadata=dict(incomplete=True,transfer_complete=False,content_encoding='gzip')
                    raise outcome
                if self.late:self.at('0920')
                p._response_metadata=dict(incomplete=self.incomplete,transfer_complete=not self.incomplete,
                    decoded_body_bytes=len(self.body),content_encoding='identity')
                return 200,self.body
            if method!='index_components':raise AssertionError('UNAUTHORIZED_DATA_METHOD')
            start,count={'000300.XSHG':(0,300),'000905.XSHG':(300,500),
                         '000852.XSHG':(800,1000)}[payload['order_book_id']]
            p._response_metadata=dict(incomplete=False,transfer_complete=True)
            return 200,('('+repr(self.ids[start:start+count])+", Timestamp('2026-10-28 08:00:00'))").encode()
        p=https.RQDataHTTPSProvider(**kwargs,transport=transport)
        self.providers.append(p)
        return p

    def main(self):
        with patch('sys.argv',['entry','--phase','preopen']),redirect_stdout(io.StringIO()):
            return daily.main()

    def attempt(self,slot):
        return self.root/'reports'/('data_only_'+DAY)/'attempts'/slot

    def inventory(self,path):
        return {str(p):rq.sha(p) for p in path.rglob('*') if p.is_file()}

    def test_dns_then_incomplete_read_then_next_window_registers_and_reads_ret120(self):
        self.outcomes=[transport_error(),transport_error('IncompleteRead'),None]
        self.assertEqual(self.main(),3)
        first=self.inventory(self.attempt('0820'))
        first_errors=self.inventory(self.ops)
        self.assertFalse(daily.local.read_json(self.ops/(DAY+'_preopen_last_error.json'))['nonretryable'])
        self.assertEqual(self.main(),3)  # Duplicate trigger cannot fetch twice in one window.
        self.assertEqual(len(self.providers),1)
        self.at('0900');self.assertEqual(self.main(),3)
        second=self.inventory(self.attempt('0900'))
        self.at('0910');self.assertEqual(self.main(),0)
        self.assertEqual(len(self.providers),3)
        self.assertEqual(first,self.inventory(self.attempt('0820')))
        self.assertEqual(second,self.inventory(self.attempt('0900')))
        archives=list(self.ops.glob('*_preopen_*_last_error.json'))
        self.assertEqual(len(archives),2)
        for name,value in first_errors.items():
            if '_082000' in name:self.assertEqual(rq.sha(Path(name)),value)
        receipt=daily.local.read_json(self.ops/(DAY+'_preopen.json'))
        self.assertTrue(receipt['production']);self.assertTrue(receipt['ret120_pit_readable'])
        self.assertEqual(len(rq.current_universe()['members']),500)
        self.assertEqual([p.state['identity_api_attempts'] for p in self.providers],[1,1,1])
        self.assertEqual(self.providers[-1].state['data_api_attempts'],3)

    def test_all_three_failures_are_bounded_and_cutoff_starts_no_capture(self):
        self.outcomes=[transport_error() for _ in range(3)]
        for slot in ('0820','0900','0910'):
            self.at(slot);self.assertEqual(self.main(),3)
        self.at('0920');self.assertEqual(self.main(),3)
        self.assertEqual(len(self.providers),3)
        self.assertFalse(self.production.exists())
        self.assertEqual(len(list((self.root/'reports').rglob('checkpoint.json'))),3)

    def test_auth_failure_blocks_later_windows_and_other_phases(self):
        self.outcomes=['auth']
        self.assertEqual(self.main(),3)
        self.at('0900');self.assertEqual(self.main(),3)
        self.assertEqual(len(self.providers),1)
        with self.assertRaisesRegex(ValueError,'NO_RETRY'):
            daily.require_source_allowed('postclose',DAY)
        self.assertFalse(self.production.exists())

    def test_unqualified_complete_response_is_not_retried_or_published(self):
        frame=pd.read_csv(io.BytesIO(self.body)).drop(columns=['exchange'])
        self.body=frame.to_csv(index=False).encode()
        self.assertEqual(self.main(),3)
        failure=daily.local.read_json(self.ops/(DAY+'_preopen_last_error.json'))
        self.assertEqual(failure['error'],'IDENTITY_FULL_RAW_IDENTITY_CONTRACT_FAILED')
        self.assertTrue(failure['nonretryable'])
        self.at('0900');self.assertEqual(self.main(),3)
        self.assertEqual(len(self.providers),1)
        self.assertFalse(self.production.exists())

    def test_truncated_http_entity_can_only_be_replaced_by_a_fresh_complete_response(self):
        self.incomplete=True
        self.assertEqual(self.main(),3)
        failure=daily.local.read_json(self.ops/(DAY+'_preopen_last_error.json'))
        self.assertFalse(failure['nonretryable'])
        old=self.inventory(self.attempt('0820'))
        self.incomplete=False;self.at('0900')
        self.assertEqual(self.main(),0)
        self.assertEqual(old,self.inventory(self.attempt('0820')))
        self.assertEqual(len(self.providers),2)

    def test_only_transport_failures_can_be_retryable(self):
        for kind in ('gaierror','TimeoutError','ConnectionRefusedError','ConnectionResetError','IncompleteRead'):
            self.assertFalse(daily.failure_result('preopen',DAY,transport_error(kind))['nonretryable'])
        for error in (https.HTTPSAuthError('PERMISSION'),https.HTTPSSecurityError('CERTIFICATE'),
                      https.HTTPSContractError('INVALID_CRC'),rq.InputBlocked('HASH_MISMATCH'),
                      transport_error('UnclassifiedError')):
            self.assertTrue(daily.failure_result('preopen',DAY,error)['nonretryable'])

    def test_registration_is_idempotent_without_another_request_or_changed_original(self):
        self.assertEqual(self.main(),0)
        originals=self.inventory(self.attempt('0820'))
        registered=self.inventory(self.production)
        self.at('0900');self.assertEqual(self.main(),0)
        self.assertEqual(len(self.providers),1)
        self.assertEqual(originals,self.inventory(self.attempt('0820')))
        self.assertEqual(registered,self.inventory(self.production))
        self.assertTrue(daily.local.read_json(self.ops/(DAY+'_preopen.json'))['reused'])

    def test_registration_collision_does_not_overwrite_or_report_green(self):
        self.assertEqual(self.main(),0)
        target=next(self.production.rglob('*.parquet'));target.write_bytes(b'FAKE_CONFLICT')
        self.at('0900');self.assertEqual(self.main(),3)
        self.assertEqual(target.read_bytes(),b'FAKE_CONFLICT')
        self.assertEqual(len(self.providers),1)
        self.assertIn('PIT_REGISTRATION_COLLISION',daily.local.read_json(self.ops/'last_run.json')['error'])

    def test_invalid_identity_hash_cannot_be_registered(self):
        with patch.object(daily,'register_pit',side_effect=ValueError('TEST_STOP_BEFORE_REGISTRATION')):
            self.assertEqual(self.main(),3)
        raw=self.attempt('0820')/'identity/full/raw.csv';raw.write_bytes(b'INVALID')
        with self.assertRaisesRegex(rq.InputBlocked,'HASH_OR_SIZE_MISMATCH'):
            daily.register_pit(self.attempt('0820')/'pit/manifests',DAY)
        self.assertFalse(self.production.exists())

    def test_ret120_read_mismatch_is_not_reported_as_success(self):
        with patch.object(rq,'current_universe',return_value={'ok':True,'members':[],'source':'wrong'}):
            self.assertEqual(self.main(),3)
        last=daily.local.read_json(self.ops/'last_run.json')
        self.assertEqual(last['status'],'RED')
        self.assertEqual(last['error'],'RET120_REGISTERED_PIT_READ_MISMATCH')

    def test_identity_finishing_at_cutoff_cannot_produce_pit(self):
        self.late=True
        self.assertEqual(self.main(),3)
        self.assertEqual(self.calls,[('/auth',None),('/api','all_instruments')])
        self.assertFalse(self.production.exists())

    def test_remaining_time_limits_session_and_too_late_does_not_start(self):
        self.clock.return_value=pd.Timestamp('2026-10-28T09:19:58+08:00')
        self.assertEqual(self.main(),3);self.factory.assert_not_called()
        self.assertFalse((self.root/'reports').exists())

    def test_cutoff_failure_is_not_a_permanent_supplier_block(self):
        self.at('0920');self.assertEqual(self.main(),3)
        self.assertIsNone(daily.source_block('preopen',DAY))
        self.factory.assert_not_called()
        error=https.HTTPSSecurityError('HTTPS_OVERALL_DEADLINE_NO_RETRY')
        self.assertFalse(daily.failure_result('preopen',DAY,error)['nonretryable'])

    def test_wrong_business_date_or_closed_session_starts_no_capture(self):
        with self.assertRaisesRegex(ValueError,'ACTUAL_CURRENT_DAY'):
            daily.run('preopen','20261027')
        self.clock.return_value=pd.Timestamp('2026-10-25T08:20:00+08:00')
        self.assertEqual(self.main(),0)
        self.factory.assert_not_called()

    def test_session_timeout_uses_remaining_window_without_extending_cutoff(self):
        self.clock.return_value=pd.Timestamp('2026-10-28T09:19:30+08:00')
        self.assertEqual(self.main(),0)
        self.assertEqual(self.factory.call_args.kwargs['total_seconds'],25)

    def test_old_failed_transport_checkpoint_is_retained_and_does_not_latch_new_day(self):
        old_day='20261027'
        proof=self.root/'reports'/('data_only_'+old_day)/'identity/full/checkpoint.json'
        daily.local.write_json(proof,dict(schema='RQDATA_IDENTITY_FULL_V1',mode='FULL_HTTP_RESPONSE',
            status='FAILED',business_date=old_day,method='all_instruments',
            parameters=dict(type='CS',date=old_day,market='cn'),
            error_type='HTTPSTransportError',details=dict(error_type='gaierror')))
        error=self.ops/(old_day+'_preopen_last_error.json')
        daily.local.write_json(error,dict(date=old_day,phase='preopen',transport=daily.TRANSPORT,
            error_type='InputBlocked',error='IDENTITY_FULL_CHECKPOINT_CONTRACT_INVALID',
            source_block_scope='preopen',nonretryable=True))
        previous=(proof.read_bytes(),error.read_bytes())
        self.assertEqual(self.main(),0)
        self.assertEqual((proof.read_bytes(),error.read_bytes()),previous)

    def test_real_contract_block_is_not_ignored_as_an_old_transport_failure(self):
        daily.local.write_json(self.ops/'20261027_preopen_last_error.json',dict(date='20261027',
            phase='preopen',transport=daily.TRANSPORT,error_type='InputBlocked',
            error='IDENTITY_FULL_CHECKPOINT_CONTRACT_INVALID',source_block_scope='preopen',nonretryable=True))
        self.assertEqual(self.main(),3);self.factory.assert_not_called()

    def test_lock_contention_does_not_latch_a_source_error(self):
        with patch.object(daily.msvcrt,'locking',side_effect=OSError('FAKE_OTHER_INSTANCE')):
            self.assertEqual(self.main(),3)
        self.factory.assert_not_called()
        self.assertEqual(list(self.ops.glob('*_last_error.json')),[])


if __name__=='__main__':unittest.main()
