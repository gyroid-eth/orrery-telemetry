"""実Node・API・鍵を使わず、起動計画の消費境界を検査する。"""
from __future__ import annotations

from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dsh_launch_plan_test", ROOT / "integrations/dsh/launch_plan.py")
policy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = policy
SPEC.loader.exec_module(policy)


class LaunchPlanTests(unittest.TestCase):
    def spec(self):
        pin = lambda path: policy.FilePin(path, "a" * 64)
        return policy.LaunchInput(
            node=pin("/opt/runtime/node"), node_version="v24.3.0",
            dsh_entry=pin("/opt/provider/dsh/bin.js"),
            guard_entry=pin("/opt/provider/guard.mjs"),
            patch_path="/opt/state/managed.patch.yml", home="/home/operator",
            worktree="/opt/worktree", state_root="/opt/state/attempt-one",
            attempt_id="attempt-one")

    def test_fixed_node_and_entry_are_argv_without_a_shell(self):
        with patch.object(subprocess, "Popen", side_effect=AssertionError("process起動")), \
             patch.object(socket, "socket", side_effect=AssertionError("通信")), \
             patch.object(Path, "read_text", side_effect=AssertionError("file読取")):
            value = self.spec()
            plan = policy.build_launch(value)
        self.assertEqual(plan.argv, (
            "/opt/runtime/node", "--import", "/opt/provider/guard.mjs",
            "/opt/provider/dsh/bin.js", "--profile", "orrery-dsh-tui",
            "--patch", "/opt/state/managed.patch.yml"))
        self.assertEqual(plan.cwd, "/opt/worktree")
        self.assertEqual(plan.pins, (value.node, value.dsh_entry, value.guard_entry))

    def test_ordinary_old_or_unfixed_node_is_rejected_before_key_handoff(self):
        for version in ("v22.17.0", "24.1.9", "24.2", "24.3.0-rc.1", None):
            with self.subTest(version=version), self.assertRaises(policy.LaunchPlanError):
                policy.build_launch(replace(self.spec(), node_version=version))
        for pin in (policy.FilePin("node", "a" * 64), policy.FilePin("/opt/node", "")):
            with self.subTest(pin=pin), self.assertRaises(policy.LaunchPlanError):
                policy.build_launch(replace(self.spec(), node=pin))
        policy.build_launch(replace(self.spec(), node_version="24.2.0"))

    def test_ambient_secrets_and_node_options_are_not_inherited(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "dummy-parent",
                                    "NODE_OPTIONS": "--import=/unexpected/module.mjs",
                                    "MCP_AGENT_MAIL_TOKEN": "dummy-token",
                                    "DSH_HOME": "/unexpected/shared"}):
            plan = policy.build_launch(self.spec())
        self.assertNotIn("dummy-parent", repr(plan))
        self.assertNotIn("dummy-token", repr(plan))
        self.assertNotIn("NODE_OPTIONS", plan.public_env)
        self.assertEqual(plan.public_env["HOME"], self.spec().home)
        self.assertEqual(plan.public_env["DSH_HOME"], "/opt/state/attempt-one/dsh")

    def test_key_is_added_only_to_runtime_environment(self):
        plan = policy.build_launch(self.spec())
        runtime = policy.runtime_environment(plan, "dummy-runtime-key")
        auxiliary = policy.auxiliary_environment(plan)
        self.assertEqual(runtime["DEEPSEEK_API_KEY"], "dummy-runtime-key")
        self.assertNotIn("DEEPSEEK_API_KEY", auxiliary)
        self.assertNotIn("DEEPSEEK_API_KEY", plan.public_env)
        self.assertNotIn("dummy-runtime-key", " ".join(plan.argv))
        self.assertNotIn("ORRERY_DSH_LIMITS", auxiliary)
        with self.assertRaises(policy.LaunchPlanError):
            policy.runtime_environment(plan, "")

    def test_title_retry_and_request_limits_form_one_consistent_plan(self):
        plan = policy.build_launch(replace(self.spec(), limits=policy.Limits(4, 2, 512, 30)))
        rows = {row["id"]: row for row in plan.patch_rows}
        for name in ("session-title", "session-title-llm", "llm-retry"):
            self.assertTrue(rows[name]["disabled"])
        llm = rows["llm-deepseek"]["config"]
        self.assertEqual(llm["retryPolicy"], {"mode": "normal", "maxRetries": 0})
        limits = json.loads(plan.public_env["ORRERY_DSH_LIMITS"])
        self.assertEqual((limits["session_requests"], limits["turn_requests"]), (4, 2))
        self.assertEqual(limits["max_output_tokens"], llm["maxTokens"])
        self.assertEqual(limits["timeout_seconds"], 30)
        self.assertFalse(limits["fallback"])
        self.assertEqual(limits["token_control"], "post_usage_stop")

    def test_effort_matches_every_value_of_pinned_deepseek_schema(self):
        for effort in ("off", "low", "high", "max"):
            with self.subTest(effort=effort):
                plan = policy.build_launch(replace(self.spec(), effort=effort))
                rows = {row["id"]: row for row in plan.patch_rows}
                self.assertEqual(rows["llm-deepseek"]["config"]["reasoningEffort"], effort)
                self.assertEqual(rows["agent-default-model"]["config"]["reasoningEffort"], effort)
        for effort in ("none", "medium", "unlimited", "", None):
            with self.subTest(effort=effort), self.assertRaises(policy.LaunchPlanError):
                policy.build_launch(replace(self.spec(), effort=effort))

    def test_unbounded_limits_and_missing_context_are_rejected(self):
        for limits in (policy.Limits(session_requests=0), policy.Limits(turn_requests=True),
                       policy.Limits(max_output_tokens=-1), policy.Limits(timeout_seconds=0)):
            with self.subTest(limits=limits), self.assertRaises(policy.LaunchPlanError):
                policy.build_launch(replace(self.spec(), limits=limits))
        for changes in ({"patch_path": "relative.yml"}, {"attempt_id": ""},
                        {"model": ""}, {"effort": "unlimited"}):
            with self.subTest(changes=changes), self.assertRaises(policy.LaunchPlanError):
                policy.build_launch(replace(self.spec(), **changes))


if __name__ == "__main__":
    unittest.main()
