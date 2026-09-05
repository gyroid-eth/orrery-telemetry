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
    script = _extract("pane_nonblank_tail") + "\n" + _extract("codex_pane_ready") + '\ncodex_pane_ready "$PANE"\n'
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


def _flags_with_env(help_text: str | None, env: dict[str, str]) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        run_env = {"HOME": tmp, **env}
        if help_text is None:
            # No codex anywhere: an empty PATH plus an empty ~/.local/bin.
            run_env["PATH"] = "/usr/bin:/bin"
        else:
            _codex_stub(tmpdir, help_text)
            run_env["PATH"] = f"{tmpdir}:/usr/bin:/bin"
        script = _extract("codex_approval_flags") + "\ncodex_approval_flags\n"
        return _run_bash(script, run_env).stdout.strip()


_MODERN_HELP = "  -s, --sandbox <MODE>\n      --ask-for-approval <POLICY>"


def test_approval_policy_comes_from_the_installer_setting():
    assert _flags_with_env(_MODERN_HELP, {"AGENTSTACK_CODEX_CHILD_APPROVAL": "on-request"}) == \
        "--ask-for-approval on-request"
    # Empty setting is the product default, not "let codex decide".
    assert _flags_with_env(_MODERN_HELP, {"AGENTSTACK_CODEX_CHILD_APPROVAL": ""}) == \
        "--ask-for-approval never"


def test_missing_codex_on_the_spawner_path_still_pins_the_policy():
    # The dashboard runs under launchd's minimal PATH. Probing `codex --help`
    # there used to yield an empty string, and the child silently fell back to
    # Codex's own on-request default (2026-09-04).
    assert _flags_with_env(None, {}) == "--ask-for-approval never"


def test_explicit_codex_bin_is_probed_instead_of_path():
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        _codex_stub(tmpdir, "  -s, --sandbox <MODE>\n      --full-auto")
        script = _extract("codex_approval_flags") + "\ncodex_approval_flags\n"
        out = _run_bash(script, {"PATH": "/usr/bin:/bin", "HOME": tmp,
                                 "AGENTSTACK_CODEX_BIN": str(tmpdir / "codex")}).stdout.strip()
    assert out == "--full-auto"


def test_network_flag_defaults_on_and_honours_off():
    script = _extract("codex_network_flags") + "\ncodex_network_flags\n"
    assert _run_bash(script, {}).stdout.strip() == \
        "-c sandbox_workspace_write.network_access=true"
    for off in ("off", "0", "false"):
        assert _run_bash(script, {"AGENTSTACK_CODEX_NETWORK": off}).stdout.strip() == ""


def test_child_add_dirs_cover_project_presets_roots_and_operator_extras():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        project = root / "proj with space"
        preset = root / "code"
        typeahead_root = root / "roots"
        extra = root / "extra"
        child_home = root / "child.codex-home"
        for d in (project, preset, typeahead_root, extra, child_home,
                  root / ".claude", root / ".codex", root / ".agentstack"):
            d.mkdir()
        script = (
            f"HOME={shlex.quote(tmp)}\n"
            f"PROJECT_KEY={shlex.quote(str(project))}\n"
            "WORKTREE_BASE=/nonexistent/cc-worktrees\n"
            f"AGENTSTACK_HOME_DIR={shlex.quote(str(root / '.agentstack'))}\n"
            + _extract("codex_child_add_dirs")
            + f"\ncodex_child_add_dirs {shlex.quote(str(child_home))}\n"
        )
        env = {
            "HOME": tmp,
            "AGENTSTACK_SPAWN_DIRS": f"~/code:{project}:/does/not/exist",
            "AGENTSTACK_SPAWN_ROOTS": str(typeahead_root),
            "AGENTSTACK_CODEX_ADD_DIRS": str(extra),
        }
        out = _run_bash(script, env).stdout.strip()
    got = out.split(":")
    real = lambda p: os.path.realpath(str(p))  # noqa: E731
    assert got == [real(project), real(preset), real(typeahead_root),
                   real(root / ".agentstack"), real(root / ".claude"),
                   real(root / ".codex"), real(child_home), real(extra)]
    # Missing entries are dropped, and the project appears once even though
    # it is also listed as a preset.
    assert "/does/not/exist" not in out


def test_launcher_owns_the_codex_flags_and_never_hands_off_to_a_user_launcher():
    text = _SPAWN.read_text(encoding="utf-8")
    assert "launch_codex_workspace.sh" not in text.replace(
        "a user-side launcher (~/.codex/bin/...)", ""
    ), "spawn_child.sh still defers to the user's ~/.codex/bin launcher"
    # Both launch paths apply network flags and the resolved writable roots.
    assert text.count("${=AGENTSTACK_CODEX_APPROVAL} ${=AGENTSTACK_CODEX_NETWORK_FLAGS}") == 2
    assert text.count("${(s.:.)AGENTSTACK_CODEX_ADD_DIRS_RESOLVED}") == 2
    assert text.count('-e "AGENTSTACK_CODEX_ADD_DIRS_RESOLVED=$(codex_child_add_dirs "$CHILD_CODEX_HOME")"') == 2


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
        ("normalize_claude_model", "fable"): "claude-fable-5-1",
        ("normalize_claude_model", "claude-fable-5"): "claude-fable-5-1",
        ("normalize_codex_model", ""): "gpt-5.6-sol",
        ("normalize_codex_model", "sol"): "gpt-5.6-sol",
        ("normalize_codex_model", "terra"): "gpt-5.6-terra",
        ("normalize_codex_model", "luna"): "gpt-5.6-luna",
        ("normalize_codex_model", "astra"): "gpt-6-astra",
        ("normalize_codex_model", "gpt-6-astra"): "gpt-6-astra",
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
    ready = _extract("pane_nonblank_tail") + "\n" + _extract("pane_normalize_nbsp") + "\n" + _extract("claude_trust_dialog_present") + "\n" + _extract("claude_pane_ready")
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


# --- 2026-09-03: trust dialogs and padded panes -------------------------------

_CLAUDE_SAFETY_CHECK = (
    " Accessing workspace:\n /Users/example/code/project\n"
    " Quick safety check: Is this a project you created or one you trust? (Like your\n"
    " own code, a well-known open source project, or work from your team). If not,\n"
    " take a moment to review what's in this folder first.\n"
    " Claude Code'll be able to read, edit, and execute files here.\n"
    " Security guide\n ❯ No, exit\n   Yes, I trust this folder\n"
    " Enter to confirm · Esc to cancel\n" + "\n" * 12
)
_CLAUDE_SAFETY_CHECK_YES_SELECTED = _CLAUDE_SAFETY_CHECK.replace(
    " ❯ No, exit\n   Yes, I trust this folder", "   No, exit\n ❯ Yes, I trust this folder"
)
_CODEX_FOOTER_PADDED = (
    "› Ask Codex to do anything\n\n"
    "  gpt-5.6-sol low · Context 100% left · ~/code/project · weekly 74% left\n"
    + "\n" * 20
)
_CODEX_TRUST_IN_SCROLLBACK = (
    "> You are in /Users/example/code/project\n"
    "  Do you trust the contents of this directory? Working with untrusted contents\n"
    "› 1. Yes, continue\n  2. No, quit\n  Press enter to continue\n"
    + "\n" * 8
    + _CODEX_FOOTER_PADDED
)


def _helpers() -> str:
    return "\n".join(
        _extract(name)
        for name in (
            "pane_nonblank_tail",
            "pane_normalize_nbsp",
            "codex_trust_dialog_present",
            "claude_trust_dialog_present",
        )
    )


def test_claude_safety_check_dialog_is_never_readiness():
    ready = _helpers() + "\n" + _extract("claude_pane_ready")
    # The selected row also starts with the cursor glyph ("❯ No, exit"); the
    # old bare-prompt regex read that as an empty input row and typed the task
    # into the dialog, and the following Enter chose "No, exit".
    assert _run_bash(ready + '\nclaude_pane_ready "$PANE"\n', {"PANE": _CLAUDE_SAFETY_CHECK}).returncode != 0
    assert _run_bash(ready + '\nclaude_pane_ready "$PANE"\n', {"PANE": _CLAUDE_SAFETY_CHECK_YES_SELECTED}).returncode != 0
    assert _run_bash(ready + '\nclaude_pane_ready "$PANE"\n', {"PANE": "Claude Code\n\n❯ \n" + "\n" * 30}).returncode == 0


def test_claude_safety_check_detector_sees_both_wordings():
    detector = _helpers()
    for pane in (_CLAUDE_SAFETY_CHECK, "Do you trust the files in this folder?\n  Yes\n  No\n"):
        assert _run_bash(detector + '\nclaude_trust_dialog_present "$PANE"\n', {"PANE": pane}).returncode == 0
    assert _run_bash(detector + '\nclaude_trust_dialog_present "$PANE"\n', {"PANE": "Claude Code\n\n❯ \n"}).returncode != 0


def _accept_with_screens(screens: list[str]) -> str:
    """Run claude_accept_trust_dialog against a tmux stub that returns the given
    capture-pane screens in order and records every send-keys."""
    stub_lines = [
        # capture-pane runs inside $(...), so the cursor lives in a file rather
        # than a shell variable that a subshell would lose.
        'SCREEN_IDX_FILE="$(mktemp)"; printf 0 > "$SCREEN_IDX_FILE"',
        "tmux() {",
        '  case "$1" in',
        "    capture-pane)",
        '      local idx; idx="$(cat "$SCREEN_IDX_FILE")"',
        '      printf \'%s\' "${SCREENS[$idx]}"',
        '      if (( idx + 1 < ${#SCREENS[@]} )); then printf %s "$((idx + 1))" > "$SCREEN_IDX_FILE"; fi',
        "      ;;",
        "    send-keys)",
        '      printf \'KEYS:%s\\n\' "$*"',
        "      ;;",
        "  esac",
        "}",
        "sleep() { :; }",
    ]
    env = {f"SCREEN_{i}": screen for i, screen in enumerate(screens)}
    script = (
        "SCREENS=(" + " ".join(f'"$SCREEN_{i}"' for i in range(len(screens))) + ")\n"
        + "\n".join(stub_lines)
        + "\n"
        + _extract("pane_normalize_nbsp")
        + "\n"
        + _extract("claude_accept_trust_dialog")
        + "\nclaude_accept_trust_dialog Child 1 5 test-prefix\n"
    )
    return _run_bash(script, env).stdout


def test_claude_safety_check_moves_to_yes_before_confirming():
    out = _accept_with_screens([_CLAUDE_SAFETY_CHECK, _CLAUDE_SAFETY_CHECK_YES_SELECTED])
    keys = [line for line in out.splitlines() if line.startswith("KEYS:")]
    assert keys == ["KEYS:send-keys -t Child Down", "KEYS:send-keys -t Child C-m"]


def test_claude_safety_check_confirms_directly_when_yes_is_selected():
    out = _accept_with_screens([_CLAUDE_SAFETY_CHECK_YES_SELECTED])
    keys = [line for line in out.splitlines() if line.startswith("KEYS:")]
    assert keys == ["KEYS:send-keys -t Child C-m"]


def test_claude_safety_check_never_presses_enter_on_no_exit():
    # Down did not move the cursor (screen unchanged): refuse to confirm.
    out = _accept_with_screens([_CLAUDE_SAFETY_CHECK, _CLAUDE_SAFETY_CHECK])
    keys = [line for line in out.splitlines() if line.startswith("KEYS:")]
    assert keys == ["KEYS:send-keys -t Child Down"]


def test_codex_readiness_survives_a_padded_pane():
    ready = _helpers() + "\n" + _extract("codex_pane_ready")
    assert _run_bash(ready + '\ncodex_pane_ready "$PANE"\n', {"PANE": _CODEX_FOOTER_PADDED}).returncode == 0


def test_codex_polls_capture_the_visible_screen_only():
    # A capture with scrollback kept an already-accepted trust dialog in view,
    # so the launcher pressed Enter on every poll and never reached readiness.
    text = _SPAWN.read_text(encoding="utf-8")
    assert 'capture-pane -t "$CHILD_NAME" -p -S -30' not in text
    assert text.count('PANE_TEXT=$(tmux capture-pane -t "$CHILD_NAME" -p 2>/dev/null || true)') == 4


def test_child_shell_never_sources_a_user_bootstrap():
    text = _SPAWN.read_text(encoding="utf-8")
    assert "codex_agent_bootstrap" not in text


def test_child_proxy_configs_carry_the_bearer_mode():
    # Claude and Codex start the proxy with the config's env table only, so
    # the mode must be written there or the proxy defaults to "auto" and
    # exits on a bearer-disabled service env before answering initialize.
    text = _SPAWN.read_text(encoding="utf-8")
    assert "AGENTSTACK_MAIL_HTTP_BEARER_MODE=bearer_mode," in text
    assert 'lines.append("AGENTSTACK_MAIL_HTTP_BEARER_MODE = " + toml_string(bearer_mode))' in text
    run_mcp = (_ROOT / "integrations" / "codex_app" / "plugin" / "scripts" / "run-mcp.sh").read_text(encoding="utf-8")
    assert "read -r MCP_AGENT_MAIL_TOKEN" not in run_mcp
