"""Direct optional Instrument-field checks with local/fake frames only.

Saved HTTP CSV is read without network or SDK initialization. No credential,
Scheduler, production data, old research output, or Git operation is used.
"""
from pathlib import Path
import hashlib
import json
import unittest

import pandas as pd

from core import p0_rq_input as rq
from core import rqdata_identity as identity
from core.rqdata_https import decode


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ['order_book_id', 'symbol', 'listed_date', 'de_listed_date',
            'special_type', 'round_lot', 'status', 'type']
DAY = '20261028'
SAVED = ROOT / 'reports/rqdata_independent_http_20261028/TEMP/comparison/01_batch4.requests.raw.csv'


def frame(symbol='600050.XSHG'):
    return pd.DataFrame([dict(order_book_id=symbol, symbol='原证券名称',
        listed_date='2002-10-09', de_listed_date='0000-00-00',
        special_type='Other', round_lot=100, status='Active', type='CS')])


class IdentityOptionalFieldsTests(unittest.TestCase):
    @unittest.skipUnless(SAVED.is_file(), 'Saved actual batch response is local acceptance evidence')
    def test_actual_complete_500_response_passes_without_optional_purchasedate(self):
        receipt = json.loads((SAVED.parent / 'RESULT.json').read_text(encoding='utf-8'))
        call = receipt['calls'][0]
        body = SAVED.read_bytes()
        self.assertEqual(len(body), 126111)
        self.assertEqual(hashlib.sha256(body).hexdigest(),
            '94c704ab9cd7d5d6ae528bf10639a33bb846077cb64bbf52368ac557d5bfcec5')
        self.assertEqual(call['content_length'], len(body))
        self.assertTrue(call['transfer_complete'])
        source = decode('instruments', body, {})
        self.assertEqual(len(source), 500)
        self.assertEqual(len(source.columns), 21)
        self.assertNotIn('purchasedate', source)
        self.assertIn('purchasedate', receipt['fields'])
        validated = identity._validate(source, call['payload']['order_book_ids'],
            receipt['business_date'], receipt['fields'])
        pd.testing.assert_frame_equal(validated, source)
        self.assertNotIn('purchasedate', validated)

    def test_absent_optional_field_is_not_invented_or_required(self):
        source = frame()
        validated = identity._validate(source, ['600050.XSHG'], DAY, REQUIRED + ['purchasedate'])
        pd.testing.assert_frame_equal(validated, source)
        self.assertNotIn('purchasedate', validated)
        self.assertEqual(validated.iloc[0].special_type, 'Other')
        self.assertEqual(validated.iloc[0].de_listed_date, '0000-00-00')

    def test_supplied_new_optional_field_and_null_are_preserved(self):
        source = frame()
        source['vendor_new_optional'] = ['UNRECOGNIZED_VENDOR_VALUE']
        source['purchasedate'] = [None]
        validated = identity._validate(source, ['600050.XSHG'], DAY, REQUIRED)
        pd.testing.assert_frame_equal(validated, source)

    def test_batch_union_produces_null_without_normal_or_old_value_fill(self):
        first = frame()
        first['purchasedate'] = ['2002-09-17']
        second = frame('600051.XSHG')
        second['vendor_new_optional'] = ['UNKNOWN_NATIVE_TEXT']
        one = identity._validate(first, ['600050.XSHG'], DAY, REQUIRED + ['purchasedate'])
        two = identity._validate(second, ['600051.XSHG'], DAY, REQUIRED + ['purchasedate'])
        combined = identity._validate(pd.concat([one, two], ignore_index=True, sort=False),
            ['600050.XSHG', '600051.XSHG'], DAY, REQUIRED + ['purchasedate'])
        self.assertEqual(combined.loc[0, 'purchasedate'], '2002-09-17')
        self.assertTrue(pd.isna(combined.loc[1, 'purchasedate']))
        self.assertTrue(pd.isna(combined.loc[0, 'vendor_new_optional']))
        self.assertEqual(combined.loc[1, 'vendor_new_optional'], 'UNKNOWN_NATIVE_TEXT')
        self.assertEqual(combined.special_type.tolist(), ['Other', 'Other'])

    def test_missing_required_fields_still_block(self):
        for name in REQUIRED:
            with self.subTest(field=name):
                with self.assertRaisesRegex(rq.InputBlocked, 'IDENTITY_EXACT_SET_OR_FIELD_CONFLICT'):
                    identity._validate(frame().drop(columns=name), ['600050.XSHG'], DAY, REQUIRED)

    def test_null_required_fields_still_block(self):
        for name in REQUIRED:
            with self.subTest(field=name):
                broken = frame()
                broken.loc[0, name] = None
                with self.assertRaises(rq.InputBlocked):
                    identity._validate(broken, ['600050.XSHG'], DAY, REQUIRED)

    def test_wrong_set_and_duplicate_identity_still_block(self):
        with self.assertRaisesRegex(rq.InputBlocked, 'IDENTITY_EXACT_SET_OR_FIELD_CONFLICT'):
            identity._validate(frame(), ['600051.XSHG'], DAY, REQUIRED)
        with self.assertRaisesRegex(rq.InputBlocked, 'IDENTITY_EXACT_SET_OR_FIELD_CONFLICT'):
            identity._validate(pd.concat([frame(), frame()], ignore_index=True), ['600050.XSHG'], DAY, REQUIRED)

    def test_class_date_and_lot_conflicts_still_block(self):
        for field, value in [('type', 'ETF'), ('listed_date', '2026-10-29'),
                             ('listed_date', '0000-00-00'), ('round_lot', 0), ('round_lot', .5)]:
            with self.subTest(field=field, value=value):
                broken = frame().astype(object)
                broken.loc[0, field] = value
                with self.assertRaises(rq.InputBlocked):
                    identity._validate(broken, ['600050.XSHG'], DAY, REQUIRED)


if __name__ == '__main__':
    unittest.main()
