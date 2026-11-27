"""Run the installer's skill integration and real uninstall in fake homes."""
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
INSTALLER = (ROOT / "scripts/install.sh").read_text(encoding="utf-8")


def _function(name):
    if name == "write_manifest":
        # Its Python heredoc contains unindented closing braces.
        body = INSTALLER.split("write_manifest() {", 1)[1].split("\nmain() {", 1)[0]
        return "write_manifest() {" + body
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", INSTALLER, re.M | re.S)
    assert match, name
    return match.group(0)


def _install(home, *, codex_home=None, tier="tier1", dry_run=False):
    install_dir = home / ".agentstack"
    (install_dir / "runtime").mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "skills", install_dir / "skills", dirs_exist_ok=True)
    functions = "\n".join(_function(name) for name in (
        "plan", "run", "symlink_points_to", "install_skill_links_for",
        "install_claude_skill_links", "install_codex_skill_links", "install_agy_skills", "write_manifest",
    ))
    # The manifest has unrelated service metadata. Initialize its variables to
    # empty values, then provide only the isolated paths and required JSON.
    variables = sorted(set(re.findall(r"\$(?:\{)?([A-Z][A-Z_0-9]*)", functions)))
    values = {key: "" for key in variables}
    values.update({
        "PYTHON_BIN": sys.executable, "REPO_ROOT": str(ROOT), "SCRIPT_DIR": str(ROOT / "scripts"),
        "INSTALL_DIR": str(install_dir), "SKILLS_DIR": str(install_dir / "skills"),
        "RUNTIME_DIR": str(install_dir / "runtime"), "TIER": tier,
        "DRY_RUN": "true" if dry_run else "false",
        "MANIFEST": str(install_dir / "install-state.json"),
        "ENV_FILE": str(install_dir / "env.sh"),
        "SAFE_MERGE_RESULT_FILE": str(install_dir / "runtime/settings.json"),
        "MCP_MERGE_RESULT_FILE": str(install_dir / "runtime/mcp.json"),
        "AGENT_MAIL_NAME_CAPABILITY_JSON": "{}",
    })
    script = "\n".join([
        "set -euo pipefail",
        *[f"{key}={shlex.quote(value)}" for key, value in values.items()],
        # Use the actual installer declarations, including CODEX_HOME fallback.
        *[re.search(rf'^{key}=.*$', INSTALLER, re.M).group(0)
          for key in ("CLAUDE_SKILLS_DIR", "CODEX_SKILLS_DIR", "AGY_SKILLS_MANIFEST")],
        'warn() { printf "warning: %s\\n" "$*" >&2; }',
        'say() { printf "%s\\n" "$*"; }',
        functions, "install_claude_skill_links", "install_codex_skill_links",
        "install_agy_skills",
        'write_manifest "" ""',
    ])
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("AGENTSTACK_") and key not in ("CODEX_HOME", "HOME")}
    env["HOME"] = str(home)
    if codex_home is not None:
        env["CODEX_HOME"] = str(codex_home)
    result = subprocess.run(["/bin/bash", "-c", script], env=env,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return result, install_dir


def _manifest(install_dir):
    return json.loads((install_dir / "install-state.json").read_text())


def _uninstall(home, install_dir):
    # No service records are created by this fixture; uninstall has no daemon
    # to stop and receives an explicit temporary install directory.
    return subprocess.run(
        ["/bin/bash", str(ROOT / "scripts/uninstall.sh"), "--install-dir", str(install_dir)],
        env={"HOME": str(home), "PATH": os.environ["PATH"]},
        capture_output=True, text=True, check=True,
    )


@pytest.mark.parametrize("codex_home_name", [None, "custom codex", 'codex"home', r"codex	home"])
def test_install_reinstall_and_uninstall_codex_links(tmp_path, codex_home_name):
    home = tmp_path / "home"
    codex_home = tmp_path / codex_home_name if codex_home_name is not None else None
    skills_dir = (codex_home or home / ".codex") / "skills"
    _, install_dir = _install(home, codex_home=codex_home)
    manifest = _manifest(install_dir)
    expected = [{"path": str(skills_dir / name),
                 "target": str(install_dir / "skills" / name)}
                for name in ("delegate", "log")]
    assert [record for record in manifest["skill_links"]
            if pathlib.Path(record["path"]).parent == skills_dir] == expected
    for record in expected:
        link = pathlib.Path(record["path"])
        assert link.is_symlink() and str(link.resolve()) == record["target"]
        assert record["path"] in manifest["owned_files"]
    if codex_home is not None:
        assert not (home / ".codex/skills").exists()
    result, _ = _install(home, codex_home=codex_home)
    assert "reuse Codex skill link" in result.stdout
    assert _manifest(install_dir)["skill_links"] == manifest["skill_links"]
    _uninstall(home, install_dir)
    assert all(not pathlib.Path(record["path"]).is_symlink() for record in expected)


@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "dangling-symlink"])
def test_user_skill_is_preserved_and_not_owned(tmp_path, kind):
    home = tmp_path / "home"
    user_skill = home / ".codex/skills/delegate"
    user_skill.parent.mkdir(parents=True)
    if kind == "directory":
        user_skill.mkdir()
        (user_skill / "SKILL.md").write_text("user skill")
    elif kind == "file":
        user_skill.write_text("user skill")
    else:
        target = home / "user-skills"
        if kind == "symlink":
            target.mkdir()
        user_skill.symlink_to(target)
    result, install_dir = _install(home)
    assert "Codex skill 'delegate' already exists; leaving it untouched" in result.stderr
    manifest = _manifest(install_dir)
    assert str(user_skill) not in manifest["owned_files"]
    assert all(record["path"] != str(user_skill) for record in manifest["skill_links"])
    _uninstall(home, install_dir)
    if "symlink" in kind:
        assert user_skill.is_symlink() and os.readlink(user_skill) == str(home / "user-skills")
    else:
        assert (user_skill / "SKILL.md" if kind == "directory" else user_skill).read_text() == "user skill"


@pytest.mark.parametrize("kind", ["retargeted", "directory"])
def test_uninstall_keeps_modified_owned_codex_link(tmp_path, kind):
    home = tmp_path / "home"
    _, install_dir = _install(home)
    link = home / ".codex/skills/log"
    link.unlink()
    if kind == "retargeted":
        link.symlink_to(home / "other-log")
    else:
        link.mkdir()
    result = _uninstall(home, install_dir)
    if kind == "retargeted":
        assert "kept retargeted skill link" in result.stderr
        assert link.is_symlink()
    else:
        assert "kept modified skill path" in result.stderr
        assert link.is_dir()


@pytest.mark.parametrize("tier,dry_run", [("tier0", False), ("tier1", True)])
def test_dashboard_only_and_dry_run_do_not_create_skill_links(tmp_path, tier, dry_run):
    home = tmp_path / "home"
    _install(home, tier=tier, dry_run=dry_run)
    assert not (home / ".codex/skills").exists()
    assert not (home / ".claude/skills").exists()
    assert not (home / ".gemini/config/skills.json").exists()


def test_default_skill_links_match_manifest_sample(tmp_path):
    home = tmp_path / "home"
    _, install_dir = _install(home)
    sample = json.loads((ROOT / "scripts/install-state.sample.json").read_text())
    sample_home = pathlib.Path(sample["install_dir"]).parent
    normalized = [{key: value.replace(str(home), str(sample_home))
                   for key, value in record.items()}
                  for record in _manifest(install_dir)["skill_links"]]
    assert normalized == sample["skill_links"]


def test_antigravity_manifest_upgrade_and_uninstall_preserve_user_entries(tmp_path):
    home = tmp_path / "home"
    config = home / ".gemini/config"
    user_skill = config / "skills/log/SKILL.md"
    user_skill.parent.mkdir(parents=True)
    user_skill.write_text("personal log")
    manifest = config / "skills.json"
    personal = {"entries": [{"path": "my-skills"}], "exclude": ["some-skill"],
                "inherits": [{"path": "/personal/skills.json"}]}
    manifest.write_text(json.dumps(personal))
    _, install_dir = _install(home)
    entry = {"path": str(install_dir / "skills"), "include_only": ["delegate", "log"]}
    merged = {**personal, "entries": [entry, *personal["entries"]]}
    assert json.loads(manifest.read_bytes()) == merged
    assert user_skill.read_text() == "personal log"
    _install(home)
    assert json.loads(manifest.read_bytes()) == merged
    merged["entries"].append({"path": "/another/user/directory"})
    manifest.write_text(json.dumps(merged))
    _uninstall(home, install_dir)
    assert json.loads(manifest.read_bytes()) == {
        **personal, "entries": [*personal["entries"], {"path": "/another/user/directory"}],
    }
    assert user_skill.read_text() == "personal log"


def test_antigravity_manifest_respects_user_skills_on_install_and_update(tmp_path):
    home = tmp_path / "home"
    config = home / ".gemini/config"
    user_log = config / "personal/log/SKILL.md"
    user_log.parent.mkdir(parents=True)
    user_log.write_text("personal log")
    inherited = config / "other-skills.json"
    inherited.write_text(json.dumps({"entries": [{"path": "personal"}]}))
    personal = {"inherits": [{"path": str(inherited)}]}
    manifest = config / "skills.json"
    manifest.write_text(json.dumps(personal))
    _, install_dir = _install(home)
    assert json.loads(manifest.read_bytes()) == {
        **personal, "entries": [{"path": str(install_dir / "skills"), "include_only": ["delegate"]}],
    }
    user_delegate = config / "personal/delegate/SKILL.md"
    user_delegate.parent.mkdir()
    user_delegate.write_text("personal delegate")
    _install(home)
    assert json.loads(manifest.read_bytes()) == personal
    assert user_log.read_text() == "personal log"
    assert user_delegate.read_text() == "personal delegate"


@pytest.mark.parametrize("kind", ["invalid", "existing-entry", "symlink"])
def test_antigravity_preserves_unowned_manifest(tmp_path, kind):
    home = tmp_path / "home"
    manifest = home / ".gemini/config/skills.json"
    manifest.parent.mkdir(parents=True)
    original = '{broken}'
    if kind == "symlink":
        target = home / "personal.json"
        target.write_text(original)
        manifest.symlink_to(target)
    elif kind == "existing-entry":
        original = json.dumps({"entries": [
            {"path": str(home / ".agentstack/skills"), "include_only": ["delegate", "log"]},
        ]})
        manifest.write_text(original)
    else:
        manifest.write_text(original)
    _, install_dir = _install(home)
    _uninstall(home, install_dir)
    assert manifest.read_text() == original
    assert manifest.is_symlink() == (kind == "symlink")
