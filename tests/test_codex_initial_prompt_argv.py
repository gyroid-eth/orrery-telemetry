"""A cold Codex child gets its first task as the positional [PROMPT] argument.

On WSL (2026-09-28) 1 of 8 cold Codex 0.158 children never started a turn
after its task was pasted. A race between the paste and Codex's startup screens
(which can discard pending input) is the leading hypothesis, not a confirmed
cause; the launcher removes the paste instead. It now writes the task to a 0600 file; the child's shell reads it and
runs `codex ... -- "<task>"`. Nothing is pasted, submitted or resent, and the
model / trust / sign-in screens are handled until the task is on screen.

The child snippets are the real ones from hooks/spawn_child.sh, run in
/bin/bash (3.2 on macOS) and zsh against a fake codex that records its argv.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPAWN = ROOT / "hooks" / "spawn_child.sh"

SHELLS = [s for s in ("/bin/bash", shutil.which("zsh")) if s and os.path.exists(s)]


def _child_scripts() -> list[str]:
    """The two Codex cold-start child scripts (pre-registered, legacy)."""
    text = SPAWN.read_text(encoding="utf-8")
    scripts = []
    for match in re.finditer(r"\"\$CHILD_SHELL\"' -lc '\"'\"'\n(.*?)'\"'\"''", text, re.S):
        body = match.group(1)
        if "AGENTSTACK_CODEX_BIN" in body:
            scripts.append(body)
    assert len(scripts) == 2, "expected the pre-registered and the legacy Codex launch"
    return scripts


def _fake_bin(tmp_path: pathlib.Path) -> pathlib.Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    codex = bindir / "codex"
    codex.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['ARGV_OUT'], 'w', encoding='utf-8') as out:\n"
        "    json.dump(sys.argv[1:], out, ensure_ascii=False)\n",
        encoding="utf-8",
    )
    codex.chmod(0o755)
    sleep = bindir / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    sleep.chmod(0o755)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "cleanup-child-agent.sh").write_text("exit 0\n", encoding="utf-8")
    return bindir


def _run_child(tmp_path, shell, script, task_file):
    bindir = _fake_bin(tmp_path)
    argv_out = tmp_path / "argv.json"
    workdir = tmp_path / "work"
    workdir.mkdir()
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "ARGV_OUT": str(argv_out),
        "AGENTSTACK_CODEX_BIN": str(bindir / "codex"),
        "AGENTSTACK_CODEX_MODEL": "gpt-6-sol",
        "AGENTSTACK_CODEX_EFFORT": "low",
        "AGENTSTACK_CODEX_APPROVAL": "--ask-for-approval never",
        "AGENTSTACK_CODEX_NETWORK_FLAGS": "",
        "AGENTSTACK_CODEX_ADD_DIRS_RESOLVED": "",
        "AGENTSTACK_HOOKS_DIR": str(tmp_path / "hooks"),
        "AGENTSTACK_CODEX_PROMPT_FILE": str(task_file),
    }
    result = subprocess.run([shell, "-c", script], cwd=workdir, env=env,
                            capture_output=True, text=True, timeout=20)
    argv = json.loads(argv_out.read_text(encoding="utf-8")) if argv_out.exists() else None
    return result, argv


def _task_file(tmp_path: pathlib.Path, text: str) -> pathlib.Path:
    path = tmp_path / ".Child.prompt.test"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


TASKS = {
    "japanese_multiline": "あなたは Child。\n\n## Role: 実装\n改行と  空白を保つ。\n最後の行",
    "quotes_and_expansions": (
        "Single ' and double \" quotes, a `backtick`, $(touch SHOULD_NOT_EXIST), "
        "${HOME} and $PATH stay literal; \\ backslash; ; && | > redirect"
    ),
    "leading_hyphen": "--version is text here, not a flag",
    "subcommand_word": "exec this as a prompt, not the exec subcommand",
    "long": ("長い task の一行。" * 40 + "\n") * 60,
}


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("which", [0, 1], ids=["preregistered", "legacy"])
@pytest.mark.parametrize("name", list(TASKS))
def test_task_reaches_codex_as_one_argv_unchanged(tmp_path, shell, which, name):
    task = TASKS[name]
    task_file = _task_file(tmp_path, task)
    result, argv = _run_child(tmp_path, shell, _child_scripts()[which], task_file)
    assert result.returncode == 0, result.stderr
    assert argv is not None, result.stderr
    assert argv[-2:] == ["--", task]
    assert argv.count("--") == 1
    assert argv[argv.index("--model") + 1] == "gpt-6-sol"
    # The task was data, never code, and its file was consumed.
    assert not (tmp_path / "work" / "SHOULD_NOT_EXIST").exists()
    assert not task_file.exists()


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("which", [0, 1], ids=["preregistered", "legacy"])
@pytest.mark.parametrize("state", ["missing", "empty"])
def test_codex_never_starts_without_its_task(tmp_path, shell, which, state):
    task_file = tmp_path / ".Child.prompt.test"
    if state == "empty":
        task_file.write_text("", encoding="utf-8")
    result, argv = _run_child(tmp_path, shell, _child_scripts()[which], task_file)
    assert argv is None, "codex must not be started without its task"
    assert result.returncode != 0
    assert "not starting Codex without its task" in result.stderr


# --- launcher side ------------------------------------------------------------

def _extract(func: str) -> str:
    text = SPAWN.read_text(encoding="utf-8")
    start = text.index(f"\n{func}() {{") + 1
    return text[start:text.index("\n}\n", start) + 3]


def _launcher_functions() -> str:
    names = ("pane_nonblank_tail", "pane_normalize_nbsp", "injection_match_key",
             "injection_utf8_locale", "codex_trust_dialog_present", "codex_trust_row_selected",
             "codex_accept_trust_dialog", "codex_task_on_screen", "codex_startup_screen",
             "codex_watch_initial_task")
    return "\n".join(_extract(name) for name in names)


PROMPT = "You are Child, a standalone agent with no parent. Start it immediately:\n\nreply STARTED"
PROVISIONAL = "\n› Ask Codex to do anything\n\n  gpt-6-luna low · ~ · ⠋\n  ? for shortcuts\n"
TRUST = ("  Trust this folder? Codex can read, edit, and run files here.\n\n"
         "› 1. Trust and continue\n  2. Quit\n\n  enter continue · esc quit\n")
STARTED = ("\n› You are Child, a standalone agent with no parent. Start it immediately:\n\n"
           "  reply STARTED\n\n• Working (1s • esc to interrupt)\n\n› Ask Codex to do anything\n")
ANSWERED = STARTED.replace("• Working (1s • esc to interrupt)", "• STARTED")


def _watch(tmp_path, screens: list[str], alive=True, prompt=PROMPT):
    """Run codex_watch_initial_task against a tmux stub replaying `screens`
    (the last one repeats); record every tmux call."""
    for i, screen in enumerate(screens):
        (tmp_path / f"screen{i}").write_text(screen, encoding="utf-8")
    calls = tmp_path / "calls"
    script = (
        # The screen moves on only at a poll boundary (`sleep 3`), so the extra
        # captures a handler makes within one poll see the same screen.
        f"SCREENS={len(screens)}; DIR={shlex.quote(str(tmp_path))}; printf -- -1 > \"$DIR/idx\"\n"
        "tmux() {\n"
        '  printf "%s\\n" "$*" >> "$DIR/calls"\n'
        '  case "$1" in\n'
        "    capture-pane)\n"
        '      local idx; idx="$(cat "$DIR/idx")"\n'
        '      if [[ -f "$DIR/advance" ]] && (( idx + 1 < SCREENS )); then idx=$((idx + 1)); printf %s "$idx" > "$DIR/idx"; fi\n'
        '      rm -f "$DIR/advance"; cat "$DIR/screen$idx" ;;\n'
        "    has-session) " + ("return 0" if alive else "return 1") + " ;;\n"
        "  esac\n"
        "}\n"
        'sleep() { [[ "$1" == 3 ]] && : > "$DIR/advance"; return 0; }\n'
        f'spawn_note() {{ printf "NOTE:%s\\n" "$1" >> {shlex.quote(str(tmp_path / "notes"))}; }}\n'
        "codex_session_alive() { tmux has-session -t \"=$1\"; }\n"
        "INJECTION_VERIFIED=false\n"
        + _launcher_functions()
        + '\nstatus=0; codex_watch_initial_task Child "$PROMPT" test || status=$?\n'
        'printf "STATUS=%s VERIFIED=%s\\n" "$status" "$INJECTION_VERIFIED"\n'
    )
    result = subprocess.run(["/bin/bash", "-c", script], env=dict(os.environ, PROMPT=prompt),
                            capture_output=True, text=True, timeout=60)
    keys = [line for line in calls.read_text().splitlines() if not line.startswith(("capture-pane", "has-session"))]
    notes = (tmp_path / "notes").read_text() if (tmp_path / "notes").exists() else ""
    return result, keys, notes


def test_trust_after_a_provisional_composer_is_handled_before_the_task_counts(tmp_path):
    result, keys, notes = _watch(tmp_path, [PROVISIONAL, TRUST, PROVISIONAL, STARTED])
    # The provisional footer did not end the watch; only the trust row got C-m.
    assert "STATUS=0 VERIFIED=true" in result.stdout, result.stderr
    assert keys == ["send-keys -t Child C-m"]
    assert "task started from argv" in notes


def test_a_task_seen_once_before_a_trust_screen_does_not_end_the_watch(tmp_path):
    result, keys, _ = _watch(tmp_path, [STARTED, TRUST, STARTED])
    assert "STATUS=0" in result.stdout
    assert keys == ["send-keys -t Child C-m"]


def test_an_already_answered_task_is_observed(tmp_path):
    result, keys, _ = _watch(tmp_path, [ANSWERED])
    assert "STATUS=0 VERIFIED=true" in result.stdout
    assert keys == []


def test_a_task_never_seen_is_a_diagnostic_not_a_resend_or_a_kill(tmp_path):
    result, keys, notes = _watch(tmp_path, [PROVISIONAL])
    assert "STATUS=3 VERIFIED=false" in result.stdout
    assert keys == [], "nothing may be typed, pasted, resent or killed"
    assert "task not seen on screen (Child)" in notes
    assert "is not resent; the child is left running" in notes


def test_a_dead_child_is_reported_apart_from_an_unseen_task(tmp_path):
    result, keys, notes = _watch(tmp_path, ["error: unexpected argument\n"], alive=False)
    assert "STATUS=2" in result.stdout
    assert "died after" in result.stderr
    assert keys == []
    assert "task not seen" not in notes


def test_the_launcher_never_pastes_or_submits_a_cold_codex_task():
    text = SPAWN.read_text(encoding="utf-8")
    for start, end in (("# --- Pre-registered mode ---", "# --- Argument validation ---"),):
        section = text[text.index(start):text.index(end)]
        codex = section[section.index('if [[ "$USE_CODEX" == true ]]; then'):section.index("# Claude Code startup (--pre-registered mode).")]
        assert "send_prompt_to_pane" not in codex and "verify_injection" not in codex
    legacy = text[text.rindex('CODEX_PROMPT="$(build_codex_mail_task_prompt'):text.rindex("# Claude Code 起動")]
    assert "send_prompt_to_pane" not in legacy and "verify_injection" not in legacy
    assert text.count('codex_watch_initial_task "$CHILD_NAME" "$CODEX_PROMPT"') == 2
    assert text.count('TMUX_ENV_ARGS+=(-e "AGENTSTACK_CODEX_PROMPT_FILE=$CODEX_PROMPT_FILE")') == 2


def test_prompt_file_is_private_and_oversized_tasks_fail_visibly(tmp_path):
    body = (
        f"CHILD_STATE_DIR={shlex.quote(str(tmp_path / 'state'))}\n"
        + _extract("write_codex_prompt_file")
        + "\nCODEX_PROMPT_MAX_BYTES=64\n"
        + 'f="$(write_codex_prompt_file Child "short task")"; echo "FILE=$f"\n'
        + 'write_codex_prompt_file Child "$(printf "x%.0s" $(seq 1 65))" && echo BIG_OK || echo BIG_FAIL\n'
    )
    result = subprocess.run(["/bin/bash", "-c", body], capture_output=True, text=True, timeout=10)
    path = pathlib.Path(result.stdout.split("FILE=")[1].splitlines()[0])
    assert path.read_text() == "short task"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct((tmp_path / "state").stat().st_mode & 0o777) == "0o700"
    assert "BIG_FAIL" in result.stdout
    assert "passed as one command-line argument" in result.stderr


# --- review 2026-09-29: a phrase quoted in the task or the answer is no dialog ---

def _quoting(phrase: str) -> str:
    return STARTED.replace("  reply STARTED\n", f"  reply STARTED; explain the phrase {phrase}\n")


@pytest.mark.parametrize("phrase", [
    "Do you trust this directory?", "Use existing model", "Press enter to continue",
])
def test_a_dialog_phrase_quoted_in_the_task_sends_no_keys(tmp_path, phrase):
    quoted_prompt = PROMPT.replace("reply STARTED", f"reply STARTED; explain the phrase {phrase}")
    result, keys, _ = _watch(tmp_path, [_quoting(phrase)], prompt=quoted_prompt)
    assert "STATUS=0 VERIFIED=true" in result.stdout, result.stdout + result.stderr
    assert keys == []


@pytest.mark.parametrize("phrase", [
    "Do you trust the contents of this directory?", "Use existing model", "Press enter to continue",
])
def test_a_dialog_phrase_quoted_in_a_sentence_of_the_answer_sends_no_keys(tmp_path, phrase):
    answer = STARTED.replace("• Working (1s • esc to interrupt)",
                             f"• The phrase \"{phrase}\" appears in dialogs.")
    result, keys, _ = _watch(tmp_path, [answer])
    assert "STATUS=0" in result.stdout
    assert keys == []


@pytest.mark.parametrize("phrase", [
    "Do you trust the contents of this directory?", "Use existing model", "Press enter to continue",
    "1. Trust and continue",
])
def test_a_dialog_phrase_on_its_own_line_of_the_answer_sends_no_keys(tmp_path, phrase):
    # The answer puts the phrase on a line of its own, exactly as a dialog
    # would; it is still transcript, above the composer.
    answer = STARTED.replace("• Working (1s • esc to interrupt)",
                             f"• The menu label is:\n\n  {phrase}\n\n• Working (2s • esc to interrupt)")
    result, keys, _ = _watch(tmp_path, [answer])
    assert "STATUS=0 VERIFIED=true" in result.stdout
    assert keys == []


def test_a_trust_dialog_below_a_provisional_composer_is_answered(tmp_path):
    # Whatever is drawn after the last composer marker is a candidate dialog.
    result, keys, _ = _watch(tmp_path, [PROVISIONAL + "\n" + TRUST, STARTED])
    assert "STATUS=0" in result.stdout
    assert keys == ["send-keys -t Child C-m"]


def test_a_real_trust_dialog_over_the_task_is_still_answered(tmp_path):
    # The task is already drawn behind a real dialog: the option rows count.
    over = STARTED.replace("• Working (1s • esc to interrupt)\n\n› Ask Codex to do anything\n", TRUST)
    result, keys, _ = _watch(tmp_path, [over, STARTED])
    assert "STATUS=0" in result.stdout
    assert keys == ["send-keys -t Child C-m"]


def test_real_model_and_signin_screens_are_answered(tmp_path):
    model = "  Choose a model\n\n› 1. Use existing model\n  2. Upgrade\n"
    signin = "  Signed in as someone\n\n  Press enter to continue\n"
    result, keys, _ = _watch(tmp_path, [model, signin, STARTED])
    assert "STATUS=0" in result.stdout
    assert keys == ["send-keys -t Child Down Enter", "send-keys -t Child Enter"]


def test_a_legacy_trust_dialog_is_answered(tmp_path):
    legacy = ("> You are in /home/example\n  Do you trust the contents of this directory? Working with untrusted contents\n"
              "› 1. Yes, continue\n  2. No, quit\n  Press enter to continue\n")
    result, keys, _ = _watch(tmp_path, [legacy, STARTED])
    assert "STATUS=0" in result.stdout
    assert keys == ["send-keys -t Child C-m"]


def test_a_task_on_the_screen_of_a_dead_session_is_not_a_success(tmp_path):
    result, keys, _ = _watch(tmp_path, [STARTED], alive=False)
    assert "STATUS=2" in result.stdout
    assert keys == []
