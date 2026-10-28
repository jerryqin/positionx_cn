"""Requests receiver only; all Sessions/streams/timers are local fake objects.

No credential entry is read, no real connection is possible, and temporary
outputs are owned and removed. Existing transport suites are not executed.
"""
import errno
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from urllib3 import exceptions as urllib_errors

from core import rqdata_https as https


BODY = b'order_book_id,symbol\n000001.XSHE,example\n'


class FakeRaw:
    def __init__(self, parts, *, extra_on_error=0, version=11):
        self.parts=list(parts)
        self.extra_on_error=extra_on_error
        self.version=version
        self.received=0
        self.stream_calls=[]
        self.enforce_content_length=False

    def stream(self, size, *, decode_content):
        self.stream_calls.append((size,decode_content))
        for part in self.parts:
            if callable(part):
                part()
                continue
            if isinstance(part,Exception):
                self.received+=self.extra_on_error
                raise part
            self.received+=len(part)
            yield part

    def tell(self):
        return self.received


class FakeResponse:
    def __init__(self, parts=(BODY,), *, headers=None, status=200, extra_on_error=0, version=11):
        self.raw=FakeRaw(parts,extra_on_error=extra_on_error,version=version)
        self.headers={'Content-Length':str(len(BODY))} if headers is None else headers
        self.status_code=status
        self.closes=0

    def close(self):
        self.closes+=1


class FakeSession:
    def __init__(self,response=None,error=None):
        self.response=response
        self.error=error
        self.trust_env=True
        self.proxies={'https':'UNAUTHORIZED_TEST_PROXY'}
        self.mounts={}
        self.posts=[]
        self.closes=0

    def mount(self,key,adapter):
        self.mounts[key]=adapter

    def post(self,*args,**kwargs):
        self.posts.append((args,kwargs))
        if self.error:raise self.error
        return self.response

    def close(self):
        self.closes+=1
        for adapter in self.mounts.values():adapter.close()


class RequestsReceiverTests(unittest.TestCase):
    def setUp(self):
        self.callbacks=[]
        self.timer=Mock()

    def timer_factory(self,seconds,callback):
        self.assertGreater(seconds,0)
        self.assertLessEqual(seconds,40)
        self.callbacks.append(callback)
        return self.timer

    def invoke(self,provider,session,path='/api',payload=None):
        with patch.object(requests,'Session',return_value=session), \
             patch.object(https.threading,'Timer',side_effect=self.timer_factory), \
             patch.object(https.http.client,'HTTPSConnection',side_effect=AssertionError('No legacy fallback')):
            return provider._post_requests(path,payload or {'method':'instruments','order_book_ids':['000001.XSHE']})

    def provider(self):
        value=https.RQDataHTTPSProvider()
        value._token='FAKE_MEMORY_TOKEN_ONLY'
        value._license='FAKE_MEMORY_LICENSE_ONLY'
        return value

    def test_complete_standard_stream_uses_verified_context_without_proxy_retry_or_redirect(self):
        response=FakeResponse(parts=[BODY[:3],BODY[3:]])
        session=FakeSession(response)
        provider=self.provider()
        status,body=self.invoke(provider,session)
        self.assertEqual((status,body),(200,BODY))
        self.assertFalse(session.trust_env)
        self.assertEqual(session.proxies,{})
        args,kwargs=session.posts[0]
        self.assertEqual(args,('https://rqdata.ricequant.com/api',))
        self.assertEqual(kwargs['headers']['token'],provider._token)
        self.assertEqual(kwargs['headers']['Connection'],'close')
        self.assertEqual(kwargs['headers']['Accept-Encoding'],'identity')
        self.assertIs(kwargs['verify'],True)
        self.assertIs(kwargs['allow_redirects'],False)
        self.assertIs(kwargs['stream'],True)
        self.assertEqual(kwargs['proxies'],{})
        self.assertLessEqual(kwargs['timeout'][0],5)
        self.assertLessEqual(kwargs['timeout'][1],20)
        adapter=session.mounts['https://']
        self.assertTrue(all(getattr(adapter.max_retries,name)==0 for name in
                            ('total','connect','read','redirect','status','other')))
        context=adapter.poolmanager.connection_pool_kw['ssl_context']
        self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertIsNone(context.keylog_filename)
        self.assertGreater(len(context.get_ca_certs()),0)
        self.assertEqual(adapter.poolmanager.connection_pool_kw['assert_hostname'],'rqdata.ricequant.com')
        self.assertEqual(response.raw.stream_calls,[(65536,False)])
        self.assertTrue(response.raw.enforce_content_length)
        metadata=provider._response_metadata
        self.assertEqual(metadata['backend'],'REQUESTS_URLLIB3')
        self.assertEqual(metadata['http_client_bytes'],len(BODY))
        self.assertEqual(metadata['saved_body_bytes'],len(BODY))
        self.assertEqual(metadata['end_mode'],'CONTENT_LENGTH_SATISFIED')
        self.assertTrue(metadata['transfer_complete'])
        self.assertFalse(metadata['incomplete'])
        self.assertEqual(session.closes,1)
        self.assertEqual(response.closes,1)
        self.timer.cancel.assert_called_once()

    def test_each_request_owns_a_new_session_and_preserves_one_memory_token(self):
        sessions=[FakeSession(FakeResponse()),FakeSession(FakeResponse())]
        provider=self.provider()
        with patch.object(requests,'Session',side_effect=sessions) as factory, \
             patch.object(https.threading,'Timer',side_effect=self.timer_factory):
            for _ in range(2):provider._post_requests('/api',{'method':'instruments'})
        self.assertEqual(factory.call_count,2)
        for session in sessions:
            self.assertEqual(len(session.posts),1)
            self.assertEqual(session.posts[0][1]['headers']['token'],provider._token)
            self.assertEqual(session.closes,1)

    def test_short_body_is_not_declared_complete(self):
        response=FakeResponse(headers={'Content-Length':str(len(BODY)+7)})
        provider=self.provider()
        self.assertEqual(self.invoke(provider,FakeSession(response)),(200,BODY))
        self.assertTrue(provider._response_metadata['incomplete'])
        self.assertFalse(provider._response_metadata['transfer_complete'])
        self.assertEqual(provider._response_metadata['end_mode'],'SHORT_CONTENT_LENGTH')

    def test_client_received_bytes_are_distinct_from_saved_prefix_on_stream_failure(self):
        cause=urllib_errors.IncompleteRead(20,80)
        error=urllib_errors.ProtocolError('UNSAFE_FAKE_MEMORY_TOKEN_ONLY',cause)
        response=FakeResponse(parts=[b'prefix',error],headers={'Content-Length':'100'},extra_on_error=14)
        provider=self.provider()
        with self.assertRaises(https.HTTPSTransportError) as caught:
            self.invoke(provider,FakeSession(response))
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.details['failure_stage'],'READ_BODY')
        self.assertIn('IncompleteRead',caught.exception.details['cause_types'])
        metadata=provider._response_metadata
        self.assertEqual(metadata['http_client_bytes'],20)
        self.assertEqual(metadata['saved_body_bytes'],6)
        self.assertEqual(metadata['retained_prefix_bytes'],6)
        self.assertEqual(provider._wire_body,b'prefix')
        self.assertFalse(metadata['transfer_complete'])
        self.assertNotIn('UNSAFE_',json.dumps(metadata))
        self.assertNotIn('FAKE_MEMORY_TOKEN_ONLY',str(caught.exception))

    def test_tls_errors_stop_and_nested_reset_retains_only_safe_class_and_errno(self):
        provider=self.provider()
        with self.assertRaises(https.HTTPSSecurityError):
            self.invoke(provider,FakeSession(error=requests.exceptions.SSLError('UNSAFE_CERT_TOKEN')))
        self.assertTrue(provider._stopped)
        self.assertNotIn('UNSAFE_CERT_TOKEN',json.dumps(provider._response_metadata))
        reset=ConnectionResetError(errno.ECONNRESET,'UNSAFE_RESET_TOKEN')
        reset.winerror=10054
        provider=self.provider()
        with self.assertRaises(https.HTTPSTransportError) as caught:
            self.invoke(provider,FakeSession(error=requests.exceptions.ConnectionError(reset)))
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.details['errno'],errno.ECONNRESET)
        self.assertEqual(caught.exception.details['winerror'],10054)
        self.assertIn('ConnectionResetError',caught.exception.details['cause_types'])
        self.assertNotIn('UNSAFE_RESET_TOKEN',json.dumps(provider._response_metadata))

    def test_response_timeout_is_recoverable_and_connect_timeout_is_not(self):
        for error,stage,retryable in [
                (requests.exceptions.ReadTimeout('UNSAFE_RESPONSE'),'READ_HEADERS',True),
                (requests.exceptions.ConnectTimeout('UNSAFE_CONNECT'),'CONNECT_TLS',False)]:
            with self.subTest(stage=stage):
                provider=self.provider()
                with self.assertRaises(https.HTTPSTransportError) as caught:
                    self.invoke(provider,FakeSession(error=error))
                self.assertEqual(caught.exception.retryable,retryable)
                self.assertEqual(caught.exception.details['failure_stage'],stage)
                self.assertNotIn('UNSAFE_',json.dumps(provider._response_metadata))

    def test_watchdog_prevents_success_after_deadline_and_global_deadline_makes_no_post(self):
        response=FakeResponse(parts=[lambda:self.callbacks[0](),BODY])
        session=FakeSession(response)
        provider=self.provider()
        with self.assertRaisesRegex(https.HTTPSTransportError,'OVERALL_TIMEOUT'):
            self.invoke(provider,session)
        self.assertTrue(provider._response_metadata['timer_fired'])
        self.assertFalse(provider._response_metadata['transfer_complete'])
        self.assertGreaterEqual(response.closes,1)
        provider=self.provider();provider.deadline=0
        session=FakeSession(FakeResponse())
        with self.assertRaisesRegex(https.HTTPSSecurityError,'OVERALL_DEADLINE'):
            self.invoke(provider,session)
        self.assertEqual(session.posts,[])

    def test_declared_size_and_unsupported_encoding_stop_before_body_read(self):
        for headers,error in [({'Content-Length':str(128*1024*1024+1)},https.HTTPSSecurityError),
                              ({'Content-Encoding':'gzip'},https.HTTPSContractError)]:
            with self.subTest(headers=headers):
                response=FakeResponse(headers=headers)
                provider=self.provider()
                with self.assertRaises(error):self.invoke(provider,FakeSession(response))
                self.assertEqual(response.raw.stream_calls,[])

    def test_auth_body_is_memory_only_and_not_added_to_safe_framing(self):
        body=b'FAKE_AUTH_RESPONSE_TOKEN_ONLY'
        response=FakeResponse(parts=[body],headers={'Content-Length':str(len(body))})
        with tempfile.TemporaryDirectory() as temporary:
            provider=https.RQDataHTTPSProvider(output=Path(temporary)/'output')
            self.assertEqual(self.invoke(provider,FakeSession(response),'/auth',
                dict(user_name='license',password='FAKE_TEST_LICENSE_ONLY')),(200,body))
            self.assertEqual(list(Path(temporary).iterdir()),[])
            self.assertNotIn('FAKE_AUTH_RESPONSE_TOKEN_ONLY',json.dumps(provider._response_metadata))


if __name__=='__main__':
    unittest.main()
