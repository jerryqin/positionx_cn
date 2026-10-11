import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_pead_phase0_acceptance_v01 as audit


class PEADPhase0AcceptanceV01Tests(unittest.TestCase):

    def good_metrics(self):
        return {
            "required_columns_present": True,
            "groups": 5000,
            "first_ann_nonnull_ratio": 0.999,
            "rows_ann_before_first": 0,
            "groups_inconsistent_first_ann": 0,
            "first_row_available_ratio": 0.998,
            "groups_conflicting_first_payload": 0,
            "first_rows_p_change_min_nonnull_ratio": 0.995,
            "first_rows_p_change_max_nonnull_ratio": 0.995,
            "first_rows_last_parent_net_nonnull_ratio": 0.993,
        }

    def test_accept_good_delivery(self):
        d = audit.decide(self.good_metrics())
        self.assertEqual(
            d["p1_earnings_forecast_drift"],
            "READY_FOR_FAST_REJECT",
        )
        self.assertEqual(
            d["p2_sue_pead"],
            "NOT_ACCEPTED_FROM_THIS_DELIVERY",
        )

    def test_reject_missing_initial_payload(self):
        m = self.good_metrics()
        m["first_row_available_ratio"] = 0.95
        d = audit.decide(m)
        self.assertEqual(
            d["p1_earnings_forecast_drift"],
            "DATA_NOT_ACCEPTED",
        )

    def test_reject_conflicting_first_payload(self):
        m = self.good_metrics()
        m["groups_conflicting_first_payload"] = 1
        d = audit.decide(m)
        self.assertEqual(
            d["p1_earnings_forecast_drift"],
            "DATA_NOT_ACCEPTED",
        )

    def test_reject_low_magnitude_coverage(self):
        m = self.good_metrics()
        m["first_rows_p_change_min_nonnull_ratio"] = 0.90
        d = audit.decide(m)
        self.assertEqual(
            d["p1_earnings_forecast_drift"],
            "DATA_NOT_ACCEPTED",
        )


if __name__ == "__main__":
    unittest.main()
