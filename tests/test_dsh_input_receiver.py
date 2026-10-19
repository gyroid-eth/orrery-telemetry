"""Node標準test runnerでplugin coreを検査する。取得やprovider起動はしない。"""
from pathlib import Path
import os
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class InputReceiverTests(unittest.TestCase):
    def test_plugin_core_and_paste_recovery(self):
        node = os.environ.get("DSH_TEST_NODE") or shutil.which("node")
        if not node:
            self.skipTest("既存Nodeがないため未実行。試験のために取得しない")
        result = subprocess.run(
            [node, "--test", str(ROOT / "tests/dsh_input_receiver.test.mjs")],
            cwd=ROOT, env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            text=True, capture_output=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print(result.stdout)


if __name__ == "__main__":
    unittest.main()
