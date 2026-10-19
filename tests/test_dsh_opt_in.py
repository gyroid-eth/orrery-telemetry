"""未選択の core 配置と、dsh 取得前確認の offline 境界を検査する。"""
from __future__ import annotations

import copy
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

# 実 installer の install_payload を実行する既存 helper を再利用する。
from test_gemini_core_install_boundary import _run_install_payload
import dashboard.service_runner as service_runner


def load_policy():
    spec = importlib.util.spec_from_file_location(
        "dsh_opt_in_under_test", ROOT / "integrations/dsh/opt_in.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DshOptInTests(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy()

    def confirmation(self, plan):
        return {"schema_version": 1, "decision": "approved",
                "plan_sha256": self.policy.plan_digest(plan),
                "approval_id": "offline-fixture", "actor": "operator",
                "confirmed_at": "2026-01-01T00:00:00Z"}

    def test_default_does_not_resolve_or_start_any_external_effect(self):
        # HOME/config・鍵・登録の解決へ先回りしない。module import も対象。
        with patch.object(Path, "home", side_effect=AssertionError("HOME参照")), \
             patch.object(Path, "read_text", side_effect=AssertionError("設定参照")), \
             patch.object(subprocess, "Popen", side_effect=AssertionError("process起動")), \
             patch.object(socket, "socket", side_effect=AssertionError("通信")), \
             patch.dict(os.environ, {"DEEPSEEK_API_KEY": "dummy-not-a-key",
                                    "AGENTSTACK_DSH_ENABLED": "1"}):
            policy = load_policy()
            plan = policy.build_plan()
            self.assertEqual(plan["phase"], "disabled")
            self.assertEqual(plan["downloads"], [])
            self.assertEqual(plan["requirements"], [])
            self.assertEqual(policy.authorized_downloads(plan, object()), [])

    def test_core_payload_keeps_dsh_absent_and_core_entry_unchanged(self):
        # live の installer/main や daemon は動かさず、実配置関数だけを隔離実行。
        with tempfile.TemporaryDirectory(prefix="dsh-null-") as temporary:
            base = Path(temporary)
            commands = base / "commands"
            commands.mkdir()
            effect_log = base / "forbidden-effects.txt"
            for name in ("curl", "wget", "npm", "npx", "node", "security", "tmux",
                         "agentstack-preregister-child", "agentstack-runtime-client"):
                path = commands / name
                path.write_text('#!/bin/bash\nprintf "%s\\n" "$0" >> "$DSH_TEST_EFFECT_LOG"\nexit 96\n')
                path.chmod(0o700)
            installed = base / "installed"
            env = {"PATH": f"{commands}:/usr/bin:/bin",
                   "DSH_TEST_EFFECT_LOG": str(effect_log)}
            with patch.dict(os.environ, env, clear=True):
                result = _run_install_payload(installed)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(effect_log.exists(), "取得・鍵・登録等の外部commandが呼ばれました")
            for relative in ("integrations/dsh", "dashboard/dsh_provider_runtime.py",
                             "dashboard/assets/deepseek.svg", "hooks/spawn_dsh_child.sh"):
                self.assertFalse((installed / relative).exists(), relative)
            for relative in ("dashboard/server.py", "dashboard/index.html",
                             "dashboard/network_node.js", "hooks/spawn_child.sh"):
                self.assertEqual((installed / relative).read_bytes(),
                                 (ROOT / relative).read_bytes(), relative)
            with patch.object(service_runner, "HERE", installed / "dashboard"):
                self.assertEqual(service_runner._default_server_path(),
                                 installed / "dashboard/server.py")

    def test_explicit_opt_in_only_plans_pinned_packages(self):
        with patch.object(subprocess, "Popen", side_effect=AssertionError("process起動")), \
             patch.object(socket, "socket", side_effect=AssertionError("通信")):
            plan = self.policy.build_plan(opt_in=True)
            self.assertEqual(plan["phase"], "planned")
            self.assertEqual(len(plan["downloads"]), 2)
            self.assertEqual({item["version"] for item in plan["downloads"]},
                             {"0.2.1-rc.2", "0.16.1"})
            self.assertTrue(all(item["integrity"].startswith("sha512-")
                                for item in plan["downloads"]))
            self.assertEqual(plan["requirements"][0]["acquisition"], "existing-pinned-binary")

    def test_non_boolean_selection_is_not_opt_in(self):
        for value in (None, 0, 1, "true", "false", [], {}):
            with self.subTest(value=value), self.assertRaises(self.policy.OptInError):
                self.policy.build_plan(opt_in=value)

    def test_confirmation_is_required_and_bound_to_exact_plan(self):
        plan = self.policy.build_plan(opt_in=True)
        for receipt in (None, {}, True, {"decision": "approved"}):
            with self.subTest(receipt=receipt), self.assertRaises(self.policy.OptInError):
                self.policy.authorized_downloads(plan, receipt)
        receipt = self.confirmation(plan)
        self.assertEqual(self.policy.authorized_downloads(plan, receipt), plan["downloads"])
        for field, value in (("plan_sha256", "0" * 64), ("decision", "declined"),
                             ("schema_version", True), ("actor", ""),
                             ("confirmed_at", "2026-02-30T00:00:00Z"),
                             ("approval_id", "../wrong")):
            altered = {**receipt, field: value}
            with self.subTest(field=field), self.assertRaises(self.policy.OptInError):
                self.policy.authorized_downloads(plan, altered)

    def test_changed_package_or_unknown_field_cannot_be_approved(self):
        original = self.policy.build_plan(opt_in=True)
        changes = [
            lambda p: p.update(schema_version=True),
            lambda p: p.update(extra="not-allowed"),
            lambda p: p.update(phase="active"),
            lambda p: p["downloads"][0].update(url="https://example.invalid/package.tgz"),
            lambda p: p["downloads"][0].update(version="latest"),
            lambda p: p["downloads"][0].update(integrity="sha512-wrong"),
            lambda p: p["requirements"][0].update(acquisition="automatic"),
        ]
        for change in changes:
            altered = copy.deepcopy(original)
            change(altered)
            with self.subTest(plan=altered), self.assertRaises(self.policy.OptInError):
                self.policy.authorized_downloads(altered, self.confirmation(original))

    def test_mutating_returned_plan_does_not_change_next_plan(self):
        first = self.policy.build_plan(opt_in=True)
        first["downloads"][0]["url"] = "https://example.invalid/"
        second = self.policy.build_plan(opt_in=True)
        self.assertNotEqual(first, second)
        receipt = self.confirmation(second)
        authorized = self.policy.authorized_downloads(second, receipt)
        authorized.clear()
        self.assertEqual(len(self.policy.authorized_downloads(second, receipt)), 2)


if __name__ == "__main__":
    unittest.main()
