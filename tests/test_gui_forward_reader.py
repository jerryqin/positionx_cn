import json
import tempfile
import unittest
from pathlib import Path

from gui.forward_reader import load_forward_view


class ForwardReaderTests(unittest.TestCase):
    def _base(self, root: Path):
        d = root / "data" / "research" / "forward_breakout_v1"
        d.mkdir(parents=True)
        return d

    def test_not_initialized(self):
        with tempfile.TemporaryDirectory() as td:
            x = load_forward_view(td)
            self.assertFalse(x["available"])
            self.assertEqual(x["status"], "NOT_INITIALIZED")

    def test_reads_armed_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = self._base(root)
            state = {
                "historical_cutoff": "20260924",
                "started_at": None,
                "last_processed_date": "20260924",
                "cash": 1000000.0,
                "positions": {},
                "pending": [],
                "closed_trades": [],
                "blocked_entries": [],
                "daily": [],
                "policy": {
                    "policy": {
                        "initial_capital": 1000000.0,
                        "max_positions": 40,
                        "target_weight": 0.015,
                        "max_total_weight": 0.60,
                        "holding_sessions_from_signal": 20,
                        "round_trip_cost": 0.002,
                        "shadow_round_trip_cost": 0.005,
                        "min_completed_trades_for_review": 60,
                    }
                },
            }
            latest = {
                "status": "OK",
                "health": {
                    "status": "ARMED_WAIT_NEXT_SESSION",
                    "historical_cutoff": "20260924",
                    "last_processed_date": "20260924",
                    "completed_trades": 0,
                    "completed_trades_target": 60,
                    "equity": 1000000.0,
                    "total_return": 0.0,
                    "max_drawdown": 0.0,
                    "active_positions": 0,
                    "pending_signals": 0,
                    "blocked_entries": 0,
                    "base_20bps": {"n": 0},
                    "shadow_50bps": {"n": 0},
                },
            }
            (d/"state.json").write_text(json.dumps(state), encoding="utf-8")
            (d/"latest.json").write_text(json.dumps(latest), encoding="utf-8")
            x = load_forward_view(root)
            self.assertTrue(x["available"])
            self.assertEqual(x["status"], "ARMED_WAIT_NEXT_SESSION")
            self.assertEqual(x["completed_trades"], 0)
            self.assertEqual(x["policy"]["target_weight"], 0.015)

    def test_positions_pending_closed_are_read_only_views(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = self._base(root)
            state = {
                "historical_cutoff": "20260924",
                "started_at": "20260928",
                "last_processed_date": "20261030",
                "positions": {
                    "600001.SH": {
                        "signal_date": "20261029",
                        "entry_date": "20261030",
                        "entry_price": 10.0,
                        "target_weight_at_entry": 0.015,
                        "last_close": 10.2,
                    }
                },
                "pending": [{"symbol": "600002.SH", "signal_date": "20261030"}],
                "closed_trades": [{
                    "symbol": "600003.SH",
                    "net_return_20bps": 0.03,
                    "net_return_50bps": 0.027,
                }],
                "blocked_entries": [{"symbol": "600004.SH", "reason": "NO_SLOT"}],
                "daily": [{"date": "20261030", "equity": 1010000, "gross_weight": 0.30}],
                "policy": {"policy": {"max_positions": 40, "target_weight": 0.015, "max_total_weight": 0.60}},
            }
            latest = {
                "health": {
                    "status": "COLLECTING",
                    "completed_trades": 1,
                    "completed_trades_target": 60,
                    "active_positions": 1,
                    "pending_signals": 1,
                    "blocked_entries": 1,
                    "equity": 1010000,
                    "total_return": 0.01,
                    "max_drawdown": -0.02,
                    "base_20bps": {"n": 1, "avg": 0.03},
                    "shadow_50bps": {"n": 1, "avg": 0.027},
                }
            }
            (d/"state.json").write_text(json.dumps(state), encoding="utf-8")
            (d/"latest.json").write_text(json.dumps(latest), encoding="utf-8")
            x = load_forward_view(root)
            self.assertEqual(x["active_positions"], 1)
            self.assertEqual(x["pending_signals"], 1)
            self.assertEqual(x["blocked_count"], 1)
            self.assertEqual(x["positions"][0]["symbol"], "600001.SH")
            self.assertAlmostEqual(x["gross_weight"], 0.30)

    def test_bad_json_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = self._base(root)
            (d/"state.json").write_text("{bad", encoding="utf-8")
            x = load_forward_view(root)
            self.assertFalse(x["available"])
            self.assertEqual(x["status"], "READ_ERROR")


if __name__ == "__main__":
    unittest.main()
