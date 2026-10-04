"""Claude readiness: screens the launcher must not answer, and failure records.

Claude Code asks some questions once, for the user ("Claude in Chrome extension
detected"). Answering may change the user's default for every later session,
so the launcher stops at once without sending a key, saves the screen and tells
the operator what to do. Other choice screens are recorded and stopped after
10s; a timeout or a dead session also leaves the screen in the incidents log.
"""
from __future__ import annotations

import os
import json
import pathlib
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from service_teardown import TEST_LABEL_PREFIX  # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parent.parent
SPAWN = ROOT / "hooks" / "spawn_child.sh"

# As captured on WSL (Claude Code 2.1.284), 2026-09-29.
CHROME_PROMPT = """\
  Claude in Chrome extension detected
  Claude will use your Chrome browser by default — navigating sites, filling
  forms, and capturing screenshots in your existing session.
  This session is in Auto mode, so an AI classifier approves routine browser
  actions — you are only prompted when it is unsure. Turn browser tools off
  for future sessions with /chrome.
  ❯ No, keep browser tools off
    Yes, use my browser
  Enter to confirm · Esc to keep browser tools off
"""
# Claude Code 2.1.289's first-run setup, captured 2026-10-04 in a HOME whose
# ~/.claude.json has no hasCompletedOnboarding (theme already in settings.json).
ONBOARDING_THEME = """\
 Let's get started.
 Choose the text style that looks best with your terminal
 To change this later, run /theme
     Auto (match terminal)
 ❯ ✔ Dark mode
     Light mode
     Dark mode (colorblind-friendly)
     Light mode (colorblind-friendly)
     Dark mode (ANSI colors only)
     Light mode (ANSI colors only)
 ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
  1  function greet() {
  2 -  console.log("Hello, World!");
  2 +  console.log("Hello, Claude!");
  3  }
 ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
  Syntax theme: Monokai Extended (ctrl+t to disable)
"""
ONBOARDING_LOGIN = """\
 Claude Code can be used with your Claude subscription or billed based on API usage through your Console account.
 Select login method:
 ❯ 1. Claude account with subscription · Pro, Max, Team, or Enterprise
   2. Anthropic Console account · API usage billing
   3. 3rd-party platform · Amazon Bedrock, Microsoft Foundry, Google Vertex AI
"""
# The two screens after login, as reported from a 2.1.289 run on a fresh WSL
# (2026-10-04); the launcher keys on "Press Enter to continue" plus the heading.
ONBOARDING_LOGIN_DONE = " Login successful. Press Enter to continue…\n"
ONBOARDING_SECURITY = """\
 Security notes:
 1. Claude can make mistakes
    You should always review Claude's responses, especially when
    running code.
 2. Due to prompt injection risks, only use it with code you trust
 Press Enter to continue…
"""
FIRST_RUN_REASON = (
    "Claude Code's first-run setup is not finished on this machine. Run 'claude' "
    "once in a terminal and go through text style, login, Security notes and "
    "trusting the folder until the normal input prompt appears, then /exit (if a "
    "'claude' window is still on those screens, finish it there). Then launch the "
    "child again."
)
TRUST_OLD = "Do you trust the files in this folder?\n  Yes\n  No\n"
READY = "\n❯ \n"
TRUST_NEW_UNSELECTED = "Quick safety check\n  \u276f No, exit\n    Yes, I trust this folder\n"
TRUST_NEW_SELECTED = "Quick safety check\n    No, exit\n  \u276f Yes, I trust this folder\n"
UNKNOWN_CHOICE = """\
  Something new to decide
  ❯ 1. Option A
    2. Option B
  Enter to confirm · Esc to cancel
"""
LOADING = "Starting Claude Code…\n  loading plugins\n"
PAD = "\n" * 30  # capture-pane pads to the window height


def _executable(path: pathlib.Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _launch(tmp_path, screens, *, die_after=0):
    """Run a pre-registered Claude launch against a fake tmux.

    ``screens`` are shown on successive polls (capture-pane without -S); the
    last one repeats. ``die_after`` > 0 ends the session after that many polls.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shots = tmp_path / "screens"
    shots.mkdir()
    for i, text in enumerate(screens, 1):
        (shots / str(i)).write_text(text + PAD, encoding="utf-8")
    runtime = tmp_path / "runtime"
    workdir = tmp_path / "work"
    workdir.mkdir()
    log = tmp_path / "tmux.log"
    _executable(
        bindir / "tmux",
        "#!/bin/bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_TMUX_LOG\"\n"
        "count_file=\"$FAKE_DIR/polls\"\n"
        "case \"$1\" in\n"
        "  new-session) : > \"$FAKE_DIR/alive\" ;;\n"
        "  capture-pane)\n"
        "    n=$(cat \"$count_file\" 2>/dev/null || echo 0)\n"
        "    if [[ \" $* \" != *' -S '* ]]; then n=$((n + 1)); echo $n > \"$count_file\"; fi\n"
        "    if [[ \"$FAKE_DIE_AFTER\" -gt 0 && $n -ge \"$FAKE_DIE_AFTER\" ]]; then rm -f \"$FAKE_DIR/alive\"; fi\n"
        "    [[ -f \"$FAKE_DIR/alive\" ]] || exit 1  # a dead session has nothing to capture\n"
        "    last=$(ls \"$FAKE_DIR/screens\" | sort -n | tail -1)\n"
        "    i=$(( n < 1 ? 1 : (n > last ? last : n) ))\n"
        "    cat \"$FAKE_DIR/screens/$i\" ;;\n"
        "  has-session) [[ -f \"$FAKE_DIR/alive\" ]] ;;\n"
        "  send-keys)\n"
        "    n=$(cat \"$count_file\" 2>/dev/null || echo 0)\n"
        "    printf 'screen=%s %s\\n' \"$n\" \"${*:2}\" >> \"$FAKE_DIR/keys\" ;;\n"
        "  kill-session) rm -f \"$FAKE_DIR/alive\" ;;\n"
        "  display-message) printf 'ParentAgent\\n' ;;\n"
        "esac\n",
    )
    _executable(bindir / "sleep", "#!/bin/bash\nexit 0\n")
    _executable(bindir / "claude", "#!/bin/bash\nexit 0\n")
    handoff = tmp_path / "child-token"
    handoff.write_text("child-owner-token", encoding="utf-8")
    handoff.chmod(0o600)
    binding = handoff.with_name(handoff.name + ".binding.json")
    binding.write_text(json.dumps({"agent_id": 73, "agent_name": "Probe-Curie",
                                   "project_key": "/shared/project", "program": "claude-code"}), encoding="utf-8")
    binding.chmod(0o600)

    env = os.environ.copy()
    env.update({
        "PATH": f"{bindir}:{env['PATH']}",
        "HOME": str(tmp_path / "home"),
        "PARENT_AGENT": "ParentAgent",
        "PROJECT_KEY": "/shared/project",
        "AGENTSTACK_PROJECT_KEY": "/shared/project",
        "AGENTSTACK_RUNTIME_DIR": str(runtime),
        "AGENTSTACK_HOOKS_DIR": str(ROOT / "hooks"),
        "AGENTSTACK_HOME": str(tmp_path / "agentstack"),
        "AGENTSTACK_LABEL_PREFIX": TEST_LABEL_PREFIX,
        "AGENTSTACK_REGISTER_LIB": str(ROOT / "bin" / "lib" / "agentstack-register.sh"),
        "AGENTSTACK_MCP_PROXY": str(tmp_path / "missing-proxy"),
        "AGENTSTACK_TERMINAL": "none",
        "FAKE_TMUX_LOG": str(log),
        "FAKE_DIR": str(tmp_path),
        "FAKE_DIE_AFTER": str(die_after),
    })
    env.pop("AGENTSTACK_SPAWN_INCIDENT_LOG", None)
    pathlib.Path(env["HOME"]).mkdir()
    result = subprocess.run(
        ["/bin/bash", str(SPAWN), "--pre-registered", "Probe-Curie",
         "--child-token-file", str(handoff), "task", str(workdir)],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=60, check=False,
    )
    calls = log.read_text(encoding="utf-8").splitlines()
    polls = int((tmp_path / "polls").read_text()) if (tmp_path / "polls").exists() else 0
    incidents = runtime / "spawn_incidents.log"
    return result, calls, polls, incidents.read_text(encoding="utf-8") if incidents.exists() else ""


def _keys_with_screens(tmp_path):
    """Each key the launcher sent, with the index of the screen then shown."""
    keys = tmp_path / "keys"
    return keys.read_text(encoding="utf-8").splitlines() if keys.exists() else []


def _keys(calls):
    return [c for c in calls if c.startswith("send-keys")]


def test_chrome_question_stops_at_once_without_any_key(tmp_path):
    result, calls, polls, incidents = _launch(tmp_path, [CHROME_PROMPT])
    assert result.returncode != 0
    assert _keys(calls) == []
    assert polls == 1  # no waiting out the 60s timeout
    assert "one-time question about Claude in Chrome" in result.stderr
    assert "No key was sent" in result.stderr
    assert "answer it yourself (or choose with /chrome)" in result.stderr
    assert "readiness timeout" not in result.stderr
    assert "not answered by the launcher (Probe-Curie)" in incidents
    assert "|   ❯ No, keep browser tools off" in incidents
    assert "|   Enter to confirm" in incidents
    # The session is cleaned up as before.
    assert any(c.startswith("kill-session") for c in calls)


@pytest.mark.parametrize("screen", [ONBOARDING_THEME, ONBOARDING_LOGIN, ONBOARDING_LOGIN_DONE, ONBOARDING_SECURITY],
                         ids=["theme", "login", "login-done", "security-notes"])
def test_first_run_setup_stops_at_once_with_a_reason_and_no_key(tmp_path, screen):
    """Before the fix the theme screen read as no choice at all, so the child
    sat out the 60s timeout and the cockpit said only that it closed."""
    result, calls, polls, incidents = _launch(tmp_path, [screen])
    assert result.returncode != 0
    assert _keys(calls) == []
    assert polls == 1
    assert "readiness timeout" not in result.stderr
    assert "first-run setup (text style, login or Security notes). No key was sent" in result.stderr
    assert "first-run setup is not finished; not answered by the launcher (Probe-Curie)" in incidents
    reasons = [line for line in result.stderr.splitlines()
               if line.startswith("[spawn_child] reason: ")]
    assert reasons == [
        "[spawn_child] reason: " + FIRST_RUN_REASON
    ]
    assert any(c.startswith("kill-session") for c in calls)


def test_chrome_question_also_leaves_a_reason_line(tmp_path):
    result, calls, polls, incidents = _launch(tmp_path, [CHROME_PROMPT])
    assert "[spawn_child] reason: Claude Code is asking a one-time question about Claude in Chrome." in result.stderr


def test_the_theme_screen_alone_is_not_mistaken_for_first_run_setup(tmp_path):
    """/theme opened by a user shows the same list but not the first-run
    heading; two cues are required."""
    later = ONBOARDING_THEME.replace("Choose the text style that looks best with your terminal", "Theme")
    result, calls, polls, incidents = _launch(tmp_path, [later, READY])
    assert "first-run setup" not in result.stderr


def test_chrome_question_replacing_the_trust_dialog_gets_no_key(tmp_path):
    """The wait loop saw the trust dialog, but by the time the trust helper
    captured again the Chrome question was up: no key may be sent."""
    result, calls, polls, incidents = _launch(tmp_path, [TRUST_OLD, CHROME_PROMPT])
    assert result.returncode != 0
    assert _keys_with_screens(tmp_path) == []
    assert "trust dialog is no longer on screen; not pressing a key" in result.stderr
    assert "one-time question about Claude in Chrome" in result.stderr
    assert "\u276f No, keep browser tools off" in incidents


@pytest.mark.parametrize("trust", ["old", "new"])
def test_trust_keys_land_only_on_trust_screens(tmp_path, trust):
    # Poll 1 and the helper's own capture (2) show the trust dialog; then the
    # Chrome question. Every key must be sent while screen 2 is up.
    screen = TRUST_OLD if trust == "old" else TRUST_NEW_SELECTED
    result, calls, polls, incidents = _launch(tmp_path, [screen, screen, CHROME_PROMPT])
    assert result.returncode != 0
    assert _keys_with_screens(tmp_path) == ["screen=2 -t Probe-Curie C-m"]
    assert "one-time question about Claude in Chrome" in result.stderr


def test_new_trust_dialog_that_turns_into_the_question_after_down_gets_no_enter(tmp_path):
    # The Yes row is not selected: Down goes to the trust screen (2); the
    # capture after it shows the Chrome question (3), so Enter is not sent.
    result, calls, polls, incidents = _launch(
        tmp_path, [TRUST_NEW_UNSELECTED, TRUST_NEW_UNSELECTED, CHROME_PROMPT])
    assert result.returncode != 0
    assert _keys_with_screens(tmp_path) == ["screen=2 -t Probe-Curie Down"]
    assert "Yes row is not selected; not pressing Enter" in result.stderr


def test_a_leftover_trust_line_does_not_make_an_unknown_choice_a_trust_dialog(tmp_path):
    """A trust line still visible higher up, an unknown No/Yes choice below:
    the choice screen wins, no key is sent, and it stops after 10s."""
    screen = ("Do you trust the files in this folder?\n"
              "  (accepted)\n\n"
              "  Something new to decide\n"
              "  \u276f No\n"
              "    Yes\n"
              "  Enter to confirm \u00b7 Esc to cancel\n")
    result, calls, polls, incidents = _launch(tmp_path, [screen])
    assert result.returncode != 0
    assert _keys_with_screens(tmp_path) == []
    assert "choice screen ORRERY does not recognise for 10s" in result.stderr
    assert "trust dialog persisted" not in result.stderr


# PinkMendeleev's probes on 0f04cdb: an earlier trust dialog (old or new
# wording) is still visible, and a different choice is the active one below it.
OTHER_CHOICE = "Settings prompt\n  \u276f Allow\n    Deny\n  Enter to confirm\n"
STALE_NEW_TRUST = ("Previous screen (accepted):\nQuick safety check\n    No, exit\n"
                   "  \u276f Yes, I trust this folder\n\n" + OTHER_CHOICE)
STALE_OLD_TRUST = "Previous: Do you trust the files in this folder?\n" + OTHER_CHOICE


@pytest.mark.parametrize("screens", [
    [STALE_NEW_TRUST],
    [STALE_OLD_TRUST],
    # The poll saw the real trust dialog; the helper's own capture already
    # shows the stale dialog with the new choice below it.
    [TRUST_NEW_SELECTED, STALE_NEW_TRUST],
], ids=["new-trust-remnant", "old-trust-remnant", "after-recapture"])
def test_only_the_active_choice_decides_and_a_trust_remnant_gets_no_key(tmp_path, screens):
    result, calls, polls, incidents = _launch(tmp_path, screens)
    assert result.returncode != 0
    assert _keys_with_screens(tmp_path) == []
    assert "choice screen ORRERY does not recognise for 10s" in result.stderr
    assert "|   \u276f Allow" in incidents


# MintHooke / Pink on 059a84f: an earlier dialog with "Yes" selected is still
# visible above the current one, where "No, exit" is selected.
OLD_YES_ABOVE = "Quick safety check\n    No, exit\n  \u276f Yes, I trust this folder\n\n"
CURRENT_NO = OLD_YES_ABOVE + TRUST_NEW_UNSELECTED + "  Enter to confirm \u00b7 Esc to exit\n"
CURRENT_YES = OLD_YES_ABOVE + TRUST_NEW_SELECTED + "  Enter to confirm \u00b7 Esc to exit\n"


def test_the_selected_row_is_read_from_the_active_dialog_only(tmp_path):
    # Poll (1) and the helper (2) see "No" selected below an old selected
    # "Yes": the helper must press Down, and Enter only once the capture after
    # it (3) shows "Yes" selected in the active dialog.
    result, calls, polls, incidents = _launch(tmp_path, [CURRENT_NO, CURRENT_NO, CURRENT_YES, READY])
    assert result.returncode == 0, result.stderr
    assert _keys_with_screens(tmp_path)[:2] == [
        "screen=2 -t Probe-Curie Down", "screen=3 -t Probe-Curie C-m"]


def test_no_enter_while_the_active_dialog_still_has_no_selected(tmp_path):
    result, calls, polls, incidents = _launch(tmp_path, [CURRENT_NO])
    assert result.returncode != 0
    keys = _keys_with_screens(tmp_path)
    assert keys and all(k.endswith(" Down") for k in keys), keys
    assert "Yes row is not selected; not pressing Enter" in result.stderr
    assert "selecting 'Yes, I trust this folder'" not in result.stderr


def test_a_trust_dialog_with_a_confirm_footer_is_still_accepted(tmp_path):
    trust = ("Quick safety check\n"
             "    No, exit\n"
             "  \u276f Yes, I trust this folder\n"
             "  Enter to confirm \u00b7 Esc to exit\n")
    result, calls, polls, incidents = _launch(tmp_path, [trust, trust, READY])
    assert result.returncode == 0, result.stderr
    assert _keys_with_screens(tmp_path)[0] == "screen=2 -t Probe-Curie C-m"


def test_chrome_question_is_recognised_by_its_options_alone(tmp_path):
    options_only = CHROME_PROMPT.replace("Claude in Chrome extension detected", "Browser tools")
    result, calls, _polls, _incidents = _launch(tmp_path, [options_only])
    assert result.returncode != 0
    assert _keys(calls) == []
    assert "one-time question about Claude in Chrome" in result.stderr


def test_unrecognised_choice_screen_stops_after_ten_seconds(tmp_path):
    result, calls, polls, incidents = _launch(tmp_path, [UNKNOWN_CHOICE])
    assert result.returncode != 0
    assert _keys(calls) == []
    # 2s per poll: first seen at 2s, stopped once it has stayed 10s.
    assert polls == 6
    assert "choice screen ORRERY does not recognise for 10s" in result.stderr
    assert "unrecognised choice screen; not answered by the launcher" in incidents
    assert "|   ❯ 1. Option A" in incidents


@pytest.mark.parametrize("ready_cue", ["\u276f \n", "? for shortcuts\n"])
def test_choice_screen_wins_over_ready_cues_left_on_screen(tmp_path, ready_cue):
    """A dialog with the empty input row or the shortcuts footer still visible
    is a choice screen: no task is pasted and nothing is confirmed."""
    result, calls, polls, incidents = _launch(tmp_path, [UNKNOWN_CHOICE + ready_cue])
    assert result.returncode != 0
    assert _keys_with_screens(tmp_path) == []
    assert not any(c.startswith(("paste-buffer", "load-buffer")) for c in calls)
    assert "choice screen ORRERY does not recognise for 10s" in result.stderr


def test_failure_record_drops_interior_blank_lines(tmp_path):
    screen = "IMPORTANT HEADING\n" + "\n" * 45 + "last detail\n"
    result, calls, polls, incidents = _launch(tmp_path, [screen])
    assert result.returncode != 0
    assert "| IMPORTANT HEADING" in incidents
    assert "| last detail" in incidents
    assert "\n    | \n" not in incidents
    assert "IMPORTANT HEADING" in result.stderr


def test_timeout_records_the_screen_and_reports_progress(tmp_path):
    result, calls, polls, incidents = _launch(tmp_path, [LOADING])
    assert result.returncode != 0
    assert _keys(calls) == []
    assert polls == 30
    assert "Claude readiness timeout (60s)" in result.stderr
    for second in (10, 20, 30, 40, 50):
        assert f"Waiting for Claude ({second}s): starting; last line:   loading plugins" in result.stderr
    # The padded capture no longer hides the screen.
    assert "Starting Claude Code" in result.stderr
    assert "Claude readiness timeout (60s) (Probe-Curie)" in incidents
    assert "|   loading plugins" in incidents


def test_dead_session_records_its_last_screen(tmp_path):
    # Poll 1 sees the screen; from poll 2 the session is gone and every
    # capture fails, so the record must fall back to the last screen seen.
    result, calls, _polls, incidents = _launch(tmp_path, [LOADING], die_after=2)
    assert result.returncode != 0
    assert "died after" in result.stderr
    assert "Aborting: Claude terminated before readiness." in result.stderr
    assert "Claude terminated before readiness (Probe-Curie)" in incidents
    assert "|   loading plugins" in incidents


@pytest.mark.parametrize("screen, guidance", [
    (CHROME_PROMPT, "answer it yourself (or choose with /chrome), then launch the child again."),
    (UNKNOWN_CHOICE, "answer it once in a normal 'claude' session, then launch the child again."),
    (LOADING, "refusing to inject the task into an unknown screen state."),
])
def test_guidance_survives_the_dashboard_tail(tmp_path, screen, guidance):
    """The dashboard shows the last 1000 characters of the launcher's output
    (dashboard/server.py); the long screen record must not push the reason
    and what to do out of it."""
    context = "".join(f"  startup context line {i:02d} with some width to it\n" for i in range(40))
    result, calls, polls, incidents = _launch(tmp_path, [context + screen])
    assert result.returncode != 0
    assert guidance in result.stderr[-1000:]


def test_ready_screen_is_still_ready(tmp_path):
    result, calls, polls, incidents = _launch(tmp_path, [LOADING, READY])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "Probe-Curie"
    assert "not answered by the launcher" not in incidents


def test_choice_screen_that_resolves_itself_does_not_stop_the_launch(tmp_path):
    result, calls, _polls, _incidents = _launch(
        tmp_path, [UNKNOWN_CHOICE, UNKNOWN_CHOICE, LOADING, READY])
    assert result.returncode == 0, result.stderr
    # No key at all: the choice resolved itself, and the task is the launch
    # argument rather than a paste followed by Enter.
    assert _keys(calls) == []


@pytest.mark.parametrize("prefix", ["spawn_child/pre-reg", "spawn_child"])
def test_both_claude_launch_paths_use_the_shared_wait(prefix):
    text = SPAWN.read_text(encoding="utf-8")
    assert f'wait_for_claude_ready "$CHILD_NAME" "{prefix}"' in text
    # The copied per-path loops are gone.
    assert "while [[ $WAITED -lt 60 ]]" not in text
    assert "\n    WAIT_MAX=60" not in text


# As captured on macOS (Sonnet 5), 2026-10-01: a child started with its first
# prompt as the `claude [prompt]` argument shows that prompt at once after the
# cursor glyph and starts working. The echoed prompt row is not a choice; the
# launcher took it for one and stopped a child that was already working.
WORKING_ON_ARGV_PROMPT = """\
❯ Child agent startup. AGENT_NAME=Probe-Curie; parent=ParentAgent. Follow the
  child-agent startup procedure in CLAUDE.md and start the task immediately.
· Wandering… (11s · thinking with medium effort)
  tmux focus-events off · add 'set -g focus-events on' to ~/.tmux.conf and re…
────────────────────────────────────────────────────────────────────────────────
❯ 
────────────────────────────────────────────────────────────────────────────────
  work | Sonnet 5
  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents
"""


def test_the_echo_of_the_launch_prompt_is_not_a_choice_screen(tmp_path):
    result, calls, polls, incidents = _launch(tmp_path, [LOADING, WORKING_ON_ARGV_PROMPT])
    assert result.returncode == 0, result.stderr
    assert "unrecognised choice screen" not in incidents
    assert _keys(calls) == []
    assert polls <= 3


def test_a_real_choice_below_the_echoed_prompt_still_stops_the_launch(tmp_path):
    """Ignoring the echo must not hide a dialog shown after it."""
    screen = WORKING_ON_ARGV_PROMPT.split("────")[0] + UNKNOWN_CHOICE
    result, calls, _polls, incidents = _launch(tmp_path, [screen])
    assert result.returncode != 0
    assert "unrecognised choice screen" in incidents
    assert _keys(calls) == []


# From the incident log of a fresh Windows/WSL machine, 2.1.289 (2026-10-04):
# the child answered its launch prompt, then Claude offered the new renderer.
# WSL draws the reply row with "●"; macOS draws "⏺".
RENDERER_QUESTION = """\
────────────────────────────────────────────────────────────────────────────────
  Try the new fullscreen renderer?
  · Flicker-free output — fixes the flashing you see during long responses
  · Mouse support — click to move your cursor or expand results
  · Selected text auto-copies to your clipboard
  ❯ 1. Yes, try it
    2. Not now
  Enter to confirm · Esc to cancel
"""
ANSWERED_ARGV_PROMPT = """\
❯ Child agent startup. AGENT_NAME=Probe-Curie; parent=ParentAgent. Follow the
  child-agent startup procedure in CLAUDE.md and start the task immediately.
● Hello from Probe-Curie. Starting on the task now.
✻ Cogitated for 2s · done 9:21 AM
"""


@pytest.mark.parametrize("glyph", ["●", "⏺"])
def test_a_question_after_the_child_answered_its_prompt_does_not_remove_it(tmp_path, glyph):
    answered = ANSWERED_ARGV_PROMPT.replace("● ", glyph + " ")
    result, calls, polls, incidents = _launch(tmp_path, [answered + RENDERER_QUESTION])
    assert result.returncode == 0, result.stderr
    assert _keys(calls) == []
    assert not any(c.startswith("kill-session") for c in calls)
    assert "unrecognised choice screen" not in incidents
    assert "has answered its first prompt and is now showing a question for you" in result.stderr


def test_an_unknown_choice_after_the_answer_is_also_left_to_the_user(tmp_path):
    result, calls, polls, incidents = _launch(tmp_path, [ANSWERED_ARGV_PROMPT + UNKNOWN_CHOICE])
    assert result.returncode == 0, result.stderr
    assert _keys(calls) == []
    assert "unrecognised choice screen" not in incidents


def test_the_renderer_question_before_any_answer_stops_with_a_reason(tmp_path):
    result, calls, polls, incidents = _launch(tmp_path, [RENDERER_QUESTION])
    assert result.returncode != 0
    assert _keys(calls) == []
    assert polls == 1
    assert "about the fullscreen renderer; not answered by the launcher (Probe-Curie)" in incidents
    assert ("[spawn_child] reason: Claude Code is asking a one-time question (Try the new "
            "fullscreen renderer?). Run 'claude' once in a terminal and answer it, then "
            "launch the child again.") in result.stderr


def test_an_echo_without_a_reply_does_not_count_as_answered(tmp_path):
    """The echo alone is not an answer: a choice below it still stops (see
    test_a_real_choice_below_the_echoed_prompt_still_stops_the_launch)."""
    echo_only = ANSWERED_ARGV_PROMPT.replace("● Hello from Probe-Curie. Starting on the task now.\n", "")
    result, calls, _polls, incidents = _launch(tmp_path, [echo_only + UNKNOWN_CHOICE])
    assert result.returncode != 0
    assert "unrecognised choice screen" in incidents
