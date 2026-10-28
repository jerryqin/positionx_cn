"""Direct offline checks: isolated fixtures, no provider, credentials or tasks."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from core.exchange_rule_evidence import (
    CONFIG, ExchangeRuleEvidenceError, resolve_rule, verify_rule_evidence,
)
from research.forward_deep_oversold import entry_fillability
from research.forward_ret120 import standard_open_v1


def identity(code='600000.XSHG', board='MainBoard'):
    return {'order_book_id': code, 'type': 'CS', 'exchange': code.split('.')[1],
            'board_type': board, 'symbol': 'Name is not a classifier', 'special_type': 'ST'}


class ExchangeRuleEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='positionx_rule_test_')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'rules.json'
        self.config = json.loads(CONFIG.read_text(encoding='utf-8'))
        for key, source in self.config['sources'].items():
            body = ('isolated official-source fixture ' + key).encode()
            file = self.root / (key + '.bin')
            file.write_bytes(body)
            source.update(path=file.name, sha256=hashlib.sha256(body).hexdigest(),
                          acquired_at='2026-10-28T08:00:00+08:00')
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.config), encoding='utf-8')

    def resolve(self, row=None, day='20261028', **kw):
        return resolve_rule(row or identity(), day, config_path=self.path,
                            evidence_root=self.root, decision=kw.pop('decision', '2026-10-28T20:00:00+08:00'), **kw)

    def verify(self, proof, day='20261028', **kw):
        return verify_rule_evidence(proof, day, config_path=self.path,
                                    evidence_root=self.root, decision='2026-10-28T20:00:00+08:00', **kw)

    def blocked(self, code, call):
        with self.assertRaises(ExchangeRuleEvidenceError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def test_explicit_native_identity_and_official_allocations(self):
        for row, kind, minimum, step in [
            (identity(), 'ORDINARY_A_SHARE', 100, 100),
            (identity('688001.XSHG', 'KSH'), 'ORDINARY_A_SHARE', 200, 1),
            (identity('302132.XSHE', 'GEM'), 'ORDINARY_A_SHARE', 100, 100),
            (identity('689009.XSHG', 'KSH'), 'CDR_A_RULES', 200, 1),
            (identity('001001.XSHE'), 'CDR_A_RULES', 100, 100),
            (identity('309800.XSHE', 'GEM'), 'CDR_A_RULES', 100, 100),
        ]:
            with self.subTest(row=row['order_book_id']):
                proof = self.resolve(row)
                self.assertEqual((proof['instrument_class'], proof['currency'], proof['price_tick']),
                                 (kind, 'CNY', 0.01))
                self.assertEqual((proof['minimum_order_quantity'], proof['quantity_step']), (minimum, step))
                self.assertEqual(self.verify(proof, identity_row=row), proof)
                if kind == 'CDR_A_RULES':
                    self.assertTrue(any('CDR' in clause for clause in proof['clauses']))

    def test_unknown_and_conflicting_identity_is_not_filled(self):
        for changes, code in [
            ({'type': 'ETF'}, 'RULE_INSTRUMENT_CLASS_UNKNOWN'),
            ({'exchange': 'XSHE'}, 'RULE_IDENTITY_EXCHANGE_CONFLICT'),
            ({'board_type': ''}, 'RULE_CLASSIFICATION_UNKNOWN'),
            ({'currency': 'USD'}, 'RULE_CURRENCY_CONFLICT'),
            ({'order_book_id': '900901.XSHG'}, 'RULE_CLASSIFICATION_UNKNOWN'),
            ({'order_book_id': '920001.BJSE', 'exchange': 'BJSE'}, 'RULE_IDENTITY_EXCHANGE_CONFLICT'),
        ]:
            with self.subTest(changes=changes):
                row = {**identity(), **changes}
                self.blocked(code, lambda: self.resolve(row))

    def test_dates_availability_reuse_and_explicit_expiry(self):
        self.blocked('RULE_CLASSIFICATION_DATE_OUT_OF_RANGE', lambda: self.resolve(day='20260101'))
        self.blocked('RULE_DATE_OUT_OF_RANGE', lambda: self.resolve(identity('000001.XSHE'), '20260705'))
        self.blocked('RULE_EVIDENCE_NOT_AVAILABLE',
                     lambda: self.resolve(decision='2026-10-28T07:59:59+08:00'))
        self.assertEqual(self.resolve(day='20261028'), self.resolve(day='20261029'))
        self.config['rules'][0]['effective_to'] = '2026-10-28'
        self.save()
        self.blocked('RULE_DATE_OUT_OF_RANGE', lambda: self.resolve(day='20261029'))

    def test_source_hash_proof_and_observed_tick_conflicts(self):
        proof = self.resolve()
        changed = copy.deepcopy(proof)
        changed['source'] = 'RQDATA'
        self.blocked('RULE_PROOF_SOURCE_INVALID', lambda: self.verify(changed))
        changed = copy.deepcopy(proof)
        changed['price_tick'] = 0.02
        self.blocked('RULE_PROOF_CONFLICT', lambda: self.verify(changed))
        self.blocked('RULE_OBSERVED_TICK_CONFLICT', lambda: self.resolve({**identity(), 'price_tick': 0.02}))
        file = self.root / self.config['sources']['sse_rules']['path']
        file.write_bytes(b'changed evidence')
        self.blocked('RULE_SOURCE_HASH_MISMATCH', lambda: self.verify(proof))

    def test_overlapping_version_or_special_provision_blocks(self):
        self.config['rules'].append(copy.deepcopy(self.config['rules'][0]))
        self.save()
        self.blocked('RULE_VERSION_CONFLICT', lambda: self.resolve())
        self.config['rules'].pop()
        self.config['rules'][0]['conflict'] = True
        self.save()
        self.blocked('RULE_SPECIAL_PROVISION_CONFLICT', lambda: self.resolve())

    def test_existing_two_strategy_half_tick_boundary_and_native_units(self):
        tick = self.resolve()['price_tick']
        for function in (entry_fillability, standard_open_v1):
            with self.subTest(strategy=function.__module__):
                self.assertTrue(function(2.994999, 3.0, 0, tick)['fillable'])
                self.assertEqual(function(2.995, 3.0, 0, tick)['reason'], 'OPEN_AT_UP_LIMIT')
                self.assertEqual(function(2.995001, 3.0, 0, tick)['reason'], 'OPEN_AT_UP_LIMIT')
                self.assertTrue(function(2.328, 2.34, 0, tick, prev_close=2.33)['fillable'])


if __name__ == '__main__':
    unittest.main()
