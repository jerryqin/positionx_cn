"""Legacy batch identity recovery with fake HTTPS/provider and owned temp files.

No actual credential read, network, task, production database, or Git access.
Existing suites and research outputs are not imported or executed.
"""
import errno
import hashlib
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from core import p0_rq_input as rq
from core import rqdata_https as https
from core import rqdata_identity as identity


NOW = pd.Timestamp('2026-10-28T12:00:00+08:00')
DAY = '20261028'
FIELDS = ['order_book_id', 'symbol', 'listed_date', 'de_listed_date',
          'special_type', 'round_lot', 'status', 'type']


def frame_for(ids):
    return pd.DataFrame([dict(order_book_id=symbol, symbol='FAKE_'+symbol,
        listed_date='2000-01-01', de_listed_date='0000-00-00', special_type='Normal',
        round_lot=100, status='Active', type='CS') for symbol in ids], columns=FIELDS)


def connection(body=b''):
    value=Mock()
    response=value.getresponse.return_value
    response.status=200
    response.length=len(body)
    response.chunked=False
    response.will_close=True
    headers={'Content-Length':str(len(body))}
    response.getheader.side_effect=lambda name: headers.get(name)
    response.read.side_effect=[body,b'']
    return value


class TransportClassificationTests(unittest.TestCase):
    def make_provider(self):
        provider=https.RQDataHTTPSProvider()
        provider._token='FAKE_MEMORY_TOKEN_ONLY'
        return provider

    def test_recoverable_class_errno_and_response_stage_are_recorded_without_raw_error(self):
        cases=[('CONNECT_TLS',ConnectionResetError(errno.ECONNRESET,'UNSAFE_ERROR_DETAIL'),True),
               ('READ_HEADERS',TimeoutError('UNSAFE_ERROR_DETAIL'),True),
               ('READ_BODY',TimeoutError('UNSAFE_ERROR_DETAIL'),True),
               ('READ_HEADERS',https.http.client.RemoteDisconnected('UNSAFE_ERROR_DETAIL'),True),
               ('READ_BODY',https.http.client.IncompleteRead(b'partial',8),True),
               ('CONNECT_TLS',TimeoutError('UNSAFE_ERROR_DETAIL'),False),
               ('SEND_REQUEST',OSError(errno.EINVAL,'UNSAFE_ERROR_DETAIL'),False)]
        for stage,error,retryable in cases:
            with self.subTest(stage=stage,error_type=type(error).__name__):
                conn=connection(b'not-read')
                if stage=='CONNECT_TLS':conn.connect.side_effect=error
                elif stage=='SEND_REQUEST':conn.request.side_effect=error
                elif stage=='READ_HEADERS':conn.getresponse.side_effect=error
                else:conn.getresponse.return_value.read.side_effect=error
                provider=self.make_provider()
                with patch.object(https.http.client,'HTTPSConnection',return_value=conn), \
                     patch.object(https.threading,'Timer'):
                    with self.assertRaises(https.HTTPSTransportError) as caught:
                        provider._post('/api',dict(method='instruments',order_book_ids=['000001.XSHE']))
                self.assertEqual(caught.exception.retryable,retryable)
                self.assertEqual(caught.exception.details['error_type'],type(error).__name__)
                self.assertEqual(caught.exception.details['failure_stage'],stage)
                if isinstance(getattr(error,'errno',None),int):
                    self.assertEqual(caught.exception.details['errno'],error.errno)
                self.assertEqual(provider._response_metadata['failure_stage'],stage)
                self.assertNotIn('UNSAFE_ERROR_DETAIL',json.dumps(provider._response_metadata))
                self.assertNotIn('UNSAFE_ERROR_DETAIL',str(caught.exception))
                self.assertFalse(provider._response_metadata['transfer_complete'])
                if isinstance(error,https.http.client.IncompleteRead):
                    self.assertEqual(provider._wire_body,b'partial')

    def test_certificate_and_unclassified_tls_failures_are_security_stops(self):
        cert=ssl.SSLCertVerificationError(1,'UNSAFE_CERT_DETAIL')
        cert.verify_code=62
        for error in (cert,ssl.SSLError(1,'UNSAFE_TLS_DETAIL')):
            with self.subTest(error_type=type(error).__name__):
                conn=connection();conn.connect.side_effect=error
                provider=self.make_provider()
                with patch.object(https.http.client,'HTTPSConnection',return_value=conn), \
                     patch.object(https.threading,'Timer'):
                    with self.assertRaises(https.HTTPSSecurityError):
                        provider._post('/api',dict(method='instruments'))
                self.assertTrue(provider._stopped)
                self.assertEqual(provider._response_metadata['error_type'],type(error).__name__)
                self.assertEqual(provider._response_metadata['failure_stage'],'CONNECT_TLS')
                if error is cert:self.assertEqual(provider._response_metadata['verify_code'],62)
                self.assertNotIn('UNSAFE_',json.dumps(provider._response_metadata))

    def test_explicit_identity_second_attempt_uses_new_connection_and_same_memory_token(self):
        first=connection();first.getresponse.side_effect=ConnectionResetError(errno.ECONNRESET,'UNSAFE_DETAIL')
        raw=frame_for(['000001.XSHE']).to_csv(index=False).encode('utf-8')
        second=connection(raw)
        provider=self.make_provider()
        token=provider._token
        with patch.object(https.http.client,'HTTPSConnection',side_effect=[first,second]) as factory, \
             patch.object(https.threading,'Timer'):
            with self.assertRaises(https.HTTPSTransportError) as error:
                provider.call('instruments',_identity_request=True,order_book_ids=['000001.XSHE'])
            self.assertTrue(error.exception.retryable)
            self.assertEqual(factory.call_count,1)
            result=provider.call('instruments',_identity_request=True,order_book_ids=['000001.XSHE'])
        self.assertEqual(result.order_book_id.tolist(),['000001.XSHE'])
        self.assertEqual(factory.call_count,2)
        self.assertEqual(provider.state['auth_attempts'],0)
        self.assertEqual(provider.state['identity_api_attempts'],2)
        self.assertEqual(provider.state['data_api_attempts'],0)
        self.assertIs(provider._token,token)
        for conn in (first,second):
            self.assertEqual(conn.request.call_args.kwargs['headers']['token'],token)
            conn.close.assert_called_once()


class FakeProvider:
    def __init__(self,outcomes=()):
        self.outcomes=list(outcomes)
        self.calls=[]
        self._token='FAKE_MEMORY_TOKEN_ONLY'
        self._license=None
        self._wire_body=b''
        self._last_api_body=None
        self._response_metadata={}
        self.state={'identity_api_attempts':0}

    def call(self,method,**params):
        if method!='instruments' or params.pop('_identity_request',None) is not True:
            raise AssertionError('Only the exact identity call is allowed')
        self.calls.append(list(params['order_book_ids']))
        self.state['identity_api_attempts']+=1
        self._wire_body=b''
        self._last_api_body=None
        self._response_metadata=dict(content_encoding='identity',transfer_complete=False,incomplete=True)
        action=self.outcomes.pop(0) if self.outcomes else None
        if isinstance(action,Exception):raise action
        frame=action if isinstance(action,pd.DataFrame) else frame_for(params['order_book_ids'])
        body=frame.to_csv(index=False).encode('utf-8')
        self._wire_body=self._last_api_body=body
        self._response_metadata=dict(content_encoding='identity',content_length=len(body),
            coded_body_bytes=len(body),decoded_body_bytes=len(body),transfer_complete=True,incomplete=False)
        return frame


def reset_error():
    return https.HTTPSTransportError('HTTPS_TRANSPORT_ConnectionResetError_READ_HEADERS',
        retryable=True,details={'error_type':'ConnectionResetError','errno':errno.ECONNRESET,
                                'failure_stage':'READ_HEADERS'})


class LegacyBatchIdentityCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name).resolve()
        self.out=self.root/'reports/capture'
        self.ids=[f'{number:06}.XSHE' for number in range(1,502)]
        self.target=dict(day=DAY,ids=self.ids,fields=FIELDS,
            target_list_sha256=hashlib.sha256(json.dumps(self.ids).encode()).hexdigest(),
            source={'manifest':str(self.root/'qualified-target.json'),'attributes_not_reused':True})
        for patcher in (patch.object(identity,'ROOT',self.root),
                        patch.object(rq,'clock',return_value=NOW),
                        patch.object(identity.time,'sleep')):
            value=patcher.start();self.addCleanup(patcher.stop)
            if patcher.attribute=='sleep':self.sleep=value
        self.loader=patch.object(identity,'target_list',return_value=self.target)
        self.target_loader=self.loader.start();self.addCleanup(self.loader.stop)

    def capture(self,provider,**kwargs):
        return identity.capture_identity_batches(provider,DAY,self.out,**kwargs)

    def state(self):
        return json.loads((self.out/'checkpoint.json').read_text(encoding='utf-8'))

    def test_single_reset_recovery_and_completed_checkpoint_reuse_are_bounded_and_immutable(self):
        provider=FakeProvider([reset_error(),None,None])
        result=self.capture(provider,max_calls=3)
        self.assertEqual(len(result),501)
        self.assertEqual([len(ids) for ids in provider.calls],[500,500,1])
        self.sleep.assert_called_once_with(.5)
        state=self.state()
        self.assertEqual(state['status'],'COMPLETE')
        self.assertEqual([len(b['attempts']) for b in state['batches']],[2,1])
        originals={path.name:path.read_bytes() for path in self.out.glob('*.raw.csv')}
        checkpoint=(self.out/'checkpoint.json').read_bytes()
        combined=(self.out/'identity.complete.parquet').read_bytes()
        with patch.object(rq,'clock',return_value=NOW+pd.Timedelta(minutes=1)):
            again=self.capture(provider,max_calls=0)
        self.assertEqual(again.order_book_id.tolist(),self.ids)
        self.assertEqual(len(provider.calls),3)
        self.assertEqual((self.out/'checkpoint.json').read_bytes(),checkpoint)
        self.assertEqual((self.out/'identity.complete.parquet').read_bytes(),combined)
        self.assertTrue(all((self.out/name).read_bytes()==body for name,body in originals.items()))
        self.assertFalse(again.attrs['identity_capture']['atomic_snapshot'])

    def test_two_transport_failures_stop_batch_and_later_call_does_not_reset_attempts(self):
        provider=FakeProvider([reset_error(),reset_error()])
        with self.assertRaises(https.HTTPSTransportError):self.capture(provider,max_calls=9)
        self.assertEqual(len(provider.calls),2)
        self.assertEqual(self.state()['batches'][0]['status'],'FAILED')
        self.assertEqual(len(self.state()['batches'][0]['attempts']),2)
        self.assertFalse((self.out/'identity.complete.parquet').exists())
        with self.assertRaisesRegex(rq.InputBlocked,'EXPLICIT_RECOVERY'):
            self.capture(provider,max_calls=9)
        self.assertEqual(len(provider.calls),2)
        self.sleep.assert_called_once_with(.5)

    def test_nonrecoverable_security_unknown_and_contract_errors_do_not_retry(self):
        errors=[https.HTTPSSecurityError('HTTPS_TLS_VERIFICATION_OR_PROTOCOL_FAILURE'),
                https.HTTPSAuthError('HTTPS_API_AUTH_OR_REDIRECT_REJECTED_403'),
                https.HTTPSContractError('instruments:HTTP_400'),
                https.HTTPSTransportError('UNKNOWN_OS_ERROR',retryable=False,
                    details={'error_type':'OSError','errno':errno.EINVAL,'failure_stage':'SEND_REQUEST'})]
        for n,error in enumerate(errors):
            with self.subTest(error_type=type(error).__name__):
                provider=FakeProvider([error])
                output=self.root/f'reports/nonretry_{n}'
                with self.assertRaises(type(error)):
                    identity.capture_identity_batches(provider,DAY,output,max_calls=4)
                self.assertEqual(len(provider.calls),1)
                state=json.loads((output/'checkpoint.json').read_text(encoding='utf-8'))
                self.assertEqual(state['batches'][0]['status'],'FAILED')
                self.assertFalse((output/'identity.complete.parquet').exists())
        self.sleep.assert_not_called()

    def test_budget_stops_before_recovery_and_later_call_uses_only_remaining_attempt(self):
        provider=FakeProvider([reset_error(),None,None])
        with self.assertRaisesRegex(rq.InputBlocked,'BUDGET_EXHAUSTED'):
            self.capture(provider,max_calls=1)
        self.assertEqual(len(provider.calls),1)
        self.assertFalse((self.out/'identity.complete.parquet').exists())
        result=self.capture(provider,max_calls=2)
        self.assertEqual(len(result),501)
        self.assertEqual(len(provider.calls),3)
        self.assertEqual(len(self.state()['batches'][0]['attempts']),2)

    def test_resume_rejects_different_business_date_or_target_pool_before_request(self):
        provider=FakeProvider()
        with self.assertRaisesRegex(rq.InputBlocked,'BUDGET_EXHAUSTED'):
            self.capture(provider,max_calls=0)
        with self.assertRaisesRegex(rq.InputBlocked,'CHECKPOINT_DATE_LIST_OR_FIELDS_MISMATCH'):
            identity.capture_identity_batches(provider,'20261029',self.out,max_calls=2)
        changed=dict(self.target,target_list_sha256='DIFFERENT_TARGET_LIST')
        self.target_loader.return_value=changed
        with self.assertRaisesRegex(rq.InputBlocked,'CHECKPOINT_DATE_LIST_OR_FIELDS_MISMATCH'):
            self.capture(provider,max_calls=2)
        self.assertEqual(provider.calls,[])

    def test_same_flat_pool_with_changed_batch_boundaries_is_rejected_before_api(self):
        provider=FakeProvider()
        with self.assertRaisesRegex(rq.InputBlocked,'BUDGET_EXHAUSTED'):
            self.capture(provider,max_calls=0)
        initial=self.state()
        merged=json.loads(json.dumps(initial))
        merged['batches']=[dict(number=1,ids=list(self.ids),status='PENDING',attempts=[])]
        shifted=json.loads(json.dumps(initial))
        shifted['batches'][0]['ids']=self.ids[:499]
        shifted['batches'][1]['ids']=self.ids[499:]
        renumbered=json.loads(json.dumps(initial))
        renumbered['batches'][1]['number']=7
        for label,state in [('merged_over_500',merged),('shifted_boundary',shifted),('nonconsecutive_number',renumbered)]:
            with self.subTest(label=label):
                self.assertEqual([symbol for batch in state['batches'] for symbol in batch['ids']],self.ids)
                (self.out/'checkpoint.json').write_text(json.dumps(state),encoding='utf-8')
                with self.assertRaisesRegex(rq.InputBlocked,'CHECKPOINT_BATCH_SET_CHANGED'):
                    self.capture(provider,max_calls=2)
                self.assertEqual(provider.calls,[])
                self.assertFalse((self.out/'identity.complete.parquet').exists())
        (self.out/'checkpoint.json').write_text(json.dumps(merged),encoding='utf-8')
        seeded=self.root/'reports/invalid_batch_count_seed'
        with self.assertRaisesRegex(rq.InputBlocked,'SEED_BATCH_SET_MISMATCH'):
            identity.capture_identity_batches(provider,DAY,seeded,seed=self.out,max_calls=2)
        self.assertEqual(provider.calls,[])
        self.assertFalse((seeded/'identity.complete.parquet').exists())

    def test_response_pool_conflict_is_retained_and_never_completed(self):
        wrong=frame_for(self.ids[:499]+['999999.XSHE'])
        provider=FakeProvider([wrong])
        with self.assertRaisesRegex(rq.InputBlocked,'EXACT_SET_OR_FIELD_CONFLICT'):
            self.capture(provider,max_calls=4)
        self.assertEqual(len(provider.calls),1)
        self.assertEqual(self.state()['batches'][0]['status'],'FAILED')
        self.assertFalse((self.out/'identity.complete.parquet').exists())
        self.sleep.assert_not_called()

    def test_new_output_seed_reuses_successful_batch_original_without_refetch(self):
        provider=FakeProvider()
        with self.assertRaisesRegex(rq.InputBlocked,'BUDGET_EXHAUSTED'):
            self.capture(provider,max_calls=1)
        first=self.state()['batches'][0]
        original=(self.out/first['raw_file']).read_bytes()
        new=self.root/'reports/seeded'
        next_provider=FakeProvider()
        result=identity.capture_identity_batches(next_provider,DAY,new,seed=self.out,max_calls=1)
        self.assertEqual(len(result),501)
        self.assertEqual(next_provider.calls,[self.ids[500:]])
        self.assertEqual((new/first['raw_file']).read_bytes(),original)
        self.assertEqual((self.out/first['raw_file']).read_bytes(),original)

    def test_deadline_reached_before_retry_does_not_sleep_or_make_second_call(self):
        provider=FakeProvider([reset_error()])
        with self.assertRaisesRegex(rq.InputBlocked,'PHASE_DEADLINE_REACHED'):
            self.capture(provider,max_calls=2,deadline=NOW+pd.Timedelta(milliseconds=200))
        self.assertEqual(len(provider.calls),1)
        self.sleep.assert_not_called()
        self.assertFalse((self.out/'identity.complete.parquet').exists())

    def test_auth_failure_without_api_never_persists_partial_auth_body(self):
        class AuthFailureProvider(FakeProvider):
            def call(self,method,**params):
                self.calls.append(params['order_book_ids'])
                self._token=None
                self._wire_body=b'FAKE_PARTIAL_AUTH_TOKEN_NEVER_SAVE'
                self._response_metadata=dict(content_encoding='identity',incomplete=True,
                                             transfer_complete=False,failure_stage='READ_BODY')
                raise https.HTTPSTransportError('AUTH_RESPONSE_TIMEOUT',retryable=True,
                    details={'error_type':'TimeoutError','failure_stage':'READ_BODY'})
        provider=AuthFailureProvider()
        with self.assertRaises(https.HTTPSTransportError):self.capture(provider,max_calls=3)
        self.assertEqual(provider.state['identity_api_attempts'],0)
        self.assertEqual(len(provider.calls),1)
        self.assertEqual(list(self.out.glob('*.raw.csv')),[])
        self.assertFalse((self.out/'identity.complete.parquet').exists())
        for path in self.out.iterdir():
            self.assertNotIn(b'FAKE_PARTIAL_AUTH_TOKEN_NEVER_SAVE',path.read_bytes())


if __name__=='__main__':
    unittest.main()
