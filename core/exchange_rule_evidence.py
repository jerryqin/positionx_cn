"""Offline execution constraints derived from pinned official exchange rules.

This source is distinct from an RQData tick observation.  Identity still comes
from the admitted identity snapshot; names and ST labels are not classifiers.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
from typing import Mapping


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'config/ops/exchange_execution_rules_2026.json'
SOURCE = 'EXCHANGE_RULE_DERIVED'
CONTRACT = 'EXCHANGE_EXECUTION_RULE_V1'


class ExchangeRuleEvidenceError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise ExchangeRuleEvidenceError(code)


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)
    try:
        return datetime.strptime(text, '%Y%m%d').date() if re.fullmatch(r'\d{8}', text) else date.fromisoformat(text)
    except (TypeError, ValueError):
        _fail('RULE_BUSINESS_DATE_INVALID')


def _instant(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError()
        return result
    except (TypeError, ValueError):
        _fail('RULE_EVIDENCE_TIME_INVALID')


@lru_cache(maxsize=16)
def _read_config(path, mtime, size):
    try:
        config = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        _fail('RULE_CONFIG_INVALID')
    if config.get('contract') != CONTRACT or config.get('source') != SOURCE:
        _fail('RULE_CONFIG_CONTRACT_INVALID')
    return config


@lru_cache(maxsize=64)
def _hash_file(path, mtime, size):
    if size > 8 * 1024 * 1024:
        _fail('RULE_SOURCE_SIZE_INVALID')
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        _fail('RULE_SOURCE_MISSING')


def _load(config_path, evidence_root):
    path = Path(config_path or CONFIG).resolve()
    root = Path(evidence_root or ROOT).resolve()
    try:
        stat = path.stat()
    except OSError:
        _fail('RULE_CONFIG_MISSING')
    return _read_config(str(path), stat.st_mtime_ns, stat.st_size), root


def _sources(config, source_ids, root, decision):
    out = []
    for key in dict.fromkeys(source_ids):
        try:
            item = config['sources'][key]
            path = (root / item['path']).resolve()
            if not path.is_relative_to(root):
                _fail('RULE_SOURCE_PATH_INVALID')
            stat = path.stat()
        except (KeyError, OSError):
            _fail('RULE_SOURCE_MISSING')
        if _hash_file(str(path), stat.st_mtime_ns, stat.st_size) != item['sha256']:
            _fail('RULE_SOURCE_HASH_MISMATCH')
        if _instant(item['acquired_at']) > decision:
            _fail('RULE_EVIDENCE_NOT_AVAILABLE')
        if not re.fullmatch(r'https://(?:www|edu)\.sse\.com\.cn/.*|https://(?:www\.szse\.cn|docs\.static\.szse\.cn)/.*', item['url']):
            _fail('RULE_SOURCE_URL_INVALID')
        out.append({'source_id': key, **item})
    return out


def _identity(row):
    if not isinstance(row, Mapping):
        _fail('RULE_IDENTITY_MISSING')
    identity = {key: str(row.get(key, '')) for key in ('order_book_id', 'type', 'exchange', 'board_type')}
    match = re.fullmatch(r'(\d{6})\.(XSHG|XSHE)', identity['order_book_id'])
    if match is None or match[2] != identity['exchange']:
        _fail('RULE_IDENTITY_EXCHANGE_CONFLICT')
    if identity['type'] != 'CS':
        _fail('RULE_INSTRUMENT_CLASS_UNKNOWN')
    if row.get('currency') not in (None, '', 'CNY', 'RMB'):
        _fail('RULE_CURRENCY_CONFLICT')
    if row.get('currency'):
        identity['currency'] = str(row['currency'])
    return identity, int(match[1])


def resolve_rule(identity_row, business_date, *, config_path=None, evidence_root=None, decision=None):
    """Return a bound, hash-verified rule proof, or block this instrument.

    No network, current-price inference, identity repair, or RQ tick fallback is
    performed.  ``decision`` enforces when this acquired rule evidence became
    available; omitting it means the current actual evaluation time.
    """
    day = _day(business_date)
    now = _instant(decision) if decision is not None else datetime.now(timezone.utc)
    config, root = _load(config_path, evidence_root)
    identity, code = _identity(identity_row)
    matches = [item for item in config['classifications']
               if item['exchange'] == identity['exchange'] and item['board_type'] == identity['board_type']
               and any(int(start) <= code <= int(end) for start, end in item['code_ranges'])]
    if len(matches) != 1:
        _fail('RULE_CLASSIFICATION_UNKNOWN' if not matches else 'RULE_CLASSIFICATION_CONFLICT')
    classification = matches[0]
    if day < _day(classification['effective_from']):
        _fail('RULE_CLASSIFICATION_DATE_OUT_OF_RANGE')
    rules = [item for item in config['rules'] if item['exchange'] == identity['exchange']
             and item['board_type'] == identity['board_type']
             and _day(item['effective_from']) <= day
             and (not item.get('effective_to') or day <= _day(item['effective_to']))]
    if len(rules) != 1:
        _fail('RULE_DATE_OUT_OF_RANGE' if not rules else 'RULE_VERSION_CONFLICT')
    rule = rules[0]
    if rule.get('conflict') or classification.get('conflict'):
        _fail('RULE_SPECIAL_PROVISION_CONFLICT')
    for key in ('price_tick', 'tick_size'):
        value = identity_row.get(key)
        if value is not None and str(value) not in ('', 'nan', 'NaN', '<NA>'):
            try:
                supplied = Decimal(str(value))
                if not supplied.is_finite() or supplied != Decimal(rule['price_tick']):
                    _fail('RULE_OBSERVED_TICK_CONFLICT')
            except InvalidOperation:
                _fail('RULE_OBSERVED_TICK_CONFLICT')
    sources = _sources(config, rule['source_ids'] + classification['source_ids'], root, now)
    main = next(item for item in sources if item['source_id'] == rule['primary_source_id'])
    return {
        'contract': CONTRACT, 'source': SOURCE, 'order_book_id': identity['order_book_id'],
        'identity': identity, 'currency': 'CNY', 'instrument_class': classification['instrument_class'],
        'classification_rule_id': classification['classification_rule_id'],
        'rule_id': rule['rule_id'], 'version': rule['version'],
        'effective_from': rule['effective_from'], 'effective_to': rule.get('effective_to'),
        'price_tick': float(rule['price_tick']),
        'minimum_order_quantity': rule['minimum_order_quantity'], 'quantity_step': rule['quantity_step'],
        'clauses': rule['clauses'] + classification.get('additional_clauses', []),
        'source_sha256': main['sha256'], 'source_url': main['url'],
        'acquired_at': max(item['acquired_at'] for item in sources), 'sources': sources,
    }


def verify_rule_evidence(evidence, business_date, *, identity_row=None, config_path=None,
                         evidence_root=None, decision=None):
    """Recompute and compare the complete proof before using its tick value."""
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except ValueError:
            _fail('RULE_PROOF_INVALID')
    if not isinstance(evidence, Mapping) or evidence.get('source') != SOURCE:
        _fail('RULE_PROOF_SOURCE_INVALID')
    row = identity_row if identity_row is not None else evidence.get('identity')
    canonical = resolve_rule(row, business_date, config_path=config_path,
                             evidence_root=evidence_root, decision=decision)
    if dict(evidence) != canonical:
        _fail('RULE_PROOF_CONFLICT')
    return canonical
