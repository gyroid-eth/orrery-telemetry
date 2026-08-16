#!/usr/bin/env python3
"""Regression: the tokenless agent-mail transport must not kill the launchers.

Background
----------
Reported 2026-08-16 against 0.9.0 by a tester running
`AGENTSTACK_MAIL_HTTP_BEARER_MODE=disabled` (owner-token transport, no legacy
HTTP bearer). Typing `cc` produced *nothing at all* — no Claude, no tmux
session, no error — while `cx` still worked, because two separate defects sit
on the no-token path in `bin/lib/agentstack-register.sh`:

1. `ags_mail_load_token` ended with `[[ -n "$tok" ]]`, so "there is no token"
   (the normal case for that transport) returned 1. Every caller runs
   `set -euo pipefail`, so the shell died at the call site with no output. The
   fix is an explicit `return 0` — the same shape `ags_load_env` already uses.
2. `ags_mcp_call` expanded an empty `auth` array as `"${auth[@]}"`. macOS bash
   3.2 — which is what `/bin/bash` and every `#!/bin/bash` script here runs —
   calls that an unbound variable under `set -u`. The fix is the nounset-safe
   `${auth[@]+"${auth[@]}"}`.

These run the real library under `/bin/bash` with `set -euo pipefail`, with
`curl` stubbed so no network or live mail server is involved. The stub records
argv with its boundaries intact (JSONL, not `"$*"`), because a quoting
regression in the Authorization header is invisible to a flattened string.

Runnable two ways (no third-party dependency required):
    python3 tests/test_register_tokenless.py   # plain script, prints PASS/FAIL
    pytest tests/test_register_tokenless.py     # under pytest if available
"""
from __future__ import annotations

import json
import os
import pathlib
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_LIB = _ROOT / "bin" / "lib" / "agentstack-register.sh"
_AGENT_START = _ROOT / "bin" / "agent-start"
# The scripts under test all use `#!/bin/bash`, so the interpreter that matters
# is the system one (bash 3.2 on macOS), not a homebrew bash 5 on PATH.
_BASH = "/bin/bash"


def _write_exec(path: pathlib.Path, body: str) -> pathlib.Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


_CURL_STUB = '''#!/usr/bin/env python3
"""Stand-in for curl: records argv *with boundaries* and answers like the MCP."""
import json, os, sys

log = os.environ["CURL_STUB_LOG"]
payload_raw = sys.stdin.read()
try:
    payload = json.loads(payload_raw)
except ValueError:
    payload = None

tool, args = "", {}
if isinstance(payload, dict):
    params = payload.get("params") or {}
    tool = params.get("name") or ""
    args = params.get("arguments") or {}

with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "tool": tool, "args": args}) + "\\n")

# register_agent must echo a name back, otherwise the caller treats the whole
# registration as failed and the launcher silently takes its fallback path —
# which would mean this suite never exercises a *successful* tokenless launch.
body = {}
if tool == "register_agent":
    body = {"name": args.get("name") or "StubAgent", "registration_token": "stub-token"}
elif tool == "check_agent_name_available":
    body = {"available": True}

print(json.dumps({"jsonrpc": "2.0", "id": "1",
                  "result": {"content": [{"type": "text", "text": json.dumps(body)}]}}))
'''

# agent-start copies the agent name to the clipboard (bin/agent-start:82). Left
# unstubbed, running this suite overwrites the developer's real clipboard.
_PBCOPY_STUB = '#!/bin/bash\ncat >> "${PBCOPY_STUB_LOG:-/dev/null}"\n'


def _stub_bin(bindir: pathlib.Path) -> pathlib.Path:
    """Populate `bindir` with the stubs and return the curl argv log path."""
    bindir.mkdir(parents=True, exist_ok=True)
    log = bindir / "curl-calls.jsonl"
    _write_exec(bindir / "curl", _CURL_STUB)
    _write_exec(bindir / "pbcopy", _PBCOPY_STUB)
    return log


def _curl_calls(log: pathlib.Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


def _run_snippet(snippet: str, env_extra: dict | None = None):
    env = dict(os.environ)
    for key in ("MCP_AGENT_MAIL_TOKEN", "AGENTSTACK_MAIL_ENV", "MAIL_ENV"):
        env.pop(key, None)
    env.update(env_extra or {})
    return subprocess.run(
        [_BASH, "-euo", "pipefail", "-c", snippet],
        capture_output=True, text=True, env=env, timeout=60,
    )


_SOURCE = f". {_LIB}\n"


# --- 1. loader returns success when there is no bearer token -----------------

def test_load_token_succeeds_without_a_bearer_token():
    with tempfile.TemporaryDirectory() as td:
        mail_env = pathlib.Path(td) / "service.env"
        mail_env.write_text(
            "AGENTSTACK_MAIL_HTTP_BEARER_MODE=disabled\nAGENTSTACK_MAIL_PORT=8765\n",
            encoding="utf-8",
        )
        r = _run_snippet(
            _SOURCE + 'ags_mail_load_token\nprintf "reached:%s\\n" "${MCP_AGENT_MAIL_TOKEN:-<none>}"\n',
            {"AGENTSTACK_MAIL_ENV": str(mail_env)},
        )
    assert r.returncode == 0, (
        "ags_mail_load_token failed on the tokenless path; `set -e` in the callers "
        f"turns this into a silent launcher death.\nstderr: {r.stderr}"
    )
    assert "reached:<none>" in r.stdout, r.stdout


def test_load_token_succeeds_when_no_mail_env_is_configured():
    r = _run_snippet(_SOURCE + 'ags_mail_load_token\nprintf "reached\\n"\n')
    assert r.returncode == 0, r.stderr
    assert "reached" in r.stdout


# --- 2. loader is a no-op (and still succeeds) when a token already exists ----

def test_load_token_keeps_an_already_exported_token():
    r = _run_snippet(
        _SOURCE + 'ags_mail_load_token\nprintf "token:%s\\n" "$MCP_AGENT_MAIL_TOKEN"\n',
        {"MCP_AGENT_MAIL_TOKEN": "preset-token"},
    )
    assert r.returncode == 0, r.stderr
    assert "token:preset-token" in r.stdout, r.stdout


def test_load_token_reads_a_token_out_of_the_mail_env():
    with tempfile.TemporaryDirectory() as td:
        mail_env = pathlib.Path(td) / "service.env"
        mail_env.write_text("HTTP_BEARER_TOKEN=from-env-file\n", encoding="utf-8")
        r = _run_snippet(
            _SOURCE + 'ags_mail_load_token\nprintf "token:%s\\n" "${MCP_AGENT_MAIL_TOKEN:-<none>}"\n',
            {"AGENTSTACK_MAIL_ENV": str(mail_env)},
        )
    assert r.returncode == 0, r.stderr
    assert "token:from-env-file" in r.stdout, r.stdout


# --- 3./4. the auth array expansion is nounset-safe and keeps its quoting -----

def test_mcp_call_without_auth_survives_nounset():
    with tempfile.TemporaryDirectory() as td:
        bindir = pathlib.Path(td)
        log = _stub_bin(bindir)
        r = _run_snippet(
            _SOURCE + 'ags_mcp_call "health_check" >/dev/null\nprintf "reached\\n"\n',
            {"PATH": f"{bindir}:{os.environ['PATH']}", "CURL_STUB_LOG": str(log)},
        )
        calls = _curl_calls(log)
    assert "unbound variable" not in r.stderr, (
        "empty auth[@] expanded as an unbound variable — macOS bash 3.2 aborts here.\n"
        f"stderr: {r.stderr}"
    )
    assert r.returncode == 0, r.stderr
    assert "reached" in r.stdout, r.stdout
    assert len(calls) == 1, calls
    assert not [a for a in calls[0]["argv"] if a.startswith("Authorization")], calls[0]["argv"]
    # The rest of the request must survive the expansion change untouched.
    assert "--data-binary" in calls[0]["argv"], calls[0]["argv"]
    assert calls[0]["tool"] == "health_check", calls[0]


def test_mcp_call_with_auth_sends_the_header_as_one_argument():
    """A flattened `"$*"` log cannot tell `-H "A: B"` from `-H A: B`."""
    with tempfile.TemporaryDirectory() as td:
        bindir = pathlib.Path(td)
        log = _stub_bin(bindir)
        r = _run_snippet(
            _SOURCE + 'ags_mcp_call "health_check" >/dev/null\n',
            {
                "PATH": f"{bindir}:{os.environ['PATH']}",
                "CURL_STUB_LOG": str(log),
                "MCP_AGENT_MAIL_TOKEN": "header-token",
            },
        )
        calls = _curl_calls(log)
    assert r.returncode == 0, r.stderr
    argv = calls[0]["argv"]
    assert "Authorization: Bearer header-token" in argv, argv
    assert argv[argv.index("Authorization: Bearer header-token") - 1] == "-H", argv


def test_mcp_call_keeps_a_hostile_token_in_a_single_argument():
    """Spaces/quotes/globs in the token must not split into extra argv slots."""
    hostile = "tok en 'q\"q' *? ;&| \\ $HOME"
    with tempfile.TemporaryDirectory() as td:
        bindir = pathlib.Path(td)
        log = _stub_bin(bindir)
        r = _run_snippet(
            _SOURCE + 'ags_mcp_call "health_check" >/dev/null\n',
            {
                "PATH": f"{bindir}:{os.environ['PATH']}",
                "CURL_STUB_LOG": str(log),
                "MCP_AGENT_MAIL_TOKEN": hostile,
            },
        )
        calls = _curl_calls(log)
    assert r.returncode == 0, r.stderr
    argv = calls[0]["argv"]
    assert f"Authorization: Bearer {hostile}" in argv, argv


# --- 5. agent-start on a tokenless transport ---------------------------------

def _agent_start_env(root: pathlib.Path, bindir: pathlib.Path, workdir: pathlib.Path,
                     log: pathlib.Path) -> dict:
    mail_env = root / "service.env"
    mail_env.write_text("AGENTSTACK_MAIL_HTTP_BEARER_MODE=disabled\n", encoding="utf-8")
    env = dict(os.environ)
    for key in ("TMUX", "MCP_AGENT_MAIL_TOKEN"):
        env.pop(key, None)
    env.update({
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "HOME": str(root / "home"),           # keep all state writes in the sandbox
        "AGENTSTACK_HOME": str(root / "agentstack"),
        # Required whenever a test sets AGENTSTACK_HOME: without a test-specific
        # label prefix a teardown can boot out this machine's real
        # org.agentstack.agentdashboard (tests/test_service_label_isolation.py).
        "AGENTSTACK_LABEL_PREFIX": "org.agentstack.register-tokenless-test",
        "AGENTSTACK_MAIL_ENV": str(mail_env),
        "AGENTSTACK_PROJECT_KEY": str(workdir),
        "AGENTSTACK_CLAUDE_BIN": str(bindir / "claude"),
        "AGENTSTACK_HOOKS_DIR": "",           # no mail-watcher tmux session
        "AGENTSTACK_MANAGED_AGENTS_FILE": str(root / "managed.txt"),
        "CURL_STUB_LOG": str(log),
        "PBCOPY_STUB_LOG": str(root / "pbcopy.txt"),
    })
    (root / "home").mkdir(exist_ok=True)
    return env


def test_agent_start_registers_and_reaches_the_launch_stage():
    """The reported symptom: `cc` exits before Claude or tmux exist, silently.

    Asserting only "the launch line was printed" is too weak on its own — that
    line is printed before the exec, so it survives a failed launch. Here the
    registration must also have gone through the tokenless transport and the
    server-returned name must be the one the session is launched under.
    """
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        bindir = root / "bin"
        log = _stub_bin(bindir)
        workdir = root / "work"
        workdir.mkdir()
        _write_exec(bindir / "claude", "#!/bin/bash\nprintf 'claude-stub\\n'\n")
        env = _agent_start_env(root, bindir, workdir, log)
        env["TMUX_TMPDIR"] = str(root)        # isolate from the user's tmux server
        env["SHELL"] = "/usr/bin/false"
        r = subprocess.run(
            [_BASH, str(_AGENT_START), str(workdir)],
            capture_output=True, text=True, env=env,
            stdin=subprocess.DEVNULL, timeout=120,
        )
        calls = _curl_calls(log)
        clipboard = (root / "pbcopy.txt").read_text(encoding="utf-8") if (root / "pbcopy.txt").exists() else ""
    combined = r.stdout + r.stderr
    assert "unbound variable" not in combined, combined
    assert "registration failed" not in combined, (
        "the tokenless pre-registration did not succeed, so this test would not "
        f"have covered the real launch path.\n{combined}"
    )
    registered = [c for c in calls if c["tool"] == "register_agent"]
    assert registered, [c["tool"] for c in calls]
    name = registered[-1]["args"].get("name")
    assert name, registered[-1]
    assert f"launching Claude agent in tmux session '{name}'" in combined, (
        "agent-start did not reach the launch stage under the registered identity — "
        f"this is the reported silent `cc` death.\n{combined}"
    )
    assert name in clipboard, (name, clipboard)


def test_agent_start_actually_execs_claude_inside_tmux():
    """End-to-end: the launch line is not proof that anything started.

    A real tmux on a private socket runs agent-start in-pane; the Claude stub
    drops a marker file. Without this, the string assertion above passes even
    when tmux fails with "open terminal failed: not a terminal".
    """
    tmux = shutil.which("tmux") or "/opt/homebrew/bin/tmux"
    if not os.path.exists(tmux):
        print("note: tmux not installed; end-to-end launch not covered here")
        return
    socket = "agentstack-tokenless-test"
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        bindir = root / "bin"
        log = _stub_bin(bindir)
        workdir = root / "work"
        workdir.mkdir()
        marker = root / "claude-ran"
        _write_exec(
            bindir / "claude",
            f"#!/bin/bash\nprintf 'started\\n' > {marker}\nsleep 5\n",
        )
        env = _agent_start_env(root, bindir, workdir, log)
        env["TMUX_TMPDIR"] = str(root)
        try:
            subprocess.run(
                [tmux, "-L", socket, "new-session", "-d", "-s", "probe",
                 f"{_BASH} {_AGENT_START} {workdir}"],
                capture_output=True, text=True, env=env, timeout=60,
            )
            deadline = time.time() + 30
            while time.time() < deadline and not marker.exists():
                time.sleep(0.5)
            started = marker.exists()
            pane = subprocess.run(
                [tmux, "-L", socket, "list-sessions"],
                capture_output=True, text=True, env=env, timeout=30,
            ).stdout
        finally:
            subprocess.run([tmux, "-L", socket, "kill-server"],
                           capture_output=True, text=True, env=env, timeout=30)
    assert started, (
        "agent-start never exec'd the Claude binary inside tmux on a tokenless "
        f"transport.\ntmux sessions: {pane}"
    )


# --- the callers that must survive a `return 1` from the loader ---------------

def test_every_bare_caller_of_the_loader_tolerates_no_token():
    """Enumerate the call sites and prove each one is safe, empirically.

    An earlier version of this file tried to catch the *class* with a regex lint
    over every shell function. RosyArrhenius' review showed that lint was
    unsound in both directions — it flagged a trailing test inside an `if false`
    branch (the function actually returns 0) and missed three real shapes (a
    comment after `fi`, a trailing test with no `&&`, and `danger; return $?`
    which still aborts under `set -e`). A regex cannot decide shell exit status,
    so this asserts the property that actually matters instead: every script
    that calls the loader as a bare statement gets a zero status from it in the
    tokenless configuration.
    """
    callers = sorted(
        path
        for d in ("bin", "hooks", "scripts")
        if (_ROOT / d).is_dir()
        for path in (_ROOT / d).rglob("*")
        if path.is_file()
        and re.search(r"^\s*ags_mail_load_token\s*$",
                      path.read_text(encoding="utf-8", errors="replace"), re.M)
    )
    assert callers, "no bare caller found — has the loader been renamed?"
    with tempfile.TemporaryDirectory() as td:
        mail_env = pathlib.Path(td) / "service.env"
        mail_env.write_text("AGENTSTACK_MAIL_HTTP_BEARER_MODE=disabled\n", encoding="utf-8")
        r = _run_snippet(
            _SOURCE + "ags_mail_load_token\nprintf 'status:%s\\n' \"$?\"\n",
            {"AGENTSTACK_MAIL_ENV": str(mail_env)},
        )
    assert "status:0" in r.stdout, (
        "the loader returns non-zero without a token; under `set -e` that kills "
        f"each of these callers where they stand:\n  "
        + "\n  ".join(str(p.relative_to(_ROOT)) for p in callers)
        + f"\nstdout: {r.stdout} stderr: {r.stderr}"
    )


# --- the same bash-3.2 empty-array shape, found elsewhere in the repo ---------

def test_claude_setup_reports_a_bad_scope_instead_of_an_unbound_variable():
    """`resolve_targets` dies inside a process substitution, so only the subshell
    dies: the message printed, TARGETS stayed empty, and bash 3.2 + `set -u`
    then aborted on `"${TARGETS[@]}"`. The user saw an internal-looking
    `TARGETS[@]: unbound variable` stacked under their real error.

    Found by RosyArrhenius while reviewing the tokenless fix (2026-08-16).
    """
    setup = _ROOT / "bin" / "agentstack-claude-setup"
    if not setup.exists():
        print("note: agentstack-claude-setup is gone; nothing to guard")
        return
    env = dict(os.environ)
    env["AGENTSTACK_CLAUDE_MD_SCOPE"] = "bogus"
    r = subprocess.run([_BASH, str(setup), "--print"], capture_output=True,
                       text=True, env=env, stdin=subprocess.DEVNULL, timeout=60)
    assert "unbound variable" not in r.stderr, r.stderr
    assert "invalid AGENTSTACK_CLAUDE_MD_SCOPE" in r.stderr, r.stderr
    assert r.returncode != 0, (
        "an unresolvable scope must fail; a nounset-safe expansion alone would "
        "turn the resolver's death into a silent successful no-op"
    )


# --- coverage guard ----------------------------------------------------------

def test_system_bash_is_the_one_the_scripts_actually_run():
    """On macOS `/bin/bash` is 3.2; that is where the empty-array defect bites.

    Elsewhere these tests still exercise the logic, but the nounset-on-empty-array
    behaviour only reproduces on 3.2, so say so rather than implying coverage.
    """
    out = subprocess.run([_BASH, "--version"], capture_output=True, text=True).stdout
    if platform.system() == "Darwin":
        assert "version 3.2" in out, (
            "macOS is expected to run these scripts under system bash 3.2; got:\n" + out
        )
    else:
        print(f"note: bash-3.2-specific coverage not available here ({out.splitlines()[0]})")


def _main() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
