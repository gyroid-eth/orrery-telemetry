import hashlib
import io
import json
from pathlib import Path
import plistlib
import sys
import tarfile
from types import SimpleNamespace
import zipfile

import pytest

from hooks import screen_state
from scripts.lib import cua_driver_install as cua


@pytest.fixture
def installer(monkeypatch, tmp_path):
    """Local signed-release fixtures for the consent and publication boundaries."""
    target = cua.Target("darwin-universal", tmp_path / "home/.cua-driver/config.json", ())
    install = tmp_path / "install"
    link = install / "cua-driver/current"
    pins = Path("integrations/cua-driver/runtime-pins.json")
    calls = []
    monkeypatch.setattr(cua, "local_target", lambda: target)
    monkeypatch.delenv("AGENTSTACK_CUA_DRIVER", raising=False)
    monkeypatch.setattr(cua.shutil, "which", lambda name: None)
    monkeypatch.setattr(cua.sys, "stdin", SimpleNamespace(isatty=lambda: True))

    def release(version, *, bad_hash=False):
        content = io.BytesIO()
        with tarfile.open(fileobj=content, mode="w:gz") as archive:
            prefix = f"cua-driver-rs-{version}-darwin-universal/"
            binary = tarfile.TarInfo(prefix + str(cua.MAC_BINARY))
            body = f"#!/bin/sh\nprintf 'cua-driver {version}\\n'\n".encode()
            binary.size = len(body)
            binary.mode = 0o755
            archive.addfile(binary, io.BytesIO(body))
            info = tarfile.TarInfo(prefix + "CuaDriver.app/Contents/Info.plist")
            metadata = plistlib.dumps({"CFBundleShortVersionString": version})
            info.size = len(metadata)
            archive.addfile(info, io.BytesIO(metadata))
        data = content.getvalue()
        digest = "0" * 64 if bad_hash else hashlib.sha256(data).hexdigest()
        monkeypatch.setattr(cua, "read_pins", lambda _: (
            version, {target.key: {"url": cua.asset_url(version, target.key), "sha256": digest}},
        ))

        def download(url, path):
            calls.append(("download", url))
            path.write_bytes(data)

        monkeypatch.setattr(cua, "download", download)

    real_checked = cua.checked

    def checked(cmd, **kwargs):
        if cmd[0] in ("/usr/bin/codesign", "/usr/sbin/spctl"):
            calls.append(("signature", cmd[0]))
            return SimpleNamespace(stdout="", stderr="source=Notarized Developer ID")
        return real_checked(cmd, **kwargs)

    monkeypatch.setattr(cua, "checked", checked)
    release("0.55.0")
    return SimpleNamespace(target=target, install=install, link=link, pins=pins,
                           calls=calls, release=release)


@pytest.mark.parametrize("answer", ["", "yes"])
def test_accepted_install_verifies_and_publishes(monkeypatch, installer, answer):
    def consent(prompt):
        assert "推奨" in prompt and "[Y/n]" in prompt
        assert installer.calls == []
        return answer

    monkeypatch.setattr("builtins.input", consent)
    cua.offer(installer.install, installer.pins)

    assert installer.link.is_symlink()
    assert installer.link.resolve().name == "cua-driver"
    assert cua.preferences_ok(installer.target.preferences)
    assert [action for action, _ in installer.calls] == ["download", "signature", "signature"]
    monkeypatch.setenv("AGENTSTACK_HOME", str(installer.install))
    monkeypatch.setattr(screen_state, "_python", lambda _: sys.executable)
    relay = screen_state.definition(installer.install / "runtime", "fixture", "WarmFixture", "a" * 32, "view",
                                    origin="http://127.0.0.1:18845")
    assert relay["env"]["AGENTSTACK_CUA_DRIVER"] == str(installer.link.resolve())


@pytest.mark.parametrize("mode", ["no", "noninteractive", "eof", "dry-run"])
def test_skipped_install_never_downloads_or_writes(monkeypatch, installer, capsys, mode):
    def consent(_):
        if mode == "eof":
            raise EOFError
        assert mode == "no"
        return "n"

    monkeypatch.setattr("builtins.input", consent)
    if mode == "noninteractive":
        monkeypatch.setattr(cua.sys.stdin, "isatty", lambda: False)
    cua.offer(installer.install, installer.pins, dry_run=mode == "dry-run")

    assert installer.calls == [] and not installer.install.exists()
    output = capsys.readouterr().out
    assert ("plan:" if mode == "dry-run" else "agentstack-install-cua-driver") in output


@pytest.mark.parametrize("failure", ["SHA256", "signature", "notarization"])
def test_verification_failure_never_publishes(monkeypatch, installer, failure):
    installer.release("0.55.0", bad_hash=failure == "SHA256")
    monkeypatch.setattr("builtins.input", lambda _: "")
    if failure != "SHA256":
        def rejected(command, **kwargs):
            if failure == "signature":
                raise cua.InstallError("signature mismatch")
            return SimpleNamespace(stdout="", stderr="source=Developer ID")

        monkeypatch.setattr(cua, "checked", rejected)

    match = "Notarized Developer ID" if failure == "notarization" else failure
    with pytest.raises(cua.InstallError, match=match):
        cua.offer(installer.install, installer.pins)
    assert not installer.link.is_symlink() and not installer.target.preferences.exists()
    assert not list((installer.link.parent / "releases").iterdir())


def test_existing_manual_driver_is_left_in_place(monkeypatch, installer, tmp_path, capsys):
    binary = tmp_path / "manual/cua-driver"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nprintf 'cua-driver 0.54.0\\n'\n")
    binary.chmod(0o755)
    monkeypatch.setenv("AGENTSTACK_CUA_DRIVER", str(binary))
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("manual driver must not prompt"))

    cua.offer(installer.install, installer.pins)

    assert "ORRERY expects 0.55.0" in capsys.readouterr().out
    assert installer.calls == [] and not installer.target.preferences.exists()
    assert "0.54.0" in binary.read_text()


def test_upgrade_requires_separate_consent_and_preserves_preferences(monkeypatch, installer, capsys):
    monkeypatch.setattr("builtins.input", lambda _: "")
    cua.offer(installer.install, installer.pins)
    original = installer.link.resolve()
    installer.target.preferences.write_text(json.dumps({
        "telemetry_enabled": True, "update_check_enabled": True, "max_image_dimension": 480,
    }))
    cua.offer(installer.install, installer.pins, check=True)
    assert "privacy preferences are incomplete" in capsys.readouterr().err

    installer.release("0.56.0")
    installer.calls.clear()
    cua.offer(installer.install, installer.pins)
    assert installer.calls == [] and installer.link.resolve() == original

    cua.offer(installer.install, installer.pins, upgrade=True)
    assert installer.link.resolve() != original and original.exists()
    assert json.loads(installer.target.preferences.read_text()) == {
        "telemetry_enabled": False, "update_check_enabled": False, "max_image_dimension": 480,
    }
    cua.offer(installer.install, installer.pins, check=True)
    assert capsys.readouterr().err == ""


def test_wsl_verifies_windows_signatures_and_installs_on_the_windows_side(monkeypatch, tmp_path):
    appdata = tmp_path / "Windows AppData"
    prefs = tmp_path / "Windows profile/.cua-driver/config.json"
    target = cua.Target("windows-x86_64", prefs, (), appdata)
    install = tmp_path / "WSL install"
    link = install / "cua-driver/current"
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as package:
        package.writestr("cua-driver.exe", b"windows executable")
        package.writestr("cua-driver-uia.exe", b"uia executable")
        package.writestr("cua_driver_sdk.dll", b"sdk")
        package.writestr("cua_driver_node_runtime.node", b"node runtime")
    data = content.getvalue()
    url = cua.asset_url("0.55.0", target.key)
    asset = {"url": url, "sha256": hashlib.sha256(data).hexdigest()}
    monkeypatch.setattr(cua, "download", lambda url, path: path.write_bytes(data))
    monkeypatch.setattr(cua, "wsl_path", lambda path, direction: "C:\\Fixture\\" + path.name)
    scripts = []

    def reject_signature(script):
        scripts.append(script)
        raise cua.InstallError("unexpected publisher")

    monkeypatch.setattr(cua, "powershell", reject_signature)
    with cua.install_lock(link.parent), pytest.raises(cua.InstallError, match="unexpected publisher"):
        cua.install_release(link, target, "0.55.0", asset)
    assert not link.is_symlink() and not prefs.exists()
    assert "Get-AuthenticodeSignature -LiteralPath" in scripts[0]
    assert all(f"C:\\Fixture\\{name}" in scripts[0] for name in (
        "cua-driver.exe", "cua-driver-uia.exe", "cua_driver_sdk.dll", "cua_driver_node_runtime.node",
    ))

    monkeypatch.setattr(cua, "powershell", scripts.append)
    with cua.install_lock(link.parent):
        binary = cua.install_release(link, target, "0.55.0", asset)
    assert binary.is_relative_to(appdata) and link.resolve() == binary.resolve()
    assert cua.preferences_ok(prefs)


def test_linux_verifies_the_archive_with_the_fixed_workflow_identity(monkeypatch, tmp_path):
    target = cua.Target("linux-arm64", tmp_path / "home/.cua-driver/config.json", ())
    link = tmp_path / "install/cua-driver/current"
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w:gz") as package:
        binary = tarfile.TarInfo("cua-driver")
        binary.mode = 0o755
        package.addfile(binary, io.BytesIO())
    data = content.getvalue()
    url = cua.asset_url("0.55.0", target.key)
    asset = {"url": url, "sha256": hashlib.sha256(data).hexdigest()}
    downloads = []

    def download(url, path):
        downloads.append(url)
        path.write_bytes(b"sigstore bundle" if url.endswith(".json") else data)

    monkeypatch.setattr(cua, "download", download)
    monkeypatch.setattr(cua.shutil, "which", lambda _: None)
    with pytest.raises(cua.InstallError, match="install cosign"):
        cua.install_release(link, target, "0.55.0", asset)
    assert downloads == []

    monkeypatch.setattr(cua.shutil, "which", lambda _: "/usr/local/bin/cosign")
    commands = []
    monkeypatch.setattr(cua, "checked", commands.append)
    with cua.install_lock(link.parent):
        installed = cua.install_release(link, target, "0.55.0", asset)
    assert downloads == [url, url + ".sigstore.json"]
    assert commands[0][:3] == ["cosign", "verify-blob", "--bundle"]
    assert (cua.REPOSITORY + "/.github/workflows/cd-rust-cua-driver.yml@refs/tags/cua-driver-rs-v0.55.0"
            in commands[0])
    assert link.resolve() == installed.resolve() and cua.preferences_ok(target.preferences)
