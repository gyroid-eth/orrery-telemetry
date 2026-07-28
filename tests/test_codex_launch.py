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
    ["", "  gpt-5.5 medium · Context 100% left · ~/Syncthing/<vault-directory>", ""]
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
    start = text.index(f"{func}() {{")
    end = text.index("\n}\n", start) + len("\n}\n")
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
