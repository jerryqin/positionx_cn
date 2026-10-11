import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_ret120_shared_equity_v01 as audit


class Ret120SharedEquityV01Tests(unittest.TestCase):

    def test_assign_quintiles_balanced(self):
        q = audit.assign_quintiles(pd.Series(np.arange(100, dtype=float)))
        counts = q.value_counts().to_dict()
        self.assertEqual(counts["Q1"], 20)
        self.assertEqual(counts["Q5"], 20)

    def test_curve_metrics_flat(self):
        curve = pd.DataFrame({
            "equity": [1.0, 1.0, 1.0],
            "gross_exposure": [0.0, 0.0, 0.0],
            "unique_positions": [0, 0, 0],
            "max_symbol_weight": [0.0, 0.0, 0.0],
        })
        m = audit.curve_metrics(curve)
        self.assertAlmostEqual(m["max_drawdown"], 0.0)

    def test_curve_metrics_drawdown(self):
        curve = pd.DataFrame({
            "equity": [1.0, 1.2, 0.9, 1.1],
            "gross_exposure": [1.0, 1.0, 1.0, 1.0],
            "unique_positions": [10, 10, 10, 10],
            "max_symbol_weight": [0.1, 0.1, 0.1, 0.1],
        })
        m = audit.curve_metrics(curve)
        self.assertAlmostEqual(m["max_drawdown"], -0.25)

    def test_turnover_one_way_is_half_gross(self):
        curve = pd.DataFrame({"equity": [1.0] * 253})
        trades = pd.DataFrame([
            {"side": "BUY", "notional": 1.0},
            {"side": "SELL", "notional": 1.0},
        ])
        t = audit.turnover_metrics(curve, trades)
        self.assertAlmostEqual(
            t["annualized_one_way_equivalent"],
            t["annualized_gross_turnover"] / 2.0,
        )

    def test_normalize_date(self):
        self.assertEqual(audit.normalize_date("2026-09-24"), "20260924")

    def test_cost_scenarios_are_round_trip(self):
        self.assertAlmostEqual(audit.COST_SCENARIOS["20bp"], 0.0020)
        self.assertAlmostEqual(audit.COST_SCENARIOS["50bp"], 0.0050)

    def test_position_level_deferred_exit_sells_executable_peer(self):
        q5 = pd.DataFrame([
            {"signal_date": "20200101", "symbol": "AAA"},
            {"signal_date": "20200101", "symbol": "BBB"},
        ])
        cohorts = pd.DataFrame([{
            "signal_date": "20200101",
            "entry_date": "20200102",
            "exit_date": "20200103",
            "slot_id": 0,
        }])
        calendar = ["20200102", "20200103", "20200104"]
        opens = {
            ("20200102", "AAA"): 10.0,
            ("20200102", "BBB"): 10.0,
        }
        exact = {
            ("20200102", "AAA"): 10.0,
            ("20200102", "BBB"): 10.0,
            ("20200103", "AAA"): 11.0,
            # BBB blocked on nominal exit
            ("20200104", "BBB"): 12.0,
        }
        marks = {
            "20200102": {"AAA": 10.0, "BBB": 10.0},
            "20200103": {"AAA": 11.0, "BBB": 10.0},
            "20200104": {"AAA": 11.0, "BBB": 12.0},
        }

        # Extend simulation horizon with a harmless later cohort in another slot.
        q5 = pd.concat([
            q5,
            pd.DataFrame([{"signal_date": "20200102", "symbol": "CCC"}]),
        ], ignore_index=True)
        cohorts = pd.concat([
            cohorts,
            pd.DataFrame([{
                "signal_date": "20200102",
                "entry_date": "20200103",
                "exit_date": "20200104",
                "slot_id": 1,
            }]),
        ], ignore_index=True)
        opens[("20200103", "CCC")] = 20.0
        exact[("20200103", "CCC")] = 20.0
        exact[("20200104", "CCC")] = 20.0
        marks["20200103"]["CCC"] = 20.0
        marks["20200104"]["CCC"] = 20.0

        _, trades, receipt = audit.simulate(
            q5=q5,
            cohorts=cohorts,
            calendar=calendar,
            opens=opens,
            exact_closes=exact,
            mark_closes=marks,
            round_trip_cost=0.0,
        )

        nominal_sell = trades[
            (trades["side"] == "SELL")
            & (trades["signal_date"] == "20200101")
        ]
        deferred_sell = trades[
            (trades["side"] == "SELL_DEFERRED_POSITION")
            & (trades["signal_date"] == "20200101")
        ]

        self.assertEqual(int(nominal_sell["n_symbols"].sum()), 1)
        self.assertEqual(int(deferred_sell["n_symbols"].sum()), 1)
        self.assertEqual(receipt["positions_deferred_at_nominal_exit"], 1)
        self.assertEqual(receipt["max_exit_delay_sessions"], 1)

    def test_released_cash_can_be_reused_while_blocked_residual_remains(self):
        q5 = pd.DataFrame([
            {"signal_date": "20200101", "symbol": "AAA"},
            {"signal_date": "20200101", "symbol": "BBB"},
            {"signal_date": "20200102", "symbol": "CCC"},
        ])
        cohorts = pd.DataFrame([
            {
                "signal_date": "20200101",
                "entry_date": "20200102",
                "exit_date": "20200103",
                "slot_id": 0,
            },
            {
                "signal_date": "20200102",
                "entry_date": "20200104",
                "exit_date": "20200105",
                "slot_id": 0,
            },
        ])
        calendar = ["20200102", "20200103", "20200104", "20200105"]
        opens = {
            ("20200102", "AAA"): 10.0,
            ("20200102", "BBB"): 10.0,
            ("20200104", "CCC"): 20.0,
        }
        exact = {
            ("20200102", "AAA"): 10.0,
            ("20200102", "BBB"): 10.0,
            ("20200103", "AAA"): 10.0,
            # BBB blocked 20200103, resumes 20200104
            ("20200104", "BBB"): 10.0,
            ("20200104", "CCC"): 20.0,
            ("20200105", "CCC"): 20.0,
        }
        marks = {
            "20200102": {"AAA": 10.0, "BBB": 10.0},
            "20200103": {"AAA": 10.0, "BBB": 10.0},
            "20200104": {"AAA": 10.0, "BBB": 10.0, "CCC": 20.0},
            "20200105": {"AAA": 10.0, "BBB": 10.0, "CCC": 20.0},
        }

        _, trades, receipt = audit.simulate(
            q5=q5,
            cohorts=cohorts,
            calendar=calendar,
            opens=opens,
            exact_closes=exact,
            mark_closes=marks,
            round_trip_cost=0.0,
        )

        second_buy = trades[
            (trades["side"] == "BUY")
            & (trades["signal_date"] == "20200102")
        ]
        self.assertEqual(len(second_buy), 1)
        self.assertGreater(float(second_buy.iloc[0]["notional"]), 0.0)
        self.assertEqual(receipt["skipped_cohorts_no_cash"], 0)
        self.assertGreaterEqual(receipt["partial_cash_entries"], 1)

    def test_no_fresh_capital_when_all_cash_locked(self):
        q5 = pd.DataFrame([
            {"signal_date": "20200101", "symbol": "AAA"},
            {"signal_date": "20200102", "symbol": "BBB"},
        ])
        cohorts = pd.DataFrame([
            {
                "signal_date": "20200101",
                "entry_date": "20200102",
                "exit_date": "20200103",
                "slot_id": 0,
            },
            {
                "signal_date": "20200102",
                "entry_date": "20200104",
                "exit_date": "20200105",
                "slot_id": 0,
            },
        ])
        calendar = ["20200102", "20200103", "20200104", "20200105"]
        opens = {
            ("20200102", "AAA"): 10.0,
            ("20200104", "BBB"): 20.0,
        }
        exact = {
            ("20200102", "AAA"): 10.0,
            # AAA remains blocked through second entry
            ("20200105", "AAA"): 10.0,
            ("20200104", "BBB"): 20.0,
            ("20200105", "BBB"): 20.0,
        }
        marks = {
            "20200102": {"AAA": 10.0},
            "20200103": {"AAA": 10.0},
            "20200104": {"AAA": 10.0, "BBB": 20.0},
            "20200105": {"AAA": 10.0, "BBB": 20.0},
        }

        _, trades, receipt = audit.simulate(
            q5=q5,
            cohorts=cohorts,
            calendar=calendar,
            opens=opens,
            exact_closes=exact,
            mark_closes=marks,
            round_trip_cost=0.0,
        )

        self.assertEqual(receipt["skipped_cohorts_no_cash"], 1)
        self.assertEqual(
            int((trades["side"] == "SKIP_NO_CASH").sum()),
            1,
        )


if __name__ == "__main__":
    unittest.main()
