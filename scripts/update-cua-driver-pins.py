#!/usr/bin/env python3
"""Refresh all Cua Driver pins for one explicit published upstream release."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
from urllib.request import Request, urlopen

from lib.cua_driver_install import RELEASES, TARGETS, InstallError, asset_url, release_tag


def get(url):
    request = Request(url, headers={"User-Agent": "orrery-cua-driver-pins"})
    with urlopen(request, timeout=45) as response:
        return response.read()


def refresh(version):
    tag = release_tag(version)
    release = json.loads(get(f"https://api.github.com/repos/trycua/cua/releases/tags/{tag}"))
    if release["tag_name"] != tag or release["draft"]:
        raise InstallError("expected a published Cua Driver release for the requested tag")
    indexed = {asset["name"]: asset["digest"] for asset in release["assets"]}
    sums = get(f"{RELEASES}/{tag}/SHA256SUMS")
    if indexed["SHA256SUMS"] != "sha256:" + hashlib.sha256(sums).hexdigest():
        raise InstallError("SHA256SUMS differs from the GitHub release digest")
    checksums = {name: digest for digest, name in (line.split() for line in sums.decode("ascii").splitlines())}
    pins = {}
    for key in TARGETS:
        url = asset_url(version, key)
        name = url.rsplit("/", 1)[1]
        digest = checksums[name]
        signature = indexed.get(name + ".sigstore.json") or ""
        if (not re.fullmatch(r"[a-f0-9]{64}", digest) or indexed[name] != "sha256:" + digest
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", signature)):
            raise InstallError(f"digest or signing bundle missing or inconsistent for {name}")
        pins[key] = {"url": url, "sha256": digest}
    return {"version": version, "assets": pins}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    path = root / "integrations/cua-driver/runtime-pins.json"
    try:
        pins = refresh(args.version)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temp:
            temporary = Path(temp.name)
            try:
                json.dump(pins, temp, indent=2)
                temp.write("\n")
                temp.close()
                temporary.chmod(0o644)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        schemas = root / "packages/agentstack_mail/src/agentstack_mail/screen_schemas.json"
        if json.loads(schemas.read_text(encoding="utf-8"))["version"] != args.version:
            print("Review the new driver's tools/list and update the pinned screen schemas before release.")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Cua Driver pins: {exc}", file=sys.stderr)
        return 1
    print(f"Updated {path.relative_to(root)} for {args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
