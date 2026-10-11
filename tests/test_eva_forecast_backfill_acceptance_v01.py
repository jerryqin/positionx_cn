import sys, unittest, importlib.util
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location(
    "v", ROOT/"scripts"/"validate_eva_forecast_backfill_v01.py"
)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_semantic_payload(self):
        df=pd.DataFrame({"profit_state":["PROFIT",None],"change_type":[None,"TURNAROUND"]})
        self.assertEqual(m.semantic_payload_rate(df),1.0)

    def test_event_years_use_event_time(self):
        df=pd.DataFrame({"event_time":["2020-01-02"],"report_period":["2018_ANNUAL"]})
        _,years=m.event_years(df)
        self.assertEqual(int(years.iloc[0]),2020)

    def test_nonempty_rate(self):
        df=pd.DataFrame({"symbol":["000001.SZ","",None]})
        self.assertAlmostEqual(m.nonempty_rate(df,"symbol"),1/3)

    def test_required_columns_contract(self):
        self.assertIn("event_time",m.REQUIRED_COLUMNS)
        self.assertIn("pdf_sha256",m.REQUIRED_COLUMNS)
        self.assertIn("announcementId",m.REQUIRED_COLUMNS)

    def test_year_set_logic(self):
        requested={2020,2021,2022,2023}
        observed={2020,2022,2023}
        self.assertEqual(sorted(requested-observed),[2021])

if __name__=="__main__":
    unittest.main()
