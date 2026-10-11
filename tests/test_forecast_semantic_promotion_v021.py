import sys
import unittest
import importlib.util
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location(
    "p",ROOT/"scripts"/"promote_forecast_semantic_corpus_v021.py"
)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_qtable(self):
        self.assertEqual(
            m.qtable("research.forecast_semantic_events_pit_v1"),
            '"research"."forecast_semantic_events_pit_v1"'
        )

    def test_sql_string(self):
        self.assertEqual(m.sql_string("a'b"),"'a''b'")

if __name__=="__main__":
    unittest.main()
