"""Claude Code children take an effort level, from the CLI's own supported set.

The level sets come from the Claude Code docs ("Adjust effort level"); the
launcher hands the choice to `claude --effort`.  Everything here runs against a
temporary HOME, a fake tmux and a stand-in ORRERY Mail on a free port.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_spawn_child_embed_task import ROOT, SPAWN  # noqa: E402
from test_spawn_child_legacy_claude import (  # noqa: E402,F401
    NAME, _legacy_child, mail_factory,
)

from dashboard import claude_models, server  # noqa: E402

ALL = ("low", "medium", "high", "xhigh", "max")


@pytest.mark.parametrize("model, levels, default", [
    ("claude-fable-5-1", ALL, "high"),
    ("claude-opus-5-5", ALL, "medium"),
    ("claude-sonnet-5-5", ALL, "medium"),
    ("claude-haiku-5-5", ALL, "medium"),
    ("claude-opus-5", ALL, "high"),
    ("claude-sonnet-5[1m]", ALL, "high"),
    ("claude-opus-4-8", ALL, "high"),
    ("claude-opus-4-7", ALL, "xhigh"),
    ("claude-opus-4-6", ("low", "medium", "high", "max"), "high"),
    ("claude-sonnet-4-6", ("low", "medium", "high", "max"), "high"),
    ("claude-haiku-4-5-20251001", (), ""),
    ("claude-sonnet-4-5", (), ""),
    ("not-a-model", (), ""),
])
def test_effort_policy_follows_the_documented_table(model, levels, default):
    assert claude_models.effort_policy(model) == (levels, default)


def test_resolve_effort_rejects_what_the_model_does_not_support():
    assert claude_models.resolve_effort("claude-opus-5-5", " HIGH ") == "high"
    assert claude_models.resolve_effort("claude-opus-5-5", "") == ""
    assert claude_models.resolve_effort("claude-haiku-4-5-20251001", "") == ""
    with pytest.raises(ValueError, match="not supported for claude-opus-4-6"):
        claude_models.resolve_effort("claude-opus-4-6", "xhigh")
    with pytest.raises(ValueError, match="effort not supported for model"):
        claude_models.resolve_effort("claude-haiku-4-5-20251001", "low")
    with pytest.raises(ValueError):
        claude_models.resolve_effort("claude-opus-5-5", "ultra")


def test_spawn_catalog_advertises_claude_efforts_per_model(monkeypatch):
    monkeypatch.setattr(server, "_claude_catalog", lambda: claude_models.ModelCatalog(
        ("claude-opus-5-5", "claude-opus-4-6", "claude-haiku-4-5-20251001"), "test"))
    monkeypatch.setattr(server, "_spawn_scientist_statuses", lambda *_: {})
    provider = server.spawn_names_payload()["providers"][0]
    assert provider["id"] == "claude"
    assert provider["efforts"] == list(ALL)
    assert provider["model_efforts"] == {
        "claude-opus-5-5": list(ALL),
        "claude-opus-4-6": ["low", "medium", "high", "max"],
        "claude-haiku-4-5-20251001": [],
    }
    assert provider["model_effort_defaults"]["claude-opus-5-5"] == "medium"
    assert provider["model_effort_defaults"]["claude-haiku-4-5-20251001"] == ""


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Keep do_spawn off the live service: no Mail, no project key from the environment."""
    monkeypatch.setattr(server, "_project_key", lambda: "/project")
    monkeypatch.setattr(server, "_spawn_unavailable_error", lambda: None)
    monkeypatch.setattr(server, "_spawn_name_status", lambda _: "available")
    monkeypatch.setattr(server, "_bind_claude_child_cli_path", lambda _hooks: "claude")
    monkeypatch.setattr(server, "SPAWN_SCRIPT", str(ROOT / "hooks" / "spawn_child.sh"))
    return tmp_path


def _dry(tmp, **extra):
    return server.do_spawn({"parent": "P", "task": "w", "provider": "claude", "dry_run": True,
                            "dir": str(tmp), **extra})


def test_do_spawn_passes_a_valid_effort_to_the_launcher(isolated):
    result = _dry(isolated, model="claude-opus-5-5", effort="High")
    assert result["ok"] is True and result["effort"] == "high"
    assert result["argv"][result["argv"].index("--effort") + 1] == "high"


def test_do_spawn_omits_effort_when_none_is_chosen(isolated):
    result = _dry(isolated, model="claude-opus-5-5")
    assert result["ok"] is True and "--effort" not in result["argv"]


def test_do_spawn_refuses_an_effort_the_model_cannot_take(isolated):
    assert _dry(isolated, model="claude-opus-4-6", effort="xhigh")["ok"] is False
    assert _dry(isolated, model="claude-haiku-4-5-20251001", effort="low") == {
        "ok": False, "error": "effort not supported for model: claude-haiku-4-5-20251001"}


def _run(env, workdir, tmp_path, *flags):
    task = tmp_path / "task.md"
    task.write_text("effort task", encoding="utf-8")
    return subprocess.run(
        ["/bin/bash", str(SPAWN), "--pre-registered", NAME, "--embed-task",
         "--task-file", str(task), *flags, str(workdir)],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=60, check=False)


def _new_session(env):
    log = pathlib.Path(env["FAKE_TMUX_LOG"]).read_text(encoding="utf-8")
    return [call for call in log.split("\035\n") if call.startswith("CALL\034new-session")]


def test_launcher_gives_claude_the_effort_flag(tmp_path, mail_factory):
    env, workdir, *_ = _legacy_child(tmp_path, mail_factory())
    result = _run(env, workdir, tmp_path, "--model", "claude-opus-5-5", "--effort", "max")
    assert result.returncode == 0, result.stderr
    (call,) = _new_session(env)
    assert "CLAUDE_CHILD_EFFORT=max" in call
    assert '--effort "$CLAUDE_CHILD_EFFORT"' in call


def test_launcher_adds_no_effort_flag_by_default(tmp_path, mail_factory):
    env, workdir, *_ = _legacy_child(tmp_path, mail_factory())
    result = _run(env, workdir, tmp_path, "--model", "claude-opus-5-5")
    assert result.returncode == 0, result.stderr
    (call,) = _new_session(env)
    assert "--effort" not in call


def test_launcher_refuses_an_unsupported_effort_before_starting(tmp_path, mail_factory):
    env, workdir, *_ = _legacy_child(tmp_path, mail_factory())
    result = _run(env, workdir, tmp_path, "--model", "claude-haiku-4-5-20251001", "--effort", "low")
    assert result.returncode != 0
    assert "effort not supported for model" in result.stderr
    log = pathlib.Path(env["FAKE_TMUX_LOG"])
    assert not log.exists() or not _new_session(env)
