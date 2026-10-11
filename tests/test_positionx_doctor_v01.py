import importlib.util
import tempfile
import unittest
from pathlib import Path

HERE=Path(__file__).resolve().parents[1]
SCRIPT=HERE/"scripts"/"run_positionx_doctor.py"
spec=importlib.util.spec_from_file_location("positionx_doctor_v01",SCRIPT)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class T(unittest.TestCase):
    def test_boolean_key(self):
        text="live_enabled: false\nauto_execute: 0\n"
        self.assertEqual(m.parse_boolean_key(text,"live_enabled"),[False])
        self.assertEqual(m.parse_boolean_key(text,"auto_execute"),[False])

    def test_boolean_key_true(self):
        self.assertEqual(m.parse_boolean_key("live_enabled: true\n","live_enabled"),[True])

    def test_lock_parser(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"requirements.lock.txt"
            p.write_text("numpy==1.26.4\n# x\npandas==2.3.3\n",encoding="utf-8")
            self.assertEqual(m.parse_lock(p),{"numpy":"1.26.4","pandas":"2.3.3"})

    def test_result_shape(self):
        r=m.result("x",m.PASS,"ok")
        self.assertEqual(r["domain"],"x")
        self.assertFalse(r["blocking"])

    def test_foundation_missing(self):
        with tempfile.TemporaryDirectory() as td:
            cfg={"required_foundation_files":["A","B"]}
            self.assertEqual(m.check_foundation_files(Path(td),cfg),["A","B"])

    def test_ascii_safe_console_json(self):
        payload={"x":"\ufffd","msg":"中文"}
        rendered=m.json.dumps(payload,ensure_ascii=True,indent=2)
        rendered.encode("ascii")
        self.assertIn(chr(92)+"ufffd",rendered)
        self.assertIn(chr(92)+"u4e2d",rendered)

if __name__=="__main__":
    unittest.main()
