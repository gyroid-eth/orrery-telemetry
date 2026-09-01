#!/usr/bin/env python3
"""Regression tests for Codex child launch flags, readiness and window focus.

Covers the 2026-07-24 tester report, defect C (sections 6.3 and 6.5) and the
2026-07-22 UX question about the child window stealing focus.

Runnable two ways (no third-party dependency required):
    python3 tests/test_codex_launch.py
    pytest tests/test_codex_launch.py
"""
from __future__ import annotations

import os
import pathlib
import shlex
import stat
import subprocess
import sys
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SPAWN = _ROOT / "hooks" / "spawn_child.sh"

# Real footers observed in the field. The first is what the tester's Codex
# printed while idle and accepting input — it has no "% left" segment, which is
# why matching that one string made the launcher wait out its whole timeout.
_FOOTER_TESTER = "\n".join(["", "  gpt-5.5 xhigh · ~/obsidian", ""])
_FOOTER_CONTEXT = "\n".join(
    ["", "  gpt-5.5 medium · Context 100% left · ~/workspace/notes", ""]
)
_FOOTER_SHORTCUTS = "\n".join(["", "  ? for shortcuts", ""])
_MODEL_DIALOG = "\n".join(
    ["  Use existing model", "  Upgrade", "  gpt-5.5 xhigh · ~/obsidian"]
)
_STARTING_UP = "\n".join(["Loading...", "", ""])


def _extract(func: str) -> str:
    """Pull one function definition out of the launcher.

    spawn_child.sh runs its main flow at import time, so the helpers are
    extracted rather than sourced.
    """
    text = _SPAWN.read_text(encoding="utf-8")
    marker = f"\n{func}() {{"
    start = text.index(marker) + 1
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def _model_catalog() -> str:
    text = _SPAWN.read_text(encoding="utf-8")
    start = text.index("# --- Child model catalog")
    end = text.index("# --- Claude モデル名の正規化 ---", start)
    return text[start:end]


def _run_bash(script: str, env: dict[str, str] | None = None,
              check: bool = False) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        ["bash", "-c", script],
        cwd=_ROOT,
        env=run_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def _ready(pane: str) -> bool:
    script = _extract("codex_pane_ready") + '\ncodex_pane_ready "$PANE"\n'
    return _run_bash(script, {"PANE": pane}).returncode == 0


def test_readiness_accepts_every_observed_idle_footer():
    # The regression: this footer used to read as "not ready" for 90s.
    assert _ready(_FOOTER_TESTER)
    assert _ready(_FOOTER_CONTEXT)
    assert _ready(_FOOTER_SHORTCUTS)


def test_readiness_rejects_dialogs_and_startup():
    # A pending model dialog is not readiness, even though the footer is drawn.
    assert not _ready(_MODEL_DIALOG)
    assert not _ready(_STARTING_UP)
    assert not _ready("")


def _codex_stub(tmpdir: pathlib.Path, help_text: str) -> None:
    stub = tmpdir / "codex"
    stub.write_text(
        "#!/bin/bash\n"
        'if [[ "$1" == "--help" ]]; then\n'
        f"  cat <<'EOF'\n{help_text}\nEOF\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)


def _flags(help_text: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        _codex_stub(tmpdir, help_text)
        script = _extract("codex_approval_flags") + "\ncodex_approval_flags\n"
        return _run_bash(
            script, {"PATH": f"{tmpdir}:{os.environ['PATH']}"}
        ).stdout.strip()


def test_approval_flags_follow_the_installed_cli():
    # Current CLI (0.144.6 and later): --full-auto was removed.
    assert _flags("  -s, --sandbox <MODE>\n      --ask-for-approval <POLICY>") == \
        "--ask-for-approval never"
    # Older CLI that still has it.
    assert _flags("  -s, --sandbox <MODE>\n      --full-auto") == "--full-auto"
    # Unknown build: pass nothing rather than an argument it would reject.
    assert _flags("  -s, --sandbox <MODE>") == ""


def _model_call(function: str, *args: str) -> subprocess.CompletedProcess[str]:
    functions = ["normalize_claude_model", "normalize_codex_model",
                 "validate_codex_effort"]
    script = _model_catalog() + "\n" + "\n".join(
        _extract(name) for name in functions
    )
    command = " ".join([function, *(shlex.quote(arg) for arg in args)])
    return _run_bash(script + "\n" + command + "\n")


def test_model_catalog_tracks_current_generations_without_dropping_old_ids():
    expected = {
        ("normalize_claude_model", ""): "claude-opus-5",
        ("normalize_claude_model", "opus"): "claude-opus-5",
        ("normalize_claude_model", "opus[1m]"): "claude-opus-4-8[1m]",
        ("normalize_claude_model", "claude-opus-4-8"): "claude-opus-4-8",
        ("normalize_claude_model", "opus-5[1m]"): "claude-opus-5[1m]",
        ("normalize_claude_model", "sonnet"): "claude-sonnet-5",
        ("normalize_claude_model", "sonnet-4-6"): "claude-sonnet-4-6",
        ("normalize_claude_model", "fable"): "claude-fable-5",
        ("normalize_codex_model", ""): "gpt-5.6-sol",
        ("normalize_codex_model", "sol"): "gpt-5.6-sol",
        ("normalize_codex_model", "terra"): "gpt-5.6-terra",
        ("normalize_codex_model", "luna"): "gpt-5.6-luna",
        ("normalize_codex_model", "gpt-5.5"): "gpt-5.5",
    }
    for (function, raw), normalized in expected.items():
        result = _model_call(function, raw)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == normalized


def test_model_specific_effort_constraints_are_enforced():
    accepted = _model_call("validate_codex_effort", "gpt-5.6-sol", "ultra")
    assert accepted.returncode == 0
    assert accepted.stdout.strip() == "ultra"

    luna = _model_call("validate_codex_effort", "gpt-5.6-luna", "ultra")
    assert luna.returncode != 0
    assert "does not support ultra" in luna.stderr

    legacy = _model_call("validate_codex_effort", "gpt-5.5", "max")
    assert legacy.returncode != 0
    assert "only through xhigh" in legacy.stderr

    unknown = _model_call("validate_codex_effort", "gpt-5.6-sol", "extreme")
    assert unknown.returncode != 0
    assert "unknown Codex reasoning effort" in unknown.stderr


def test_both_launch_paths_use_the_shared_model_catalog():
    text = _SPAWN.read_text(encoding="utf-8")
    assert text.count('normalize_codex_model "$CLAUDE_MODEL"') == 2
    assert text.count('validate_codex_effort "$CHILD_MODEL" "$CODEX_EFFORT"') == 2
    assert text.count('normalize_claude_model "$CLAUDE_MODEL"') == 2
    assert '${CLAUDE_MODEL:-gpt-5.5}' not in text
    assert '"$CLAUDE_WARM_OPUS_MODEL")' in text
    assert '"$CLAUDE_WARM_SONNET_MODEL")' in text


def test_launcher_no_longer_hardcodes_full_auto():
    text = _SPAWN.read_text(encoding="utf-8")
    assert "--full-auto \\" not in text, "hardcoded --full-auto still in a launch line"
    # Both launch paths take the probed flags from the child environment.
    assert text.count("--sandbox workspace-write ${=AGENTSTACK_CODEX_APPROVAL}") == 2
    assert text.count('-e "AGENTSTACK_CODEX_APPROVAL=$(codex_approval_flags)"') == 2


def test_dead_child_fails_fast_instead_of_waiting_out_the_timeout():
    text = _SPAWN.read_text(encoding="utf-8")
    # Both readiness loops check liveness and abort.
    assert text.count("codex_session_alive") == 3, "expected 1 definition + 2 call sites"
    assert text.count("DIED=true") == 2
    assert text.count("exited before becoming ready") == 2


def test_trust_dialog_uses_carriage_return_and_has_a_hard_attempt_limit():
    helper = _extract("codex_accept_trust_dialog")
    fake_tmux = """
tmux() {
    printf '%s\\n' "$*"
}
"""
    accepted = _run_bash(
        helper + fake_tmux
        + '\ncodex_accept_trust_dialog Child 1 10 test-prefix\n'
    )
    assert accepted.returncode == 0
    assert accepted.stdout.strip() == "send-keys -t Child C-m"
    assert "(1/10)" in accepted.stderr

    exhausted = _run_bash(
        helper + fake_tmux
        + '\ncodex_accept_trust_dialog Child 11 10 test-prefix\n'
    )
    assert exhausted.returncode != 0
    assert exhausted.stdout == ""
    assert "persisted after 10 attempts" in exhausted.stderr

    text = _SPAWN.read_text(encoding="utf-8")
    assert text.count('TRUST_MAX=10') == 2
    # Two Codex paths plus the corresponding Claude trust-gate paths.
    assert text.count('TRUST_FAILED=true') == 4
    assert text.count('codex_accept_trust_dialog \\') == 2


def test_claude_fresh_directory_trust_gate_is_not_mistaken_for_readiness():
    ready = _extract("claude_pane_ready")
    trust = _extract("claude_accept_trust_dialog")

    gated = _run_bash(
        ready + '\nclaude_pane_ready "$PANE"\n',
        {"PANE": "Do you trust the files in this folder?\n  Yes\n  No"},
    )
    assert gated.returncode != 0

    prompt = _run_bash(
        ready + '\nclaude_pane_ready "$PANE"\n',
        {"PANE": "Claude Code\n\n❯ "},
    )
    assert prompt.returncode == 0

    accepted = _run_bash(
        trust + "\ntmux() { printf '%s\\n' \"$*\"; }\n"
        + "\nclaude_accept_trust_dialog Child 1 5 test-prefix\n"
    )
    assert accepted.returncode == 0
    assert accepted.stdout.strip() == "send-keys -t Child C-m"
    assert "Claude trust dialog detected" in accepted.stderr


def test_readiness_timeouts_fail_instead_of_injecting_into_unknown_ui():
    text = _SPAWN.read_text(encoding="utf-8")
    assert "injecting prompt anyway" not in text
    assert text.count("refusing to inject the task into an unknown screen state") == 4
    assert text.count("claude_accept_trust_dialog") == 3  # definition + 2 paths


def test_prompt_injection_is_verified_in_every_launch_path():
    text = _SPAWN.read_text(encoding="utf-8")
    verifier = _extract("verify_injection")

    assert text.count('verify_injection "$CHILD_NAME"') == 4
    assert text.count('flush_queued_prompt "$CHILD_NAME"') == 2
    assert "capture-pane" in verifier and "-S -1000" in verifier
    assert "kill-session" not in verifier


def test_injection_verifier_uses_scrollback_and_warns_without_killing():
    spawn_note = _extract("spawn_note")
    verifier = _extract("verify_injection")
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        incident_log = tmpdir / "spawn-incidents.log"
        tmux_log = tmpdir / "tmux.log"
        common = f"""
SPAWN_INCIDENT_LOG={str(incident_log)!r}
INJECTION_VERIFIED=false
{spawn_note}
{verifier}
sleep() {{ :; }}
"""
        delivered = _run_bash(
            common
            + f"""
tmux() {{
    printf '%s\\n' "$*" >> {str(tmux_log)!r}
    printf '%s\\n' 'Canonical task' 'begins with details'
}}
verify_injection Child 'Canonical task begins with details'
printf '%s\\n' "$INJECTION_VERIFIED"
"""
        )
        assert delivered.returncode == 0, delivered.stderr
        assert delivered.stdout.strip() == "true"
        assert "injected ok (Child)" in incident_log.read_text(encoding="utf-8")
        assert "-S -1000" in tmux_log.read_text(encoding="utf-8")

        incident_log.unlink()
        tmux_log.unlink()
        missing = _run_bash(
            common
            + f"""
tmux() {{ printf '%s\\n' "$*" >> {str(tmux_log)!r}; }}
status=0
verify_injection Child 'Task text that never arrived' || status=$?
printf '%s\\n' "$status"
"""
        )
        assert missing.returncode == 0, missing.stderr
        assert missing.stdout.strip() == "1"
        assert "injection FAILED (Child)" in incident_log.read_text(encoding="utf-8")
        assert "kill-session" not in tmux_log.read_text(encoding="utf-8")


def test_queued_claude_prompt_is_flushed_with_an_empty_submit():
    flush = _extract("flush_queued_prompt")
    spawn_note = _extract("spawn_note")
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        flushed = tmpdir / "flushed"
        tmux_log = tmpdir / "tmux.log"
        result = _run_bash(
            f"""
SPAWN_INCIDENT_LOG={str(tmpdir / 'spawn-incidents.log')!r}
{spawn_note}
{flush}
sleep() {{ :; }}
tmux() {{
    printf '%s\\n' "$*" >> {str(tmux_log)!r}
    if [[ "$1" == capture-pane ]]; then
        [[ -f {str(flushed)!r} ]] || printf '%s\\n' 'Press up to edit queued messages'
    elif [[ "$1" == send-keys ]]; then
        : > {str(flushed)!r}
    fi
}}
flush_queued_prompt Child
"""
        )
        assert result.returncode == 0, result.stderr
        calls = tmux_log.read_text(encoding="utf-8")
        assert "send-keys -t Child C-m" in calls
        assert flushed.exists()


def test_optional_terminal_open_is_detached_from_spawn_completion():
    text = _SPAWN.read_text(encoding="utf-8")
    helper = _extract("open_child_terminal")
    assert '(_open_child_terminal "$1") </dev/null >/dev/null 2>&1 &' in helper
    assert "optional observer side effect" in text


def test_preregistered_standalone_contract_is_parentless_and_direct_prompted():
    text = _SPAWN.read_text(encoding="utf-8")
    prereg = text[text.index("# --- Pre-registered mode ---"):
                  text.index("# --- Argument validation ---")]

    assert 'STANDALONE=false' in text
    assert '--standalone requires --pre-registered' in text
    assert 'if [[ "$STANDALONE" != true ]]; then' in prereg
    assert 'TMUX_ENV_ARGS+=(-e "PARENT_AGENT=$PARENT_NAME")' in prereg
    assert "a standalone agent with no parent" in prereg
    assert prereg.count("${TASK}") >= 2
    assert prereg.count("printf '\\033[200~'") == 2


def test_child_window_opens_in_the_background_by_default():
    text = _SPAWN.read_text(encoding="utf-8")
    assert "open -na Ghostty.app" not in text, "child window still steals focus"
    assert "open ${open_bg[@]+\"${open_bg[@]}\"} -na Ghostty.app" in text
    assert 'AGENTSTACK_FOCUS_CHILD' in text
    # The AppleScript adapters must not unconditionally activate either:
    # 'activate' is now interpolated from a variable that stays empty unless
    # the user opted into focus.
    for var in ("iterm_activate", "terminal_activate"):
        assert f'local {var}=""' in text, var
        assert f'{var}="activate"' in text, var
        assert f'\'"${var}"\'' in text, var


def _main() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print("\n" + ("ALL PASSED" if not failures else f"{failures} FAILED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
