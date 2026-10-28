"""Only action-window repair checks: fake calls and self-cleaning local output.

No RQ initialization, external request, credential access, Scheduler, Broker,
production database, frozen artifact or historical strategy computation.
"""
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from core import p0_rq_input as rq
from scripts import collect_p0_rq_close_v01 as capture


class FakeActions:
    def __init__(self, *, split=None, rights=None):
        self.calls = []
        self.split = split
        self.rights = rights

    def get_dividend(self, ids, **kwargs):
        self.calls.append(('dividend', list(ids), kwargs))
        return None

    def get_split(self, ids, **kwargs):
        self.calls.append(('split', list(ids), kwargs))
        return self.split

    def get_allotment(self, ids, **kwargs):
        self.calls.append(('rights', list(ids), kwargs))
        return self.rights


class HTTPSActionWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)

    def run_capture(self, provider, target, previous):
        capture.capture_action_terms(provider, ['600050.XSHG'], previous, target, self.work)
        return json.loads((self.work / 'action_capture.json').read_text(encoding='utf-8'))

    def test_split_record_window_includes_friday_before_monday_ex_date(self):
        provider = FakeActions()
        receipt = self.run_capture(provider, '20261019', '20261018')
        call = next(x for x in provider.calls if x[0] == 'split')
        self.assertEqual(call[2], {'start_date': date(2026, 10, 16), 'end_date': date(2026, 10, 19)})
        self.assertEqual(receipt['queries'][1]['filter_semantics'], 'book_closure_date')
        self.assertEqual(receipt['caller_previous'], '2026-10-18')

    def test_split_record_window_includes_session_before_spring_festival(self):
        provider = FakeActions()
        self.run_capture(provider, '20260224', '20260223')
        call = next(x for x in provider.calls if x[0] == 'split')
        self.assertEqual(call[2]['start_date'], date(2026, 2, 13))
        self.assertEqual(call[2]['end_date'], date(2026, 2, 24))

    def test_missing_previous_approved_session_blocks_before_any_call(self):
        provider = FakeActions()
        with self.assertRaisesRegex(rq.InputBlocked, '^ACTION_PREVIOUS_APPROVED_SESSION_MISSING$'):
            self.run_capture(provider, '20260105', '20260104')
        self.assertEqual(provider.calls, [])
        self.assertFalse((self.work / 'action_capture.json').exists())

    def test_rights_keeps_old_announcement_and_filters_exact_target_ex_date(self):
        rights = pd.DataFrame([
            dict(order_book_id='600050.XSHG', declaration_announcement_date='2025-04-10',
                 ex_right_date='2026-10-28', proportion=.3, allotted_proportion=.3,
                 allotment_price=3., stock_type='a_share'),
            dict(order_book_id='600050.XSHG', declaration_announcement_date='2024-04-10',
                 ex_right_date='2025-07-07', proportion=.3, allotted_proportion=.3,
                 allotment_price=3., stock_type='a_share')])
        provider = FakeActions(rights=rights)
        receipt = self.run_capture(provider, '20261028', '20261027')
        self.assertEqual(provider.calls[-1], ('rights', ['600050.XSHG'], {'end_date': date(2026, 10, 28)}))
        self.assertTrue(receipt['queries'][-1]['start_omitted'])
        self.assertIsNone(receipt['queries'][-1]['start'])
        raw = pd.read_parquet(self.work / 'action_raw_rights.parquet')
        self.assertEqual(len(raw), 2)
        actions = pd.read_parquet(self.work / 'captured_actions.parquet')
        self.assertEqual(len(actions), 1)
        self.assertEqual(pd.Timestamp(actions.iloc[0].ex_date), pd.Timestamp('2026-10-28'))
        self.assertEqual(actions.iloc[0].rights_per_share, .3)
        self.assertEqual(actions.iloc[0].rights_price, 3.)
        self.assertEqual(actions.iloc[0].available_at, '2025-04-10T23:59:59.999999+08:00')

    def test_dividend_keeps_existing_90_day_announcement_window(self):
        provider = FakeActions()
        self.run_capture(provider, '20261028', '20261027')
        self.assertEqual(provider.calls[0][2], {'start_date': date(2026, 7, 30), 'end_date': date(2026, 10, 28)})

    def test_rights_original_ratio_evidence_gate_still_blocks(self):
        provider = FakeActions(rights=pd.DataFrame([
            dict(order_book_id='600050.XSHG', declaration_announcement_date='2025-04-10',
                 ex_right_date='2026-10-28', proportion=.3, allotted_proportion=.29,
                 allotment_price=3., stock_type='a_share')]))
        receipt = self.run_capture(provider, '20261028', '20261027')
        self.assertEqual(receipt['status'], 'BLOCKED')
        self.assertEqual(receipt['reason'], 'ACTION_RIGHTS_RATIO_REQUIRES_NATIVE_EVIDENCE')
        self.assertFalse((self.work / 'captured_actions.parquet').exists())

    def test_split_original_publication_evidence_gate_still_blocks(self):
        provider = FakeActions(split=pd.DataFrame([
            dict(order_book_id='600050.XSHG', book_closure_date='2026-10-16',
                 ex_dividend_date='2026-10-19', split_coefficient_from=10., split_coefficient_to=14.)]))
        receipt = self.run_capture(provider, '20261019', '20261018')
        self.assertEqual(receipt['status'], 'BLOCKED')
        self.assertEqual(receipt['reason'], 'ACTION_PUBLICATION_EVIDENCE_MISSING')
        self.assertFalse((self.work / 'captured_actions.parquet').exists())

    def test_split_original_positive_share_unit_gate_still_blocks(self):
        provider = FakeActions(split=pd.DataFrame([
            dict(order_book_id='600050.XSHG', book_closure_date='2026-10-16',
                 ex_dividend_date='2026-10-19', split_coefficient_from=0., split_coefficient_to=14.)]))
        receipt = self.run_capture(provider, '20261019', '20261018')
        self.assertEqual(receipt['status'], 'BLOCKED')
        self.assertEqual(receipt['reason'], 'ACTION_SPLIT_UNIT_INVALID')
        self.assertFalse((self.work / 'captured_actions.parquet').exists())


if __name__ == '__main__':
    unittest.main()
