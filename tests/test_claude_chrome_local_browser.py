#!/usr/bin/env python3
"""Unit tests for Claude in Chrome default local-machine browser selection.

Pure stdlib (no git, tmux, or network required). Runnable two ways:
    python3 tests/test_claude_chrome_local_browser.py
    pytest tests/test_claude_chrome_local_browser.py
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
POLICY_PATH = ROOT / "hooks" / "claude_chrome_policy.py"
LAUNCH_ID = "11111111-2222-3333-4444-555555555555"
SESSION_ID = "0123abcd-4567-89ef-0123-456789abcdef"
OTHER_SESSION_ID = "fedcba98-7654-3210-fedc-ba9876543210"


def _load_policy():
    spec = importlib.util.spec_from_file_location("claude_chrome_policy_local_test", POLICY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_policy(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(POLICY_PATH), *args],
        text=True,
        capture_output=True,
        check=False,
    )


class TestClaudeChromeLocalBrowser(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = _load_policy()

    def test_local_browser_os_mapping(self) -> None:
        # macOS always resolves to macOS, even if WSL env vars happen to be present.
        self.assertEqual(
            self.policy.local_browser_os(platform="darwin", environ={"WSL_DISTRO_NAME": "Ubuntu"}),
            "macOS",
        )
        # Native Windows resolves to Windows.
        self.assertEqual(self.policy.local_browser_os(platform="win32"), "Windows")
        # WSL resolves to Windows (browser runs on the Windows host).
        self.assertEqual(self.policy.local_browser_os(platform="linux", wsl=True), "Windows")
        self.assertEqual(
            self.policy.local_browser_os(platform="linux", environ={"WSL_DISTRO_NAME": "Ubuntu"}),
            "Windows",
        )
        self.assertEqual(
            self.policy.local_browser_os(platform="linux", environ={"WSL_INTEROP": "/run/WSL/1_interop"}),
            "Windows",
        )
        # Native Linux resolves to Linux.
        self.assertEqual(self.policy.local_browser_os(platform="linux", wsl=False), "Linux")
        # Unknown OS returns None so policy falls back fail-closed.
        self.assertIsNone(self.policy.local_browser_os(platform="freebsd14"))

    def test_wsl_detection_checks_proc_version_and_interop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            proc_wsl = root / "proc_version_wsl"
            proc_wsl.write_text(
                "Linux version 5.15.153.1-microsoft-standard-WSL2 (root@host)\n",
                encoding="utf-8",
            )
            proc_linux = root / "proc_version_linux"
            proc_linux.write_text(
                "Linux version 6.8.0-generic (buildd@host)\n",
                encoding="utf-8",
            )
            interop = root / "WSLInterop"
            missing = str(root / "missing")

            self.assertTrue(
                self.policy._running_in_wsl(
                    {}, proc_version_path=str(proc_wsl), interop_path=missing
                )
            )
            self.assertFalse(
                self.policy._running_in_wsl(
                    {}, proc_version_path=str(proc_linux), interop_path=missing
                )
            )
            interop.write_text("enabled\n", encoding="utf-8")
            self.assertTrue(
                self.policy._running_in_wsl(
                    {}, proc_version_path=str(proc_linux), interop_path=str(interop)
                )
            )

    def test_default_policy_without_device_selects_local_os(self) -> None:
        for os_name in ("macOS", "Windows", "Linux"):
            for standalone, report_to in ((False, "your parent agent"), (True, "the operator")):
                with self.subTest(os_name=os_name, standalone=standalone):
                    text = self.policy.policy_text("", standalone, target_os=os_name)
                    self.assertIn(f"use this machine's {os_name} browser", text)
                    self.assertIn("use no browser tool except list_connected_browsers", text)
                    self.assertIn(f"whose OS is {os_name}", text)
                    self.assertIn(
                        f"If exactly one connected browser matches {os_name}, "
                        "call select_browser with its deviceId",
                        text,
                    )
                    self.assertIn("tabs_create_mcp", text)
                    self.assertIn(
                        f"If zero or more than one connected browser matches {os_name}, "
                        "do not select or switch browsers, open tabs, navigate, read pages or click",
                        text,
                    )
                    self.assertIn(f"to {report_to} and wait for the deviceId to use", text)
                    self.assertIn("Never fall back to a browser on another OS or machine", text)
                    self.assertIn("not a technical lock", text)

    def test_default_policy_unknown_os_fails_closed(self) -> None:
        for standalone, report_to in ((False, "your parent agent"), (True, "the operator")):
            with self.subTest(standalone=standalone):
                text = self.policy.policy_text("", standalone, target_os=None)
                self.assertIn("no target browser was specified", text)
                self.assertIn("OS could not be determined", text)
                self.assertIn("do not select or switch browsers, open tabs, navigate, read pages or click", text)
                self.assertIn(f"to {report_to} and wait for the deviceId to use", text)
                self.assertNotIn("call select_browser", text)

    def test_explicit_device_overrides_local_os(self) -> None:
        # Even on macOS, an explicit deviceId targeting another machine stays pinned to that deviceId.
        for standalone, report_to in ((False, "your parent agent"), (True, "the operator")):
            with self.subTest(standalone=standalone):
                text = self.policy.policy_text("win-brave.1", standalone, target_os="macOS")
                self.assertIn("The browser to use is deviceId win-brave.1", text)
                self.assertIn("call select_browser with exactly that deviceId", text)
                self.assertIn("Never fall back to another browser (not the first one listed, not the local one", text)
                self.assertIn(f"report it to {report_to}", text)
                self.assertNotIn("use this machine's", text)

    def test_cli_prompt_and_session_without_and_with_device(self) -> None:
        expected_os = self.policy.local_browser_os()
        self.assertIn(expected_os, ("macOS", "Windows", "Linux"))

        # CLI `prompt` without deviceId selects this machine's OS.
        no_dev = _run_policy("prompt", "", "0")
        self.assertEqual(no_dev.returncode, 0, no_dev.stderr)
        self.assertIn(f"use this machine's {expected_os} browser", no_dev.stdout)
        self.assertIn("your parent agent", no_dev.stdout)

        # CLI `prompt` with explicit deviceId keeps exact deviceId selection.
        with_dev = _run_policy("prompt", "remote-mac-2", "1")
        self.assertEqual(with_dev.returncode, 0, with_dev.stderr)
        self.assertIn("The browser to use is deviceId remote-mac-2", with_dev.stdout)
        self.assertIn("the operator", with_dev.stdout)

        # CLI `prepare` + `session` (first start, resume, /clear) without deviceId.
        with tempfile.TemporaryDirectory() as tmp:
            prep = _run_policy("prepare", tmp, "LocalChild", LAUNCH_ID, "", "0")
            self.assertEqual(prep.returncode, 0, prep.stderr)

            first = _run_policy("session", tmp, "LocalChild", SESSION_ID, LAUNCH_ID)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn(f"use this machine's {expected_os} browser", first.stdout)

            resumed = _run_policy("session", tmp, "LocalChild", SESSION_ID, "")
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertIn(f"use this machine's {expected_os} browser", resumed.stdout)

            cleared = _run_policy("session", tmp, "LocalChild", OTHER_SESSION_ID, LAUNCH_ID)
            self.assertEqual(cleared.returncode, 0, cleared.stderr)
            self.assertIn(f"use this machine's {expected_os} browser", cleared.stdout)

            # Separate agent with explicit deviceId preserves that deviceId across resume.
            prep_dev = _run_policy("prepare", tmp, "PinnedChild", LAUNCH_ID, "mac-chrome-2", "1")
            self.assertEqual(prep_dev.returncode, 0, prep_dev.stderr)
            pinned = _run_policy("session", tmp, "PinnedChild", SESSION_ID, LAUNCH_ID)
            self.assertEqual(pinned.returncode, 0, pinned.stderr)
            self.assertIn("The browser to use is deviceId mac-chrome-2", pinned.stdout)
            self.assertIn("the operator", pinned.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
