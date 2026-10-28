"""Bounded RQData HTTPS transport for the existing data-only consumers.

Official License mapping only; no SDK initialization, proxies, redirects,
automatic retries, token files, or native transport fallback.
"""
from __future__ import annotations

import ast
import csv
import errno
from datetime import date, datetime
import http.client
import io
import json
import os
from pathlib import Path
import re
import socket
import ssl
import threading
import time
import zlib

import pandas as pd


class HTTPSContractError(RuntimeError):
    pass


class HTTPSAuthError(RuntimeError):
    pass


class HTTPSSecurityError(RuntimeError):
    pass


class HTTPSNotReady(RuntimeError):
    pass


class HTTPSTransportError(RuntimeError):
    def __init__(self, code, *, retryable=False, details=None):
        super().__init__(code)
        self.retryable = retryable
        self.details = details or {}


METHODS = frozenset(('is_data_ready', 'all_instruments', 'instruments', 'get_price',
    'is_suspended', 'is_st_stock', 'get_tick_size', 'get_ex_factor',
    'get_dividend', 'get_split', 'get_allotment', 'index_components'))
NUMERIC = frozenset(('open', 'high', 'low', 'close', 'prev_close', 'volume',
    'total_turnover', 'limit_up', 'limit_down', 'num_trades', 'tick_size',
    'round_lot', 'ex_factor', 'ex_cum_factor', 'dividend_cash_before_tax',
    'split_coefficient_from', 'split_coefficient_to', 'allotment_ratio',
    'allotment_price', 'proportion', 'allotted_proportion', 'cum_factor', 'allotted_shares',
    'issue_price', 'market_tplus'))
DATES = frozenset(('date', 'datetime', 'ex_date', 'announcement_date',
    'ex_end_date', 'ex_dividend_date', 'book_closure_date', 'payable_date',
    'ex_right_date', 'latest_date', 'expected_date', 'declaration_announcement_date', 'advance_date'))
OPTIONAL_EMPTY = frozenset(('get_ex_factor', 'get_dividend', 'get_split', 'get_allotment'))


def _wire(value):
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.strftime('%Y%m%d')
    if isinstance(value, dict):
        return {k: _wire(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wire(v) for v in value]
    return value


def _members(text):
    """Decode the official tuple serializer without executing response code."""
    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, type(None))):
            return node.value
        if isinstance(node, (ast.Tuple, ast.List)):
            result = [visit(n) for n in node.elts]
            return tuple(result) if isinstance(node, ast.Tuple) else result
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == 'Timestamp' and len(node.args) == 1
                and not node.keywords):
            value = visit(node.args[0])
            if isinstance(value, str):
                return pd.Timestamp(value)
        raise HTTPSContractError('INDEX_COMPONENTS_NATIVE_TIME_SERIALIZATION_UNSUPPORTED')
    try:
        result = visit(ast.parse(text, mode='eval'))
    except HTTPSContractError:
        raise
    except Exception:
        raise HTTPSContractError('INDEX_COMPONENTS_NATIVE_TIME_SERIALIZATION_UNSUPPORTED') from None
    if (not isinstance(result, tuple) or len(result) != 2
            or not isinstance(result[0], list) or not result[0]
            or not all(isinstance(s, str) and re.fullmatch(r'\d{6}\.(XSHG|XSHE|XBSE)', s) for s in result[0])
            or len(set(result[0])) != len(result[0])
            or not isinstance(result[1], pd.Timestamp) or pd.isna(result[1])):
        raise HTTPSContractError('INDEX_COMPONENTS_NATIVE_CREATION_TIME_MISSING')
    return result


def _qualified_empty_action(method, body, params, framing):
    """Only the two observed CN action contracts permit a zero-length body.

    2026-10-28: known nonempty events, known no-event windows, and corrected
    target batches were compared through the original action consumer.
    Evidence: reports/rqdata_https_return_fix_20261028/TEMP/diagnostic/.
    This does not qualify other methods, whitespace CSV, unknown framing,
    custom fields, other markets, HTTP errors, or incomplete transfers.
    """
    if method not in ('get_split', 'get_allotment') or body != b'' or not framing:
        return False
    if (framing.get('transfer_complete') is not True or framing.get('incomplete') is not False
            or framing.get('content_length') != 0 or framing.get('coded_body_bytes') != 0
            or framing.get('decoded_body_bytes') != 0 or framing.get('content_encoding') != 'identity'
            or framing.get('transfer_encoding') != '' or framing.get('end_mode') != 'CONTENT_LENGTH_SATISFIED'):
        return False
    if (set(params)-{'order_book_ids','start_date','end_date','market'}
            or params.get('market', 'cn') != 'cn' or params.get('end_date') is None):
        return False
    ids=params.get('order_book_ids')
    ids=[ids] if isinstance(ids,str) else ids
    if (not isinstance(ids,list) or not ids or len(set(ids)) != len(ids)
            or not all(isinstance(s,str) and re.fullmatch(r'\d{6}\.(XSHG|XSHE|XBSE)',s) for s in ids)):
        return False
    def day(value):
        value=_wire(value)
        if isinstance(value,bool):
            raise ValueError('INVALID_DATE')
        value=str(value)
        return pd.to_datetime(value,format='%Y%m%d') if re.fullmatch(r'\d{8}',value) else pd.Timestamp(value)
    try:
        end=day(params['end_date'])
        start=day(params['start_date']) if params.get('start_date') is not None else None
        return bool(not pd.isna(end) and (start is None or (not pd.isna(start) and start<=end))
                    and (method=='get_allotment' or start is not None))
    except (ValueError,TypeError,OverflowError):
        return False


def decode(method, body, params, *, response_framing=None):
    """Restore the installed SDK consumer shape, never fill missing status."""
    text = body.decode('utf-8-sig').strip()
    if method == 'index_components' and params.get('return_create_tm'):
        return _members(text)
    if text in ('null', 'None') and method in OPTIONAL_EMPTY:
        return None
    if not text:
        if _qualified_empty_action(method, body, params, response_framing):
            return None
        raise HTTPSContractError(method + ':EMPTY_HTTP_BODY_NOT_QUALIFIED_EMPTY_SET')
    try:
        records = csv.reader(io.StringIO(text), strict=True)
        header = next(records)
        if len(header) != len(set(header)):
            raise HTTPSContractError(method + ':DUPLICATE_COLUMNS')
        if any(len(row) != len(header) for row in records):
            raise HTTPSContractError(method + ':CSV_RECORD_WIDTH_INVALID')
        frame = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    except HTTPSContractError:
        raise
    except Exception:
        raise HTTPSContractError(method + ':CSV_RESPONSE_INVALID') from None
    if frame.columns.duplicated().any():
        raise HTTPSContractError(method + ':DUPLICATE_COLUMNS')
    if method == 'index_components':
        if list(frame.columns) != ['order_book_id'] or frame.empty:
            raise HTTPSContractError('INDEX_COMPONENTS_CSV_INVALID')
        return frame.order_book_id.tolist()
    if method in ('is_suspended', 'is_st_stock'):
        if 'date' not in frame or frame.empty:
            raise HTTPSContractError(method + ':DATED_STATUS_MISSING')
        requested = params['order_book_ids']
        requested = [requested] if isinstance(requested, str) else requested
        if set(frame.columns) != {'date', *requested}:
            raise HTTPSContractError(method + ':STATUS_POOL_MISMATCH')
        frame['date'] = pd.to_datetime(frame['date'], errors='raise')
        for col in requested:
            if not frame[col].isin(['True', 'False']).all():
                raise HTTPSContractError(method + ':UNKNOWN_BOOLEAN')
            frame[col] = frame[col].eq('True')
        return frame.set_index('date')
    if method == 'get_price' and 'datetime' in frame:
        if 'date' in frame:
            raise HTTPSContractError('PRICE_AMBIGUOUS_DATE')
        frame = frame.rename(columns={'datetime': 'date'})
    if method == 'get_ex_factor' and 'rice_create_tm' in frame:
        values = frame.rice_create_tm
        if not values.map(lambda s: bool(re.fullmatch(r'-?\d+', s))).all():
            # HTTP exposes a native datetime string instead of the SDK's raw
            # integer. Preserve it separately; neither UTC nor CST is inferred.
            if not values.map(lambda s: s == '' or bool(re.fullmatch(
                    r'\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?', s))).all():
                raise HTTPSContractError('FACTOR_RAW_CREATION_TYPE_CHANGED')
            pd.to_datetime(values.replace('', None), errors='raise')
            frame = frame.rename(columns={'rice_create_tm': 'rice_create_tm_http_native'})
    frame = frame.replace('', None)
    for col in frame:
        if col == 'rice_create_tm' and method == 'get_ex_factor':
            if not frame[col].map(lambda s: bool(re.fullmatch(r'-?\d+', s))).all():
                raise HTTPSContractError('FACTOR_RAW_CREATION_TYPE_CHANGED')
            frame[col] = pd.to_numeric(frame[col], errors='raise').astype('int64')
        elif col == 'rice_create_tm':
            # Non-factor raw action timestamps retain the provider's native
            # datetime representation; never substitute our response clock.
            frame[col] = pd.to_datetime(frame[col].replace({'': None, 'NaT': None}), errors='raise')
        elif col in NUMERIC:
            frame[col] = pd.to_numeric(frame[col].replace('', None), errors='raise')
        elif col in DATES:
            frame[col] = pd.to_datetime(frame[col].replace({'': None, 'NaT': None}), errors='raise')
    if method == 'is_data_ready':
        required = {'market', 'category', 'ready', 'latest_date', 'expected_date'}
        if not required.issubset(frame) or frame.empty or not frame.ready.isin(['True', 'False']).all():
            raise HTTPSContractError('DATA_READY_SCHEMA_INVALID')
        frame['ready'] = frame.ready.eq('True')
        return frame.set_index(['market', 'category'])
    if method in ('all_instruments', 'instruments'):
        required = {'order_book_id', 'symbol', 'listed_date', 'de_listed_date', 'status', 'special_type', 'round_lot'}
    elif method == 'get_price':
        required = {'order_book_id', 'date', *params.get('fields', [])}
    elif method == 'get_tick_size':
        required = {'order_book_id', 'tick_size'}
    elif method == 'get_ex_factor':
        required = {'order_book_id', 'ex_date', 'ex_factor', 'ex_cum_factor', 'announcement_date', 'ex_end_date'}
    else:
        required = {'order_book_id', 'ex_right_date' if method == 'get_allotment' else 'ex_dividend_date'}
    if not required.issubset(frame) or (frame.empty and method not in OPTIONAL_EMPTY):
        raise HTTPSContractError(method + ':REQUIRED_COLUMNS_OR_RECORDS_MISSING')
    if method == 'get_tick_size':
        requested = params['order_book_ids']
        requested = [requested] if isinstance(requested, str) else requested
        if (frame.order_book_id.duplicated().any() or set(frame.order_book_id) != set(requested)
                or frame.tick_size.isna().any() or not frame.tick_size.gt(0).all()):
            raise HTTPSContractError('TICK_IDENTITY_OR_VALUE_CONTRACT_INVALID')
        return frame.set_index('order_book_id').tick_size
    return frame


class RQDataHTTPSProvider:
    source_version = 'RQDATA_HTTPS_V1'
    host = 'rqdata.ricequant.com'

    def __init__(self, *, output=None, api_limit=16, total_seconds=240, transport=None):
        self.output = Path(output).resolve() if output else None
        self.api_limit = min(int(api_limit), 16)
        self.deadline = time.monotonic() + total_seconds
        self._token = None
        self._license = None
        self._transport = transport
        self._stopped = False
        self._blocked_methods = set()
        self._response_metadata = {}
        self.state = {'source': self.source_version, 'auth_attempts': 0, 'api_attempts': 0,
                      'identity_api_attempts': 0, 'data_api_attempts': 0,
                      'calls': [], 'started_at': datetime.now().astimezone().isoformat()}

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise HTTPSSecurityError('HTTPS_OVERALL_DEADLINE_NO_RETRY')
        return remaining

    def _save(self):
        if self.output:
            self.output.mkdir(parents=True, exist_ok=True)
            (self.output / 'HTTPS_SESSION.json').write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')

    def _safe_error(self, body):
        try:
            value = json.loads(body.decode('utf-8'))
        except Exception:
            return 'UNSTRUCTURED_ERROR_BODY_OMITTED'
        result = {}
        if isinstance(value, dict):
            for key in ('code', 'error_code', 'error', 'message', 'msg', 'detail'):
                item = value.get(key)
                if isinstance(item, (str, int)) and not isinstance(item, bool):
                    item = str(item)
                    for secret in (self._license, self._token):
                        if secret:
                            item = item.replace(secret, '[REDACTED]')
                    item = re.sub(r'[A-Za-z0-9_+/=.-]{32,}', '[REDACTED_OPAQUE_VALUE]', item)
                    result[key] = ''.join(c for c in item if c.isprintable())[:400]
        return result or 'ERROR_BODY_OMITTED'

    def _post_requests(self, path, payload):
        """Standard Requests receiver, explicit opt-in until live qualification.

        Each call owns a new Session. Only safe framing and exception types are
        retained; authentication bodies and tokens are never written here.
        """
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        from urllib3 import exceptions as urllib_errors

        if self.host != 'rqdata.ricequant.com' or path not in ('/auth', '/api'):
            raise HTTPSSecurityError('HTTPS_HOST_OR_PATH_OUTSIDE_AUTHORIZATION')
        self._response_metadata = dict(backend='REQUESTS_URLLIB3', transfer_complete=False)
        self._wire_body = b''
        self._remaining()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cafile=requests.certs.where())
        context.set_alpn_protocols(['http/1.1'])
        if context.keylog_filename is not None:
            raise HTTPSSecurityError('TLS_KEY_LOGGING_FORBIDDEN')

        class VerifiedAdapter(HTTPAdapter):
            def init_poolmanager(adapter, *args, **kwargs):
                kwargs.update(ssl_context=context, assert_hostname='rqdata.ricequant.com')
                return super().init_poolmanager(*args, **kwargs)

            def proxy_manager_for(adapter, *args, **kwargs):
                raise HTTPSSecurityError('HTTPS_PROXY_NOT_AUTHORIZED')

        session = requests.Session()
        session.trust_env = False
        session.proxies = {}
        session.mount('https://', VerifiedAdapter(pool_connections=1, pool_maxsize=1,
            max_retries=Retry(total=0, connect=0, read=0, redirect=0, status=0, other=0)))
        holder = [None]
        expired = threading.Event()
        request_deadline = min(self.deadline, time.monotonic() + 40.0)
        content = bytearray()
        phase = 'CONNECT_OR_HEADERS'
        size_limit = 128 * 1024 * 1024

        def remaining():
            global_remaining = self._remaining()
            seconds = request_deadline - time.monotonic()
            if expired.is_set() or seconds <= 0:
                raise HTTPSTransportError('HTTPS_REQUEST_OVERALL_TIMEOUT', retryable=True,
                    details=dict(error_type='TimeoutError', failure_stage=phase))
            return min(global_remaining, seconds)

        def cancel():
            expired.set()
            if holder[0] is not None:
                holder[0].close()
            session.close()

        def counts():
            actual = None
            if holder[0] is not None:
                try:
                    value = holder[0].raw.tell()
                    if isinstance(value, int) and not isinstance(value, bool):
                        actual = value
                except Exception:
                    pass
            self._wire_body = bytes(content)
            self._response_metadata.update(http_client_bytes=actual,
                saved_body_bytes=len(content), retained_prefix_bytes=len(content),
                bytes_received=actual if actual is not None else len(content),
                coded_body_bytes=len(content), timer_fired=expired.is_set())
            return actual

        timer = None
        try:
            timer = threading.Timer(remaining(), cancel)
            timer.daemon = True
            timer.start()
            headers = {'Content-Type': 'application/json', 'Connection': 'close',
                       'Accept-Encoding': 'identity'}
            if path == '/api':
                headers['token'] = self._token
            response = session.post('https://rqdata.ricequant.com' + path,
                json=_wire(payload), headers=headers, verify=True, allow_redirects=False,
                proxies={}, timeout=(min(5.0, remaining()), min(20.0, remaining())), stream=True)
            holder[0] = response
            phase = 'READ_HEADERS'
            raw_length = response.headers.get('Content-Length')
            declared = int(raw_length) if raw_length is not None and raw_length.isdigit() else None
            encoding = (response.headers.get('Content-Encoding') or 'identity').strip().lower()
            transfer = (response.headers.get('Transfer-Encoding') or '').strip().lower()
            chunked = transfer == 'chunked'
            expected = None if chunked else declared
            self._response_metadata.update(http_status=response.status_code,
                declared_bytes=expected, content_length=declared,
                content_encoding=encoding if encoding in ('identity', 'gzip') else 'UNSUPPORTED',
                transfer_encoding=transfer if transfer in ('', 'chunked', 'identity') else 'UNSUPPORTED',
                chunked=chunked, http_version=response.raw.version,
                connection_will_close=True)
            if response.raw.version != 11:
                raise HTTPSContractError('HTTPS_RESPONSE_HTTP_VERSION_NOT_1_1')
            if expected is not None and expected > size_limit:
                raise HTTPSSecurityError('HTTPS_RESPONSE_SIZE_BOUND_EXCEEDED')
            if encoding != 'identity' or transfer not in ('', 'chunked', 'identity'):
                raise HTTPSContractError('HTTPS_REQUESTS_RESPONSE_ENCODING_UNSUPPORTED')
            response.raw.enforce_content_length = True
            phase = 'READ_BODY'
            for part in response.raw.stream(65536, decode_content=False):
                remaining()
                content.extend(part)
                if len(content) > size_limit:
                    raise HTTPSSecurityError('HTTPS_RESPONSE_SIZE_BOUND_EXCEEDED')
            remaining()
            actual = counts()
            incomplete = ((expected is not None and len(content) != expected)
                          or (actual is not None and actual != len(content)))
            self._response_metadata.update(incomplete=incomplete, transfer_complete=not incomplete,
                decoded_body_bytes=len(content), end_mode='SHORT_CONTENT_LENGTH' if incomplete else
                    'CHUNK_TERMINATOR' if chunked else
                    'CONTENT_LENGTH_SATISFIED' if expected is not None else 'CONNECTION_EOF')
            return response.status_code, self._wire_body
        except Exception as exc:
            counts()
            nodes, pending = [], [exc]
            while pending and len(nodes) < 12:
                node = pending.pop(0)
                if not isinstance(node, BaseException) or any(node is old for old in nodes):
                    continue
                nodes.append(node)
                pending.extend([getattr(node, '__cause__', None), getattr(node, '__context__', None),
                                getattr(node, 'reason', None)])
                pending.extend(value for value in node.args if isinstance(value, BaseException))
            if phase == 'CONNECT_OR_HEADERS':
                if any(isinstance(node, (requests.exceptions.ConnectTimeout, urllib_errors.ConnectTimeoutError)) for node in nodes):
                    phase = 'CONNECT_TLS'
                elif any(isinstance(node, (requests.exceptions.ReadTimeout, urllib_errors.ReadTimeoutError,
                                          http.client.RemoteDisconnected)) for node in nodes):
                    phase = 'READ_HEADERS'
            details = dict(error_type=type(exc).__name__, failure_stage=phase,
                           cause_types=[type(node).__name__ for node in nodes[1:]])
            for node in nodes:
                for key in ('errno', 'winerror', 'verify_code'):
                    value = getattr(node, key, None)
                    if isinstance(value, int) and not isinstance(value, bool):
                        details.setdefault(key, value)
            self._response_metadata.update(details, incomplete=True, transfer_complete=False,
                                           end_mode='CONNECTION_ERROR')
            if isinstance(exc, (HTTPSContractError, HTTPSSecurityError)):
                if isinstance(exc, HTTPSSecurityError):
                    self._stopped = True
                raise
            if isinstance(exc, HTTPSTransportError):
                raise
            if any(isinstance(node, (requests.exceptions.SSLError, ssl.SSLError, urllib_errors.SSLError)) for node in nodes):
                self._stopped = True
                raise HTTPSSecurityError('HTTPS_TLS_VERIFICATION_OR_PROTOCOL_FAILURE') from None
            reset = any(isinstance(node, ConnectionResetError) for node in nodes) or details.get('errno') in (errno.ECONNRESET, 10054) or details.get('winerror') == 10054
            response_timeout = phase in ('READ_HEADERS', 'READ_BODY') and any(isinstance(node,
                (TimeoutError, requests.exceptions.ReadTimeout, urllib_errors.ReadTimeoutError)) for node in nodes)
            premature = any(isinstance(node, (http.client.RemoteDisconnected,
                http.client.IncompleteRead, urllib_errors.IncompleteRead)) for node in nodes)
            raise HTTPSTransportError('HTTPS_REQUESTS_' + type(exc).__name__ + '_' + phase,
                retryable=bool(reset or response_timeout or premature), details=details) from None
        finally:
            if timer is not None:
                timer.cancel()
            if holder[0] is not None:
                holder[0].close()
            session.close()

    def _post(self, path, payload):
        if self.host != 'rqdata.ricequant.com' or path not in ('/auth', '/api'):
            raise HTTPSSecurityError('HTTPS_HOST_OR_PATH_OUTSIDE_AUTHORIZATION')
        started = time.monotonic()
        started_at = datetime.now().astimezone().isoformat()
        self._response_metadata = {}
        self._wire_body = b''
        if self._transport:
            return self._transport(path, payload, self._token)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_default_certs(ssl.Purpose.SERVER_AUTH)
        if context.keylog_filename is not None:
            raise HTTPSSecurityError('TLS_KEY_LOGGING_FORBIDDEN')
        conn = http.client.HTTPSConnection(self.host, 443,
            timeout=min(5.0, self._remaining()), context=context)
        conn.set_debuglevel(0)
        active_socket = [None]
        watchdog_fired = [False]
        body_events = []
        body_operation = None
        def abort():
            watchdog_fired[0] = True
            body_events.append({'event':'watchdog_abort'})
            if active_socket[0] is not None:
                try:
                    active_socket[0].shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            conn.close()
        timer = threading.Timer(self._remaining(), abort)
        timer.daemon = True
        timer.start()
        phase = 'CONNECT_TLS'
        try:
            conn.connect()
            sock = conn.sock
            active_socket[0] = sock
            sock.settimeout(min(20.0, self._remaining()))
            headers = {'Content-Type': 'application/json', 'Connection': 'close',
                       'Accept-Encoding': ('gzip' if path == '/api' and
                           payload.get('method') == 'all_instruments' else 'identity')}
            if path == '/api':
                headers['token'] = self._token
            phase = 'SEND_REQUEST'
            conn.request('POST', path, body=json.dumps(_wire(payload)).encode('utf-8'), headers=headers)
            phase = 'READ_HEADERS'
            response = conn.getresponse()
            expected = response.length
            declared = response.getheader('Content-Length')
            encoding = (response.getheader('Content-Encoding') or 'identity').strip().lower()
            transfer = (response.getheader('Transfer-Encoding') or '').strip().lower()
            self._response_metadata = dict(declared_bytes=expected, chunked=bool(response.chunked),
                content_length=int(declared) if declared and declared.isdigit() else None,
                content_encoding=encoding if encoding in ('identity', 'gzip') else 'UNSUPPORTED',
                transfer_encoding=transfer if transfer in ('', 'chunked', 'identity') else 'UNSUPPORTED',
                connection_will_close=bool(response.will_close), transfer_complete=False,
                http_status=response.status, http_version={10:'1.0',11:'1.1'}.get(
                    getattr(response,'version',None),'UNKNOWN'),
                connection_header=(response.getheader('Connection') or 'ABSENT')
                    if (response.getheader('Connection') or 'ABSENT').lower()
                    in ('close','keep-alive','absent') else 'UNRECOGNIZED')
            content = bytearray()
            phase = 'READ_BODY'
            size_limit = 128 * 1024 * 1024
            if expected is not None and expected > size_limit:
                raise HTTPSSecurityError('HTTPS_RESPONSE_SIZE_BOUND_EXCEEDED')
            while expected is None or len(content) < expected:
                body_operation = 'sock.settimeout'
                sock.settimeout(min(20.0, self._remaining()))
                size = min(65536, size_limit - len(content) + 1)
                if expected is not None:
                    size = min(size, expected - len(content))
                body_operation = 'response.read'
                try:
                    part = response.read(size)
                except http.client.IncompleteRead as exc:
                    content.extend(exc.partial)
                    raise
                body_events.append(dict(event='read_return',bytes=len(part),
                    response_closed=response.isclosed(),chunk_left=response.chunk_left,
                    socket_fileno=getattr(sock,'fileno',lambda:None)()))
                body_events[:] = body_events[-16:]
                if not part:
                    break
                content.extend(part)
                if len(content) > size_limit:
                    raise HTTPSSecurityError('HTTPS_RESPONSE_SIZE_BOUND_EXCEEDED')
                # The final nonempty read may also consume the terminating chunk.
                if response.chunked and response.isclosed() and response.chunk_left is None:
                    body_events.append({'event':'chunked_terminal_consumed'})
                    break
            self._remaining()
            incomplete = expected is not None and len(content) != expected
            self._wire_body = bytes(content)
            self._response_metadata.update(bytes_received=len(content), coded_body_bytes=len(content),
                chunked_terminal_consumed=bool(response.chunked and response.isclosed()
                    and response.chunk_left is None),
                incomplete=incomplete, transfer_complete=not incomplete,
                end_mode='SHORT_CONTENT_LENGTH' if incomplete else
                    'CHUNK_TERMINATOR' if response.chunked else
                    'CONTENT_LENGTH_SATISFIED' if expected is not None else 'CONNECTION_EOF')
            if incomplete:
                return response.status, self._wire_body
            if encoding not in ('identity', 'gzip') or transfer not in ('', 'chunked', 'identity'):
                raise HTTPSContractError('HTTPS_RESPONSE_ENCODING_UNSUPPORTED')
            decoded = self._wire_body
            if encoding == 'gzip':
                phase = 'DECODE_GZIP'
                decompress = zlib.decompressobj(16 + zlib.MAX_WBITS)
                decoded = decompress.decompress(decoded, 128 * 1024 * 1024 + 1)
                self._response_metadata.update(gzip_eof=decompress.eof,
                    gzip_unused_data_bytes=len(decompress.unused_data),
                    gzip_unconsumed_tail_bytes=len(decompress.unconsumed_tail))
                if len(decoded) > 128 * 1024 * 1024 or decompress.unconsumed_tail:
                    raise HTTPSSecurityError('HTTPS_DECODED_RESPONSE_SIZE_BOUND_EXCEEDED')
                if not decompress.eof or decompress.unused_data:
                    raise HTTPSContractError('HTTPS_GZIP_BODY_INCOMPLETE_OR_EXTRA_DATA')
                self._response_metadata['gzip_crc_verified'] = True
            self._remaining()
            self._response_metadata['decoded_body_bytes'] = len(decoded)
            return response.status, decoded
        except Exception as exc:
            chain = []
            cursor = exc
            seen = set()
            while cursor is not None and id(cursor) not in seen and len(chain) < 8:
                seen.add(id(cursor))
                item = {'type': type(cursor).__module__ + '.' + type(cursor).__name__}
                frames = []
                trace = cursor.__traceback__
                while trace is not None and len(frames) < 16:
                    frames.append(dict(file=trace.tb_frame.f_code.co_filename,
                        line=trace.tb_lineno,function=trace.tb_frame.f_code.co_name))
                    trace = trace.tb_next
                item['traceback'] = frames
                for key in ('errno','winerror','verify_code'):
                    value = getattr(cursor,key,None)
                    if isinstance(value,int): item[key] = value
                chain.append(item)
                cursor = cursor.__cause__ if cursor.__cause__ is not None else cursor.__context__
            self._response_metadata.update(failure_stage=phase, exception_chain=chain)
            if not isinstance(exc,(ssl.SSLError,OSError,http.client.HTTPException,TimeoutError)):
                raise
            details = dict(error_type=type(exc).__name__, failure_stage=phase, exception_chain=chain)
            for key in ('errno', 'winerror', 'verify_code'):
                value = getattr(exc, key, None)
                if isinstance(value, int):
                    details[key] = value
            self._response_metadata.update(details, incomplete=True, transfer_complete=False,
                                           end_mode='CONNECTION_ERROR')
            if 'content' in locals():
                self._wire_body = bytes(content)
                self._response_metadata.update(bytes_received=len(content), coded_body_bytes=len(content))
            if isinstance(exc, ssl.SSLError):
                self._stopped = True
                raise HTTPSSecurityError('HTTPS_TLS_VERIFICATION_OR_PROTOCOL_FAILURE') from None
            reset = isinstance(exc, ConnectionResetError) or details.get('errno') in (errno.ECONNRESET, 10054) or details.get('winerror') == 10054
            response_timeout = isinstance(exc, (TimeoutError, socket.timeout)) and phase in ('READ_HEADERS', 'READ_BODY')
            premature = isinstance(exc, (http.client.RemoteDisconnected, http.client.IncompleteRead))
            raise HTTPSTransportError('HTTPS_TRANSPORT_' + details['error_type'] + '_' + phase,
                retryable=bool(reset or response_timeout or premature), details=details) from None
        finally:
            self._response_metadata.update(started_at=started_at,
                finished_at=datetime.now().astimezone().isoformat(),
                elapsed_seconds=round(time.monotonic()-started,6),
                watchdog_fired=watchdog_fired[0],body_operation=body_operation,
                body_events=body_events[-16:])
            timer.cancel()
            conn.close()

    def _authenticate(self):
        if self._token is not None:
            return
        if self.state['auth_attempts']:
            raise HTTPSAuthError('SECOND_AUTH_ATTEMPT_FORBIDDEN')
        self._license = os.environ.get('RQDATA_LICENSE')
        if not self._license or self._license != self._license.strip():
            raise HTTPSAuthError('EXISTING_RQDATA_LICENSE_ENTRY_MISSING_OR_INVALID')
        self.state['auth_attempts'] = 1
        self._save()
        status, body = self._post('/auth', {'user_name': 'license', 'password': self._license})
        self.state['auth_http_status'] = status
        if self._response_metadata.get('incomplete'):
            self._stopped = True
            raise HTTPSSecurityError('HTTPS_AUTH_RESPONSE_TRUNCATED_NO_API')
        if status != 200:
            self._stopped = True
            self.state['auth_error'] = self._safe_error(body)
            self._save()
            raise HTTPSAuthError('HTTPS_AUTH_REJECTED_' + str(status))
        if body.lstrip().startswith(b'{'):
            self._stopped = True
            self.state['auth_error'] = self._safe_error(body)
            self._save()
            raise HTTPSAuthError('HTTPS_AUTH_BUSINESS_ERROR_NO_API')
        self._token = body.decode('utf-8')
        if not self._token or any(c in self._token for c in '\r\n'):
            self._stopped = True
            raise HTTPSAuthError('HTTPS_TOKEN_HEADER_FORMAT_INVALID')
        self._save()

    def call(self, method, **params):
        self._last_api_body = None
        self._last_api_status = None
        identity_request = params.pop('_identity_request', False)
        if identity_request and method not in ('instruments', 'all_instruments'):
            raise HTTPSContractError('IDENTITY_REQUEST_METHOD_INVALID')
        if method not in METHODS or self._stopped:
            raise HTTPSSecurityError('HTTPS_METHOD_NOT_ALLOWED_OR_SESSION_STOPPED')
        if method in self._blocked_methods:
            raise HTTPSContractError(method + ':PREVIOUS_CONTRACT_FAILURE_NO_RETRY')
        self._remaining()
        self._authenticate()
        if not identity_request and self.state['data_api_attempts'] >= self.api_limit:
            raise HTTPSSecurityError('HTTPS_API_BUDGET_EXHAUSTED')
        self.state['api_attempts'] += 1
        self.state['identity_api_attempts' if identity_request else 'data_api_attempts'] += 1
        number = self.state['api_attempts']
        entry = {'number': number, 'method': method, 'request': _wire(params),
                 'observed_at': datetime.now().astimezone().isoformat()}
        self.state['calls'].append(entry)
        self._save()
        try:
            status, body = self._post('/api', {'method': method, **params})
            self._last_api_body, self._last_api_status = body, status
            entry['http_status'] = status
            entry['response_framing'] = dict(self._response_metadata)
            if status != 200:
                entry['safe_error'] = self._safe_error(body)
                if (status in (401, 403) or 300 <= status < 400
                        or re.search(r'(?i)permission|unauthorized|forbidden|invalid.token|license.expir', str(entry['safe_error']))):
                    self._stopped = True
                    raise HTTPSAuthError('HTTPS_API_AUTH_OR_REDIRECT_REJECTED_' + str(status))
                raise HTTPSContractError(method + ':HTTP_' + str(status))
            # Some APIs report business errors in a HTTP200 JSON envelope.
            if body.lstrip().startswith(b'{'):
                entry['safe_error'] = self._safe_error(body)
                if re.search(r'(?i)auth|permission|license|token|denied|unauthorized|forbidden', str(entry['safe_error'])):
                    self._stopped = True
                    raise HTTPSAuthError(method + ':BUSINESS_AUTH_REJECTED')
                raise HTTPSContractError(method + ':BUSINESS_ERROR_RESPONSE')
            if any(secret and secret.encode('utf-8') in body for secret in (self._license, self._token)):
                self._stopped = True
                raise HTTPSSecurityError('HTTPS_API_RESPONSE_SECRET_CONTENT_FORBIDDEN')
            if self.output:
                name = f'{number:02d}_{method}.raw.txt'
                (self.output / name).write_bytes(body)
                entry['raw_response'] = name
            if self._response_metadata.get('incomplete'):
                if identity_request:
                    raise HTTPSTransportError('HTTPS_RESPONSE_TRUNCATED', retryable=True,
                        details=dict(error_type='ShortBody',failure_stage='READ_BODY'))
                raise HTTPSContractError(method + ':HTTP_RESPONSE_TRUNCATED')
            result = decode(method, body, params, response_framing=self._response_metadata)
            entry['status'] = 'LEGITIMATE_EMPTY' if result is None or getattr(result, 'empty', False) else 'CONTRACT_PASS'
            return result
        except HTTPSTransportError as exc:
            entry.update(status='TRANSPORT_FAILED',error=str(exc),retryable=exc.retryable,
                         response_framing=dict(self._response_metadata))
            raise
        except (HTTPSContractError, HTTPSAuthError, HTTPSSecurityError) as exc:
            entry['response_framing'] = dict(self._response_metadata)
            if isinstance(exc, HTTPSContractError):
                self._blocked_methods.add(method)
            else:
                self._stopped = True
            entry['status'] = 'BLOCKED'
            entry['error'] = str(exc)
            raise
        except Exception as exc:
            self._blocked_methods.add(method)
            entry['status'] = 'BLOCKED'
            entry['error'] = 'HTTPS_RESPONSE_CONTRACT_' + type(exc).__name__
            raise HTTPSContractError(method + ':RESPONSE_CONTRACT_' + type(exc).__name__) from None
        finally:
            self._save()

    def __getattr__(self, name):
        if name not in METHODS:
            raise AttributeError(name)
        def invoke(*args, **kwargs):
            if len(args) > 1:
                raise HTTPSContractError('HTTPS_ONLY_ONE_POSITIONAL_ID_ARGUMENT')
            if args:
                kwargs['order_book_id' if name == 'index_components' else 'order_book_ids'] = args[0]
            return self.call(name, **kwargs)
        return invoke


_PROVIDER = None


def get_provider():
    global _PROVIDER
    if _PROVIDER is None:
        _PROVIDER = RQDataHTTPSProvider()
    return _PROVIDER
