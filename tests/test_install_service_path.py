"""The service PATH must keep a Node-installed `codex` with its own runtime.

A Node wrapper resolves its platform package through the first `node` on PATH.
Under the service's minimal PATH that is a different Node than the one the
wrapper was installed under, so `codex app-server` dies and the dashboard
reports no Codex usage at all.
"""

from __future__ import annotations

import pathlib
import re
import subprocess


ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts" / "install.sh"
DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def _codex_path_block() -> str:
    text = INSTALLER.read_text(encoding="utf-8")
    match = re.search(r'^if \[\[ -n "\$CODEX_BIN_SETTING" \]\]; then\n.*?^fi\n',
                      text, re.DOTALL | re.MULTILINE)
    assert match, "codex PATH block missing from the installer"
    return match.group(0)


def _resolve(codex_bin: str, path_value: str = DEFAULT_PATH) -> str:
    script = "\n".join([
        "set -euo pipefail",
        f'CODEX_BIN_SETTING={codex_bin!r}',
        f'PATH_VALUE={path_value!r}',
        _codex_path_block(),
        'printf "%s" "$PATH_VALUE"',
    ])
    result = subprocess.run(["/bin/bash", "-c", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_a_codex_outside_the_service_path_brings_its_directory_along(tmp_path):
    codex = tmp_path / "nodebrew" / "bin" / "codex"
    codex.parent.mkdir(parents=True)
    codex.write_text("#!/bin/sh\n", encoding="utf-8")

    resolved = _resolve(str(codex))

    assert resolved.split(":")[0] == str(codex.parent)
    assert resolved.endswith(DEFAULT_PATH)


def test_a_codex_already_on_the_service_path_does_not_repeat_it(tmp_path):
    resolved = _resolve("/usr/local/bin/codex")

    assert resolved == DEFAULT_PATH


def test_no_codex_leaves_the_service_path_alone():
    assert _resolve("") == DEFAULT_PATH
