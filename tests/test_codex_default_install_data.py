"""Exercise only the isolated systemd renderer, not installation or services."""
from pathlib import Path
import os
import subprocess

from test_install_launchd_placeholders import _render


def test_systemd_writer_treats_default_values_as_data(tmp_path):
    marker = tmp_path / 'executed'
    value = f'"\n__import__("pathlib").Path("{marker}").touch()\n#'
    output = _render(tmp_path, 'systemd', None, None, {
        'CODEX_DEFAULT_MODEL_SETTING': value,
        'CODEX_DEFAULT_EFFORT_SETTING': value,
    }).read_text()
    assert not marker.exists()
    assert str(marker) in output


def test_preflight_reports_invalid_values_without_executing_them(tmp_path):
    source = (Path(__file__).resolve().parents[1] / "scripts/install.sh").read_text()
    start = source.index("preflight_codex_defaults() {")
    end = source.index("\n}", start) + 2
    function = source[start:end]
    marker = tmp_path / "executed"
    env = {**os.environ, "CODEX_DEFAULT_MODEL_SETTING": f'"\n$(touch {marker})',
           "CODEX_DEFAULT_EFFORT_SETTING": "high\n"}
    result = subprocess.run(["/bin/bash", "-c", 'preflight_error() { printf "%s\n" "$1"; }\n' + function + '\npreflight_codex_defaults'], env=env, text=True, capture_output=True, check=True)
    assert "invalid AGENTSTACK_CODEX_DEFAULT_MODEL" in result.stdout
    assert "invalid AGENTSTACK_CODEX_DEFAULT_EFFORT" in result.stdout
    assert not marker.exists()
