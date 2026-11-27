"""Consent, verification and publication of the ORRERY-managed Cua Driver."""

import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import plistlib
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import urlopen
import zipfile


REPOSITORY = "https://github.com/trycua/cua"
RELEASES = REPOSITORY + "/releases/download"
TARGETS = {
    "darwin-universal": ".tar.gz",
    "linux-arm64": "-binary.tar.gz",
    "linux-x86_64": "-binary.tar.gz",
    "windows-arm64": "-binary.zip",
    "windows-x86_64": "-binary.zip",
}
APPLE_TEAM = "YCK386LBJ7"
MAC_BINARY = Path("CuaDriver.app/Contents/MacOS/cua-driver")
PREFERENCES = {"telemetry_enabled": False, "update_check_enabled": False}
OPT_OUTS = {
    "CUA_DRIVER_RS_TELEMETRY_ENABLED": "false",
    "CUA_DRIVER_RS_UPDATE_CHECK": "false",
}
HELP = "https://github.com/gyroid-eth/orrery/blob/master/docs/en/computer-use.md"


class InstallError(ValueError):
    pass


@dataclass(frozen=True)
class Target:
    key: str
    preferences: Path
    existing: tuple[Path, ...]
    windows_appdata: Path | None = None

    @property
    def os(self):
        return self.key.split("-")[0]

    @property
    def binary(self):
        return "cua-driver.exe" if self.os == "windows" else "cua-driver"


def release_tag(version):
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise InstallError("an explicit Cua Driver version is required (for example 0.55.0)")
    return "cua-driver-rs-v" + version


def asset_url(version, key):
    return f"{RELEASES}/{release_tag(version)}/cua-driver-rs-{version}-{key}{TARGETS[key]}"


def read_pins(path):
    pins = json.loads(path.read_text(encoding="utf-8"))
    version = pins["version"]
    release_tag(version)
    assets = pins["assets"]
    if assets.keys() != TARGETS.keys():
        raise InstallError("Cua Driver pins must name every supported platform and architecture")
    for key, asset in assets.items():
        if asset["url"] != asset_url(version, key) or not re.fullmatch(r"[0-9a-f]{64}", asset["sha256"]):
            raise InstallError(f"invalid Cua Driver release pin for {key}")
        if key.startswith("linux-") and not re.fullmatch(r"[0-9a-f]{64}", asset["sigstore_sha256"]):
            raise InstallError(f"invalid Cua Driver signing bundle pin for {key}")
    return version, assets


def checked(command, *, env=None, timeout=45):
    result = subprocess.run(command, env=env, timeout=timeout, stdin=subprocess.DEVNULL,
                            capture_output=True, encoding="utf-8", errors="replace")
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise InstallError(f"{Path(command[0]).name}: {detail}")
    return result


def powershell(script):
    executable = shutil.which("powershell.exe")
    if not executable:
        raise InstallError("Windows PowerShell is unavailable through WSL interop")
    prelude = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
    encoded = base64.b64encode((prelude + script).encode("utf-16-le")).decode("ascii")
    return checked([executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]).stdout


def wsl_path(value, direction):
    result = checked(["wslpath", direction, str(value)]).stdout.rstrip("\r\n")
    if not result or (direction == "-u" and not Path(result).is_absolute()):
        raise InstallError("could not resolve the Windows path through WSL interop")
    return result


def local_target():
    home = Path.home()
    architecture = {"aarch64": "arm64", "arm64": "arm64",
                    "amd64": "x86_64", "x86_64": "x86_64"}.get(platform.machine().lower())
    if sys.platform == "darwin" and architecture:
        return Target("darwin-universal", home / ".cua-driver/config.json", (
            home / "Applications" / MAC_BINARY, Path("/Applications") / MAC_BINARY,
            home / ".local/bin/cua-driver",
        ))
    if sys.platform != "linux" or not architecture:
        raise InstallError("Cua Driver installation supports macOS, WSL and Linux on arm64 or x86_64")
    if os.environ.get("WSL_DISTRO_NAME") or "microsoft" in platform.release().lower():
        context = json.loads(powershell(
            "$cua=Get-Command cua-driver.exe -CommandType Application -ErrorAction SilentlyContinue; "
            "$driverHome=$env:HOME; if (-not $driverHome) {$driverHome=$env:USERPROFILE}; "
            "[ordered]@{appdata=[Environment]::GetFolderPath('LocalApplicationData'); "
            "home=$driverHome; exe=$cua.Source}|ConvertTo-Json -Compress"
        ))
        appdata = Path(wsl_path(context["appdata"], "-u"))
        prefs = Path(wsl_path(context["home"], "-u")) / ".cua-driver/config.json"
        paths = (appdata / "Programs/Cua/cua-driver/bin/cua-driver.exe",
                 appdata / "Programs/trycua/cua-driver-rs/bin/cua-driver.exe")
        if context["exe"]:
            paths = (Path(wsl_path(context["exe"], "-u")), *paths)
        return Target(f"windows-{architecture}", prefs, paths, appdata)
    return Target(f"linux-{architecture}", home / ".cua-driver/config.json",
                  (home / ".local/bin/cua-driver",))


def executable(path):
    return path.is_file() and os.access(path, os.X_OK)


def existing_driver(target, link):
    configured = os.environ.get("AGENTSTACK_CUA_DRIVER")
    if configured:
        path = Path(configured)
        if not path.is_absolute() or not executable(path):
            raise InstallError("AGENTSTACK_CUA_DRIVER must point to an existing executable by absolute path")
        return path, False
    if link.is_symlink() and executable(link):
        return link.resolve(), True
    found = shutil.which(target.binary)
    if found:
        return Path(found), False
    return next(((path, False) for path in target.existing if executable(path)), (None, False))


def driver_version(path, target):
    env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "WSL_INTEROP", "WSL_DISTRO_NAME")
           if key in os.environ}
    if target.os != "windows":
        env.update(OPT_OUTS)
    line = checked([str(path), "--version"], env=env, timeout=10).stdout.strip()
    match = re.fullmatch(r"(?:cua-driver(?:-rs)? )?([0-9]+\.[0-9]+\.[0-9]+(?:[-+][\w.+-]+)?)", line)
    if not match:
        raise InstallError("could not determine the existing Cua Driver version")
    return match[1]


def download(url, path):
    with urlopen(url, timeout=45) as response, path.open("wb") as output:
        shutil.copyfileobj(response, output)


def verify_linux(archive, asset, version):
    bundle = archive.with_name(archive.name + ".sigstore.json")
    download(asset["url"] + ".sigstore.json", bundle)
    with bundle.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != asset["sigstore_sha256"]:
            raise InstallError(f"Cua Driver SHA256 mismatch for {bundle.name}")
    checked(["cosign", "verify-blob", "--bundle", str(bundle),
             "--certificate-identity", f"{REPOSITORY}/.github/workflows/cd-rust-cua-driver.yml@refs/tags/{release_tag(version)}",
             "--certificate-oidc-issuer", "https://token.actions.githubusercontent.com", str(archive)])


def extract(archive, dest):
    dest.mkdir()
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as package:
            for member in package.infolist():
                name = PurePosixPath(member.filename)
                if (name.is_absolute() or ".." in name.parts or "\\" in member.filename
                        or ":" in member.filename or stat.S_ISLNK(member.external_attr >> 16)):
                    raise InstallError("unexpected path in the Cua Driver ZIP")
            package.extractall(dest)
    else:
        with tarfile.open(archive, "r:gz") as package:
            package.extractall(dest, filter="data")


def verified_payload(dest, target, version):
    if target.os == "darwin":
        app = dest / f"cua-driver-rs-{version}-darwin-universal/CuaDriver.app"
        requirement = f'anchor apple generic and certificate leaf[subject.OU] = "{APPLE_TEAM}"'
        checked(["/usr/bin/codesign", "--verify", "--deep", "--strict", "-R=" + requirement, str(app)])
        result = checked(["/usr/sbin/spctl", "--assess", "--type", "execute", "--verbose=4", str(app)])
        if "source=Notarized Developer ID" not in result.stderr + result.stdout:
            raise InstallError("CuaDriver.app is missing Gatekeeper's Notarized Developer ID verdict")
        with (app / "Contents/Info.plist").open("rb") as info:
            if plistlib.load(info).get("CFBundleShortVersionString") != version:
                raise InstallError(f"CuaDriver.app must match the pinned version {version}")
        payload = dest / "verified"
        payload.mkdir()
        app.rename(payload / app.name)
        binary = payload / MAC_BINARY
    else:
        payload = dest
        binary = payload / target.binary
        if target.os == "windows":
            signed = sorted(p for p in payload.iterdir() if p.suffix.lower() in {".exe", ".dll", ".node"})
            for path in signed:
                if path.suffix.lower() == ".exe":
                    path.chmod(path.stat().st_mode | stat.S_IXUSR)
            files = ",".join("'" + wsl_path(path, "-w").replace("'", "''") + "'" for path in signed)
            powershell(
                f"foreach($file in @({files})) {{"
                "$signature=Get-AuthenticodeSignature -LiteralPath $file; "
                r"""if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch '(?:^|, ?)CN="?Cua AI, Inc\."?(?:,|$)') {"""
                'throw "Unexpected Authenticode signer or signature state for $file ($($signature.Status))"'
                "}}"
            )
    if not executable(binary):
        raise InstallError(f"verified archive is missing an executable {target.binary}")
    return payload, binary.relative_to(payload)


def preferences_ok(path):
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    return isinstance(saved, dict) and all(saved.get(key) is False for key in PREFERENCES)


def persist_preferences(path):
    """Merge the two preferences from the pinned driver's config format."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise InstallError(f"resolve the Cua Driver preferences symlink before installing: {path}")
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        saved = {}
    if not isinstance(saved, dict):
        raise InstallError(f"Cua Driver preferences are not a JSON object: {path}")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temp:
        temporary = Path(temp.name)
        try:
            json.dump({**saved, **PREFERENCES}, temp, indent=2)
            temp.write("\n")
            temp.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    if not preferences_ok(path):
        raise InstallError("could not verify Cua Driver telemetry and update preferences")


@contextmanager
def install_lock(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / ".install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def install_release(link, target, version, asset):
    url = asset["url"]
    if target.os == "linux" and not shutil.which("cosign"):
        raise InstallError("install cosign to verify the Linux release signature, then retry")
    releases = (target.windows_appdata / "Programs/ORRERY/CuaDriver" if target.windows_appdata
                else link.parent / "releases")
    releases.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Keep previous generations in place for running processes. An interrupted
    # publication leaves an unselected directory that cannot block a retry.
    destination = releases / f"{version}-{target.key}-{secrets.token_hex(8)}"
    with tempfile.TemporaryDirectory(prefix=".cua-stage-", dir=releases) as temporary:
        stage = Path(temporary)
        archive = stage / url.rsplit("/", 1)[1]
        download(url, archive)
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != asset["sha256"]:
            raise InstallError(f"Cua Driver SHA256 mismatch for {archive.name}")
        if target.os == "linux":
            verify_linux(archive, asset, version)
        unpacked = stage / "payload"
        extract(archive, unpacked)
        payload, relative_binary = verified_payload(unpacked, target, version)
        persist_preferences(target.preferences)
        payload.rename(destination)
        try:
            with tempfile.TemporaryDirectory(prefix=".publish-", dir=link.parent) as publication:
                pending = Path(publication) / "current"
                pending.symlink_to(destination / relative_binary)
                pending.replace(link)
        except OSError:
            shutil.rmtree(destination)
            raise
    return destination / relative_binary


def offer(install, pins, *, dry_run=False, upgrade=False, check=False):
    link = install / "cua-driver/current"
    hint = f"Computer use を使う場合は {shlex.quote(str(install / 'bin/agentstack-install-cua-driver'))} を実行してください。"
    if dry_run:
        print("plan: offer Cua Driver installation if missing in an interactive install")
        return
    if check and not link.is_symlink():
        return
    target = local_target()
    if check:
        if not executable(link):
            print("warning: ORRERY-managed Cua Driver executable is missing", file=sys.stderr)
        elif not preferences_ok(target.preferences):
            print(f"warning: Cua Driver privacy preferences are incomplete. See {HELP}", file=sys.stderr)
        return
    current, managed = existing_driver(target, link)
    if not sys.stdin.isatty():
        if current is None or upgrade:
            print(hint)
        return
    version, assets = read_pins(pins)
    if current:
        installed = driver_version(current, target)
        if upgrade and not managed:
            raise InstallError("only ORRERY-managed Cua Driver installations can be upgraded here")
        if installed == version or not upgrade:
            print(f"Cua Driver {installed} installed at {current}")
            if not managed and not os.environ.get("AGENTSTACK_CUA_DRIVER") and not shutil.which(target.binary):
                print(f"Set AGENTSTACK_CUA_DRIVER to this path if the relay cannot find it. See {HELP}")
            if installed != version:
                print(f"ORRERY expects {version}. See {HELP}")
                if managed:
                    print(f"Upgrade with {shlex.quote(str(install / 'bin/agentstack-install-cua-driver'))} --upgrade")
            return
    print("Computer use の画面確認・操作に使います。画面処理はローカルで、"
          "ORRERY 管理の起動では利用状況の送信と自動更新確認を無効にします。")
    action = "更新" if current else "インストール"
    try:
        answer = input(f"Cua Driver {version} を{action}しますか？（推奨） [Y/n] ").strip().lower()
    except EOFError:
        answer = "n"
    if answer not in {"", "y", "yes"}:
        print(hint)
        return
    with install_lock(link.parent):
        if existing_driver(target, link) != (current, managed):
            raise InstallError("Cua Driver changed while waiting for consent. Run the command again.")
        if link.exists() and not link.is_symlink():
            raise InstallError(f"unexpected file at managed Cua Driver path: {link}")
        binary = install_release(link, target, version, assets[target.key])
    print(f"Cua Driver {version}: {binary}")
    if target.os == "darwin":
        print("macOS のシステム設定で CuaDriver.app にアクセシビリティと画面収録を許可してください。")
    elif target.os == "windows":
        print("Windows に対話ログオンし、稼働中の daemon を使うか daemon を起動してください。")
    print(f"Computer use: {HELP}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", type=Path, required=True)
    parser.add_argument("--pins", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--upgrade", action="store_true", help="offer the pinned version for an older managed install")
    parser.add_argument("--check", action="store_true", help="check managed privacy preferences without starting Cua Driver")
    args = parser.parse_args()
    try:
        offer(args.install.resolve(), args.pins, dry_run=args.dry_run, upgrade=args.upgrade, check=args.check)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"Cua Driver: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
