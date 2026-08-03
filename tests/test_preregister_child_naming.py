#!/usr/bin/env python3
"""Regression tests for delegated-child naming (the "Flory" report).

A child pre-registered with a hand-written name never went through the picker
and was never checked against the bundled scientist list, so names like
"Curious-Flory" registered fine and then had no dashboard portrait.

Runnable two ways (no third-party dependency required):
    python3 tests/test_preregister_child_naming.py
    pytest tests/test_preregister_child_naming.py
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_HELPER = _ROOT / "bin" / "agentstack-preregister-child"

# Stands in for bin/lib/agentstack-register.sh so the test exercises the
# helper's real control flow without touching a live agent-mail server.
_FAKE_LIB = r"""
ags_mail_load_token() { :; }
ags_pick_available_agent_name() { echo "PICKER_CALLED" >&2; printf 'Picked-Curie\n'; }
ags_has_scientist_suffix() {
  case "$1" in
    *Curie|*Bohr|*Fermi) return 0 ;;
    *) return 1 ;;
  esac
}
ags_generate_registration_token() { printf 'test-token\n'; }
ags_mcp_call() {
  case "$1" in
    register_agent)
      for arg in "$@"; do
        case "$arg" in
          name=*)
            returned_name="${arg#name=}"
            [[ -n "${FAKE_RETURNED_NAME:-}" ]] && returned_name="$FAKE_RETURNED_NAME"
            printf '{"name":"%s"}\n' "$returned_name"
            ;;
        esac
      done
      ;;
    *) printf '{}\n' ;;
  esac
}
ags_mcp_has_error() { return 1; }
ags_extract_agent_name() { python3 -c 'import json,sys; print(json.load(sys.stdin).get("name",""))'; }
ags_extract_registration_token() { printf '\n'; }
ags_store_registration_token() { :; }
ags_apply_contact_policy() { :; }
"""


def _run(args: list[str], env: dict[str, str] | None = None
         ) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)
        lib = tmpdir / "fake-register.sh"
        lib.write_text(_FAKE_LIB, encoding="utf-8")
        run_env = os.environ.copy()
        run_env.update({
            "AGENTSTACK_REGISTER_LIB": str(lib),
            "AGENTSTACK_ENV_FILE": "",
            "AGENTSTACK_HOME": str(tmpdir),
            "AGENTSTACK_PROJECT_KEY": "/p",
            "AGENTSTACK_STRICT_AGENT_NAMES": "",
        })
        if env:
            run_env.update(env)
        return subprocess.run(
            [str(_HELPER), "--token-file-out", str(tmpdir / "tok"), *args],
            cwd=_ROOT, env=run_env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )


def test_name_is_optional_and_defaults_to_the_picker():
    result = _run(["--program", "claude-code", "--model", "m"])
    assert result.returncode == 0, result.stderr
    assert "PICKER_CALLED" in result.stderr, "helper did not use the picker"
    assert result.stdout.strip() == "Picked-Curie", result.stdout


def test_off_list_name_warns_but_still_registers():
    result = _run(["--name", "Curious-Flory", "--program", "claude-code", "--model", "m"])
    assert result.returncode == 0, result.stderr
    assert "PICKER_CALLED" not in result.stderr, "explicit name must be honoured"
    assert "does not end in a known scientist" in result.stderr
    assert "portrait" in result.stderr
    assert result.stdout.strip() == "Curious-Flory"


def test_off_list_name_is_rejected_in_strict_mode():
    result = _run(
        ["--name", "Curious-Flory", "--program", "claude-code", "--model", "m"],
        {"AGENTSTACK_STRICT_AGENT_NAMES": "1"},
    )
    assert result.returncode != 0
    assert "refusing off-list name" in result.stderr
    assert result.stdout.strip() == ""


def test_on_list_name_passes_silently():
    result = _run(["--name", "Curious-Curie", "--program", "claude-code", "--model", "m"])
    assert result.returncode == 0, result.stderr
    assert "does not end in a known scientist" not in result.stderr
    assert result.stdout.strip() == "Curious-Curie"


def test_server_substitution_is_reported_and_returned_name_is_authoritative():
    result = _run(
        ["--name", "Zesty-Einstein", "--program", "claude-code", "--model", "m"],
        {"FAKE_RETURNED_NAME": "MossyEagle"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "MossyEagle"
    assert "changed requested identity 'Zesty-Einstein' to 'MossyEagle'" in result.stderr
    assert "tmux session must use the server-returned name" in result.stderr


def test_delegate_skill_no_longer_tells_callers_to_hand_pick_a_name():
    skill = (_ROOT / "skills" / "delegate" / "SKILL.md").read_text(encoding="utf-8")
    assert '--name "<child-name>"' not in skill, "skill still passes a hand-written name"
    assert "Omit `--name`" in skill


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
