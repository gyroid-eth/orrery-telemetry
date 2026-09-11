from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parent.parent
SYSTEM_BASH = pathlib.Path("/bin/bash")
GEMINI_SHELL_ENTRYPOINTS = (
    ROOT / "bin" / "agent-start-gemini",
    ROOT / "bin" / "agentstack-gemini-bootstrap",
    ROOT / "bin" / "agentstack-gemini-setup",
    ROOT / "bin" / "agentstack-gemini-mcp",
    ROOT / "hooks" / "spawn_gemini_child.sh",
    ROOT / "hooks" / "spawn_gemini_preregistered.sh",
    ROOT / "scripts" / "install-gemini-provider.sh",
)


def _system_bash_version() -> tuple[int, int]:
    result = subprocess.run(
        [
            str(SYSTEM_BASH),
            "-lc",
            "printf '%s %s\\n' \"${BASH_VERSINFO[0]}\" \"${BASH_VERSINFO[1]}\"",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    major, minor = result.stdout.strip().split()
    return int(major), int(minor)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS system Bash compatibility gate")
def test_gemini_shell_entrypoints_parse_with_macos_bash_32() -> None:
    assert SYSTEM_BASH.exists(), "/bin/bash is required on supported macOS installs"
    version = _system_bash_version()
    if version != (3, 2):
        pytest.skip(f"macOS system Bash is {version[0]}.{version[1]}, not 3.2")

    for script in GEMINI_SHELL_ENTRYPOINTS:
        assert script.is_file(), f"missing Gemini shell entrypoint: {script.relative_to(ROOT)}"
        result = subprocess.run(
            [str(SYSTEM_BASH), "-n", str(script)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"{script.relative_to(ROOT)} does not parse with macOS Bash 3.2:\n"
            f"{result.stderr}"
        )
