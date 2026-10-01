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
    names = ("pane_nonblank_tail", "pane_normalize_nbsp", "codex_trust_row_selected",
             "codex_accept_trust_dialog", "codex_trust_screen_up", "codex_turn_running_on_screen",
             "injection_utf8_locale", "codex_task_pinned_on_screen", "codex_turn_finished_on_screen",
             "codex_watch_initial_task")
    return "\n".join(_extract(name) for name in names)


PROMPT = "You are Child, a standalone agent with no parent. Start it immediately:\n\nreply STARTED"
PROVISIONAL = "\n› Ask Codex to do anything\n\n  gpt-6-luna low · ~ · ⠋\n  ? for shortcuts\n"
# The trust screen of Codex 0.158 as captured on WSL (2026-09-28).
TRUST = (
    "\n  Folder access\n  /home/example\n\n"
    "  Trust this folder? Codex can read, edit, and run files here, subject to your\n"
    "  permission settings. Folder settings can run code automatically, even\n"
    "  without a model request. Continue only if you trust these files. Your trust\n"
    "  decision will be saved.\n\n"
    "› 1. Trust and continue\n  2. Quit\n\n  enter continue · esc quit\n"
)
LEGACY_TRUST = ("> You are in /home/example\n"
                "  Do you trust the contents of this directory? Working with untrusted contents\n"
                "› 1. Yes, continue\n  2. No, quit\n  Press enter to continue\n")
STARTED = ("\n› You are Child, a standalone agent with no parent. Start it immediately:\n\n"
           "  reply STARTED\n\n• Working (1s • esc to interrupt)\n\n› Ask Codex to do anything\n")


def _watch(tmp_path, screens: list[str], statuses: list[str] | None = None, alive=True, prompt=PROMPT):
    """Run codex_watch_initial_task with tmux replaying `screens` and the rollout
    helper replaying `statuses` (each list's last entry repeats)."""
    statuses = statuses or ["unknown"]
    for i, screen in enumerate(screens):
        (tmp_path / f"screen{i}").write_text(screen, encoding="utf-8")
    for i, status in enumerate(statuses):
        (tmp_path / f"status{i}").write_text(status, encoding="utf-8")
    calls = tmp_path / "calls"
    script = (
        # The screen moves on only at a poll boundary (`sleep 3`), so the extra
        # captures a handler makes within one poll see the same screen.
        f"SCREENS={len(screens)}; STATUSES={len(statuses)}; DIR={shlex.quote(str(tmp_path))}\n"
        'printf -- -1 > "$DIR/idx"; printf 0 > "$DIR/sidx"\n'
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
        "codex_initial_task_status() {\n"
        '  local i; i="$(cat "$DIR/sidx")"; cat "$DIR/status$i"\n'
        '  if (( i + 1 < STATUSES )); then printf %s "$((i + 1))" > "$DIR/sidx"; fi\n'
        "}\n"
        f'spawn_note() {{ printf "NOTE:%s\\n" "$1" >> {shlex.quote(str(tmp_path / "notes"))}; }}\n'
        "codex_session_alive() { tmux has-session -t \"=$1\"; }\n"
        "INJECTION_VERIFIED=false\n"
        + _launcher_functions()
        + '\nstatus=0; codex_watch_initial_task Child "$PROMPT" test /launch/1.json abc || status=$?\n'
        'printf "STATUS=%s VERIFIED=%s\\n" "$status" "$INJECTION_VERIFIED"\n'
    )
    result = subprocess.run(["/bin/bash", "-c", script], env=dict(os.environ, PROMPT=prompt),
                            capture_output=True, text=True, timeout=60)
    keys = [line for line in calls.read_text().splitlines() if not line.startswith(("capture-pane", "has-session"))]
    notes = (tmp_path / "notes").read_text() if (tmp_path / "notes").exists() else ""
    return result, keys, notes


def test_the_real_trust_screen_is_answered_and_the_rollout_confirms_the_start(tmp_path):
    result, keys, notes = _watch(tmp_path, [PROVISIONAL, TRUST, STARTED],
                                 ["unknown", "unknown", "unknown", "started"])
    assert "STATUS=0 VERIFIED=true" in result.stdout, result.stderr
    assert keys == ["send-keys -t Child C-m"]
    assert "recorded in this launch's rollout" in notes


def test_without_a_binding_the_start_is_unknown_not_a_failure(tmp_path):
    # The history binding is optional: a late trust screen is still answered,
    # and the watch ends saying the start could not be confirmed.
    result, keys, notes = _watch(tmp_path, [PROVISIONAL, PROVISIONAL, TRUST, STARTED])
    assert "STATUS=3 VERIFIED=false" in result.stdout
    assert keys == ["send-keys -t Child C-m"]
    assert "Codex started (Child); a running first turn was seen on screen" in notes
    assert "First-task confirmation unknown" in notes
    assert "WARNING" not in notes


def _waited(notes: str) -> int:
    import re

    return int(re.search(r"(?:after|before) (\d+)s", notes).group(1))


def test_without_a_receipt_a_running_turn_ends_the_wait_early(tmp_path):
    # Where the history binding is not installed, every Codex spawn used to
    # wait the full 90 s bound (WSL, 2026-09-29). A visibly running turn ends
    # it one poll later; still no key and no success claimed.
    result, keys, notes = _watch(tmp_path, [PROVISIONAL, STARTED])
    assert "STATUS=3 VERIFIED=false" in result.stdout
    assert keys == []
    assert _waited(notes) <= 12


def test_without_a_receipt_and_no_running_turn_the_full_bound_still_applies(tmp_path):
    result, keys, notes = _watch(tmp_path, [PROVISIONAL])
    assert "STATUS=3" in result.stdout
    assert "Codex started (Child); first-task confirmation unknown" in notes


def test_a_bound_session_waits_for_its_rollout_even_with_a_running_turn(tmp_path):
    result, keys, notes = _watch(tmp_path, [STARTED], ["bound"])
    assert "STATUS=3" in result.stdout and keys == []
    assert "WARNING: first task not yet recorded (Child)" in notes


def test_the_rollout_still_confirms_when_it_arrives_with_the_turn(tmp_path):
    result, keys, notes = _watch(tmp_path, [STARTED], ["unknown", "started"])
    assert "STATUS=0 VERIFIED=true" in result.stdout
    assert "recorded in this launch's rollout" in notes


def test_the_running_turn_line_must_be_at_the_bottom():
    body = (_extract("pane_nonblank_tail") + _extract("pane_normalize_nbsp")
            + _extract("codex_turn_running_on_screen") + '\ncodex_turn_running_on_screen "$SCREEN"\n')

    def check(screen):
        return subprocess.run(["/bin/bash", "-c", body], env=dict(os.environ, SCREEN=screen),
                              capture_output=True, text=True, timeout=10).returncode

    assert check(STARTED) == 0
    assert check(STARTED.replace("(1s •", "(1m 05s •")) == 0
    assert check(PROVISIONAL) != 0
    assert check(TRUST) != 0
    # Quoted high up in a long conversation, with an idle composer below.
    quoted = "• Working (1s • esc to interrupt)\n" + "\n".join(f"line {i}" for i in range(12)) + PROVISIONAL
    assert check(quoted) != 0


def test_a_task_on_screen_is_not_a_success_without_the_rollout(tmp_path):
    result, keys, _ = _watch(tmp_path, [STARTED])
    assert "STATUS=3 VERIFIED=false" in result.stdout
    assert keys == []


def test_a_bound_session_gets_no_more_keys(tmp_path):
    # Once this launch's receipt is verified, even a trust-looking screen gets
    # no keys; the watch waits for the rollout only.
    result, keys, notes = _watch(tmp_path, [TRUST], ["bound"])
    assert "STATUS=3" in result.stdout
    assert keys == []
    assert "WARNING: first task not yet recorded (Child)" in notes


def test_a_dead_child_is_reported_apart_from_an_unconfirmed_start(tmp_path):
    result, keys, notes = _watch(tmp_path, ["error: unexpected argument\n"], alive=False)
    assert "STATUS=2" in result.stdout
    assert "died after" in result.stderr
    assert keys == []
    assert notes == ""


def test_a_legacy_trust_screen_in_its_full_form_is_answered(tmp_path):
    result, keys, _ = _watch(tmp_path, [LEGACY_TRUST, STARTED], ["unknown", "unknown", "started"])
    assert "STATUS=0" in result.stdout
    assert keys == ["send-keys -t Child C-m"]


@pytest.mark.parametrize("screen", [
    "  Choose a model\n\n› 1. Use existing model\n  2. Upgrade\n",
    "  Signed in as someone\n\n  Press enter to continue\n",
], ids=["model", "signin"])
def test_unverified_model_and_signin_layouts_get_no_keys(tmp_path, screen):
    result, keys, _ = _watch(tmp_path, [screen])
    assert "STATUS=3" in result.stdout
    assert keys == []


def _with_answer(lines: str) -> str:
    return STARTED.replace("• Working (1s • esc to interrupt)", lines + "\n\n• Working (2s • esc to interrupt)")


@pytest.mark.parametrize("screen", [
    _with_answer("• The menu label is:\n\n  Do you trust the contents of this directory?"),
    _with_answer("• The menu label is:\n\n  Use existing model"),
    _with_answer("• The menu label is:\n\n  Press enter to continue"),
    _with_answer("• It reads:\n\n  1. Trust and continue\n  2. Quit\n\n  enter continue · esc quit"),
    # The task itself quotes the whole trust screen, above the composer.
    STARTED.replace("  reply STARTED\n", "  reply STARTED; the screen was:\n" + TRUST),
], ids=["legacy-question", "model", "signin", "trust-block-in-answer", "trust-screen-in-task"])
def test_dialog_text_in_the_conversation_gets_no_keys(tmp_path, screen):
    result, keys, _ = _watch(tmp_path, [screen])
    assert keys == [], "conversation text must never be answered as a dialog"
    assert "STATUS=1" not in result.stdout


def test_a_real_trust_screen_below_a_task_that_quotes_one_is_answered(tmp_path):
    # Task text quoting the dialog is above; the real dialog is at the bottom.
    quoting = "› reply STARTED; the screen was: 1. Trust and continue / 2. Quit\n"
    result, keys, _ = _watch(tmp_path, [quoting + TRUST, STARTED], ["unknown", "unknown", "started"])
    assert "STATUS=0" in result.stdout
    assert keys == ["send-keys -t Child C-m"]


def test_the_trust_detector_accepts_the_captured_wsl_frame():
    frame = pathlib.Path(__file__).with_name("fixtures") / "codex-0.158-trust-frame.txt"
    body = (_extract("pane_nonblank_tail") + _extract("pane_normalize_nbsp") + _extract("codex_trust_screen_up")
            + '\ncodex_trust_screen_up "$(cat "$FRAME")"\n')
    result = subprocess.run(["/bin/bash", "-c", body], env=dict(os.environ, FRAME=str(frame)),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


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


# --- real Codex 0.158 frames captured on WSL (2026-09-28) ---------------------
FIXTURES = pathlib.Path(__file__).with_name("fixtures")
RUNNING = (FIXTURES / "codex-0.158-running-frame.txt").read_text(encoding="utf-8")
FINISHED = (FIXTURES / "codex-0.158-finished-frame.txt").read_text(encoding="utf-8")
FINISHED_TASK = ("You are OrangeMendeleev, a standalone agent with no parent. Start it immediately:\n\n"
                 "最終確認2です。STARTED とだけ答えて待機してください。")


def test_the_real_running_frame_ends_the_wait_early(tmp_path):
    # The status line is 7 lines above the bottom, blank lines included.
    result, keys, notes = _watch(tmp_path, [RUNNING])
    assert "STATUS=3 VERIFIED=false" in result.stdout and keys == []
    assert "a running first turn was seen on screen" in notes
    assert _waited(notes) <= 12


def test_a_status_line_with_more_after_it_still_counts(tmp_path):
    screen = RUNNING.replace("(1s • esc to interrupt)", "(1s • esc to interrupt) · 2 background terminals")
    result, keys, notes = _watch(tmp_path, [screen])
    assert _waited(notes) <= 12 and keys == []


def test_a_short_turn_already_over_ends_the_wait_at_once(tmp_path):
    # "Reply STARTED" is done before the next poll: reply, time, idle composer.
    result, keys, notes = _watch(tmp_path, [FINISHED], prompt=FINISHED_TASK)
    assert "STATUS=3 VERIFIED=false" in result.stdout and keys == []
    assert "a reply to its first task is on screen" in notes
    assert _waited(notes) <= 6


def test_a_trust_screen_after_a_running_line_is_answered_first(tmp_path):
    result, keys, notes = _watch(tmp_path, [RUNNING, TRUST, PROVISIONAL])
    assert keys == ["send-keys -t Child C-m"]
    # The trust screen reset the early end: nothing ran after it, so the
    # watch ran to its bound instead of ending on the old running line.
    assert "a running first turn was seen" not in notes
    assert "first-task confirmation unknown" in notes


def _finished(screen: str, prompt: str) -> int:
    body = ("\n".join(_extract(n) for n in ("pane_nonblank_tail", "pane_normalize_nbsp", "codex_trust_row_selected",
                                           "codex_trust_screen_up", "injection_utf8_locale",
                                           "codex_task_pinned_on_screen", "codex_turn_finished_on_screen"))
            + '\ncodex_turn_finished_on_screen "$SCREEN" "$PROMPT"\n')
    return subprocess.run(["/bin/bash", "-c", body], env=dict(os.environ, SCREEN=screen, PROMPT=prompt),
                          capture_output=True, text=True, timeout=10).returncode


def test_the_finished_turn_needs_the_task_a_reply_after_it_and_an_idle_composer():
    assert _finished(FINISHED, FINISHED_TASK) == 0
    # Another task on screen: not this launch's turn.
    assert _finished(FINISHED, "Reply with the current date and nothing else.") != 0
    # Still running, or no reply below the task yet.
    assert _finished(RUNNING, PROMPT) != 0
    no_reply = FINISHED.replace("• STARTED", "")
    assert _finished(no_reply, FINISHED_TASK) != 0
    # A bullet inside the task itself is not a reply.
    bulleted = "Do these:\n• one\n• two"
    screen = "› Do these:\n  • one\n  • two\n\n" + PROVISIONAL
    assert _finished(screen, bulleted) != 0
    # A trust screen up: never "finished".
    assert _finished(FINISHED.replace("› Ask Codex to do anything", "") + TRUST, FINISHED_TASK) != 0


STREAMING = (FIXTURES / "codex-0.158-streaming-frame.txt").read_text(encoding="utf-8")
LONG_TASK = "Start it immediately:\n\n1〜200 の素数を1行ずつ理由つきで書き出してください"


def test_a_streaming_reply_ends_the_wait_even_without_the_status_line():
    # While a reply streams, Codex hides "Working (...)": reply lines and an
    # idle-looking composer are what the screen shows (real frame, 2026-09-29).
    # The note therefore says a reply is on screen, not that the turn is over.
    assert _finished(STREAMING, LONG_TASK) == 0


# --- #118: a long reply, captured once a second on WSL (2026-09-30) ------------
# Frames 005-040 of one standalone spawn (Codex 0.158, gpt-6-luna low, 80x24, no
# receipt): banner, task, the reply streaming with no status line, then the task
# pushed off the screen while Codex pins its first line, cut with "…", as the
# first line. Polled every 3 s, the end of the task and the running line are
# each on screen for only a few seconds; missing both, the watch ran to its
# 90 s bound (90.3 s measured).
LONG_FRAMES = (FIXTURES / "codex-0.158-long-reply-frames.txt").read_text(encoding="utf-8").split("\f\n")
LONG_REPLY_TASK = ("You are PureBose, a standalone agent with no parent. The name PureBose is already "
                   "reserved and registered; do not register another identity, do not re-register yourself "
                   "(no agentstack-reregister), and do not fetch the inbox as a startup ritual. Starting child "
                   "agents of your own later is allowed. This prompt is the canonical task. Start it "
                   "immediately:\n\n1〜200 の素数を1行ずつ理由つきで書き出してください")


@pytest.mark.parametrize("offset", range(3))
@pytest.mark.parametrize("start", [5, 12, 15], ids=["from-banner", "task-scrolling", "task-gone"])
def test_a_long_reply_ends_the_wait_whatever_the_poll_phase(tmp_path, start, offset):
    # One poll every 3 frames; the last frame repeats, as the screen then stays.
    frames = LONG_FRAMES[start - 5 + offset::3]
    result, keys, notes = _watch(tmp_path, frames, prompt=LONG_REPLY_TASK)
    assert "STATUS=3 VERIFIED=false" in result.stdout and keys == []
    assert "First-task confirmation unknown" in notes
    # Before the fix the pinned-task frames (016 on) matched nothing: 90 s.
    assert _waited(notes) <= 9, notes


def test_the_pinned_task_line_must_be_this_tasks_first_line():
    pinned = LONG_FRAMES[-1]
    assert pinned.splitlines()[0].endswith("…")
    assert _finished(pinned, LONG_REPLY_TASK) == 0
    # Another child's task pinned there is not this launch's turn.
    other = LONG_REPLY_TASK.replace("PureBose", "BlueLake")
    assert _finished(pinned, other) != 0
    # Too short a prefix to tell tasks apart.
    short = pinned.replace(pinned.splitlines()[0], "You are…", 1)
    assert _finished(short, "You are somebody else entirely") != 0
    # A pinned line alone is not enough without the idle composer.
    assert _finished(pinned.replace("› Ask Codex to do anything", ""), LONG_REPLY_TASK) != 0


def test_a_task_end_wrapped_across_lines_is_still_found():
    # The last 24 characters of the task can straddle a terminal wrap.
    prompt = "Start it immediately: write the primes up to two hundred, each with its reason"
    wrapped = ("› Start it immediately: write the primes up to two hundred, each with\n"
               "  its reason\n\n• 2 — prime\n" + PROVISIONAL)
    assert _finished(wrapped, prompt) == 0
    # Regex characters in the task are taken literally.
    tricky = "Answer [yes] or no? (a.b*c) ^$ \\ done"
    screen = "› " + tricky + "\n\n• yes\n" + PROVISIONAL
    assert _finished(screen, tricky) == 0
    assert _finished("› something else\n\n• yes\n" + PROVISIONAL, tricky) != 0
