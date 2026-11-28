"""业务服务端到端检查：复用 scripts/run_checks 的完整合成演练。"""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_checks  # noqa: E402


class ServiceFlowChecks(unittest.TestCase):
    def test_full_contract_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            partner = subprocess.Popen(
                [sys.executable, str(ROOT / "partner/server.py"), "--port", "0",
                 "--state", str(Path(tmp) / "partner.sqlite3")],
                stdout=subprocess.PIPE, text=True, cwd=str(ROOT))
            try:
                fund_port = json.loads(partner.stdout.readline())["port"]
                service = subprocess.Popen(
                    [sys.executable, "-m", "forest.server", "--port", "0",
                     "--db", str(Path(tmp) / "forest.sqlite3"),
                     "--fund-base-url", f"http://127.0.0.1:{fund_port}"],
                    stdout=subprocess.PIPE, text=True, cwd=str(ROOT))
                try:
                    service_port = json.loads(service.stdout.readline())["port"]
                    base = f"http://127.0.0.1:{service_port}"
                    run_checks.wait_health(base)
                    with contextlib.redirect_stdout(io.StringIO()):
                        counts = run_checks.run_checks(base, f"http://127.0.0.1:{fund_port}")
                    self.assertEqual(counts["passed"], counts["checks"])
                    self.assertGreaterEqual(counts["checks"], 30)
                finally:
                    service.terminate()
                    service.wait(timeout=5)
                    service.stdout.close()
            finally:
                partner.terminate()
                partner.wait(timeout=5)
                partner.stdout.close()


if __name__ == "__main__":
    unittest.main()
