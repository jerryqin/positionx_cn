from pathlib import Path
import tempfile, unittest, sqlite3, sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.pilot_risk_recovery import PilotRiskRecoveryPolicy, RiskInputs, evaluate_new_entry, copy_verify, sqlite_readable

class T(unittest.TestCase):
    def test_healthy_allows(self):
        self.assertTrue(evaluate_new_entry(RiskInputs(proposed_target_gross=0.30))["allow_new_entry"])
    def test_stale_blocks(self):
        self.assertFalse(evaluate_new_entry(RiskInputs(fresh_market_data=False))["allow_new_entry"])
    def test_missing_pit_blocks(self):
        self.assertFalse(evaluate_new_entry(RiskInputs(pit_snapshot_complete=False))["allow_new_entry"])
    def test_missed_session_blocks(self):
        self.assertFalse(evaluate_new_entry(RiskInputs(missed_forward_session=True))["allow_new_entry"])
    def test_policy_hash_blocks(self):
        self.assertFalse(evaluate_new_entry(RiskInputs(policy_hash_match=False))["allow_new_entry"])
    def test_reconcile_blocks(self):
        self.assertFalse(evaluate_new_entry(RiskInputs(reconcile_clean=False))["allow_new_entry"])
    def test_duplicate_blocks(self):
        self.assertFalse(evaluate_new_entry(RiskInputs(duplicate_intent=True))["allow_new_entry"])
    def test_gross_above_30_blocks(self):
        self.assertFalse(evaluate_new_entry(RiskInputs(proposed_target_gross=0.300001))["allow_new_entry"])
    def test_missing_manual_approval_blocks(self):
        self.assertFalse(evaluate_new_entry(RiskInputs(manual_approval=False))["allow_new_entry"])
    def test_integrity_failure_does_not_force_liquidation(self):
        self.assertFalse(evaluate_new_entry(RiskInputs(fresh_market_data=False))["force_liquidation"])
    def test_copy_hash(self):
        with tempfile.TemporaryDirectory() as d:
            a=Path(d)/"a"; b=Path(d)/"b"; a.write_text("abc")
            self.assertTrue(copy_verify(a,b)["match"])
    def test_sqlite_readonly(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"x.db"; c=sqlite3.connect(p); c.execute("create table t(x int)"); c.commit(); c.close()
            self.assertTrue(sqlite_readable(p))
    def test_policy_manual_no_auto(self):
        p=PilotRiskRecoveryPolicy()
        self.assertTrue(p.require_manual_approval); self.assertFalse(p.auto_execute)
    def test_policy_30pct(self):
        self.assertAlmostEqual(PilotRiskRecoveryPolicy().portfolio_max_target_gross,0.30)

if __name__=="__main__": unittest.main()
