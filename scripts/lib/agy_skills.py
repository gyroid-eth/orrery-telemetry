"""Manage the ORRERY entry in Antigravity's personal skills.json."""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile


SKILLS = ("delegate", "log")


def read_json(path):
    if path.is_symlink():
        raise ValueError(f"refusing to edit symlink {path}")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}, None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value, raw


def write_json(path, value, expected):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        if read_json(path)[1] != expected:
            raise ValueError(f"{path} changed during install")
        if expected is None:
            os.link(temp, path)
        else:
            temp.chmod(stat.S_IMODE(path.stat().st_mode))
            temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def skill_entries(config):
    entries = config.get("entries", [])
    if not isinstance(entries, list):
        raise ValueError("Antigravity skills.json entries must be an array")
    return entries


def filters(entry, key):
    value = entry.get(key)
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def user_skills(config, base, visited):
    """Find user-provided instances of the two bundled skill names."""
    found = set()
    for entry in skill_entries(config):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not entry["path"]:
            continue
        path = base / entry["path"]
        includes, excludes = filters(entry, "include_only"), filters(entry, "exclude")
        for name in SKILLS:
            candidates = includes or [name]
            for candidate in candidates:
                folder = Path(candidate)
                if folder.name != name or folder.is_absolute() or ".." in folder.parts:
                    continue
                if name in excludes or candidate in excludes:
                    continue
                direct = path / "SKILL.md" if path.is_dir() else path
                if (direct.name == "SKILL.md" and direct.parent.name == name and direct.is_file()) or (
                    path / folder / "SKILL.md"
                ).is_file():
                    found.add(name)
    inherited = config.get("inherits")
    for entry in inherited if isinstance(inherited, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        path = Path(entry["path"])
        if not path.is_absolute():
            continue
        path = path.resolve()
        if path in visited:
            continue
        try:
            inherited_config, _ = read_json(path)
        except (OSError, ValueError):
            continue
        inherited_base = path.parent.parent if path.parent.name == ".agents" else path.parent
        names = user_skills(inherited_config, inherited_base, visited | {path})
        includes, excludes = filters(entry, "include_only"), filters(entry, "exclude")
        found.update(name for name in names if (not includes or name in includes) and name not in excludes)
    return found


def managed_entries(owned, source):
    return [entry for entry in (owned.get("entry"), owned.get("previous_entry"))
            if isinstance(entry, dict) and entry.get("path") == source]


def install(manifest, receipt, source):
    config, raw = read_json(manifest)
    owned, previous = read_json(receipt)
    entries = skill_entries(config)
    managed = managed_entries(owned, source)
    personal = [entry for entry in entries if entry not in managed]
    if any(isinstance(entry, dict) and entry.get("path") == source for entry in personal):
        return
    # Duplicate names among manifest sources have no reliable tie-breaker in
    # agy 1.5.18. Register only names the user has not already defined there.
    claimed = user_skills({**config, "entries": personal}, manifest.parent, {manifest.resolve()})
    names = [name for name in SKILLS if name not in claimed]
    if not names:
        uninstall(manifest, receipt, source, dry_run=False)
        return
    entry = {"path": source, "include_only": names}
    updated = [entry, *personal]
    if updated == entries:
        return
    prior_entry = next((item for item in entries if item in managed), None)
    baseline = owned if prior_entry else {"created_manifest": raw is None, "had_entries": "entries" in config}
    ownership = {"entry": entry, "previous_entry": prior_entry,
                 "created_manifest": baseline.get("created_manifest"), "had_entries": baseline.get("had_entries")}
    write_json(receipt, ownership, previous)
    config["entries"] = updated
    write_json(manifest, config, raw)


def uninstall(manifest, receipt, source, dry_run):
    owned, _ = read_json(receipt)
    managed = managed_entries(owned, source)
    if not managed:
        return
    config, raw = read_json(manifest)
    entries = skill_entries(config)
    personal = [entry for entry in entries if entry not in managed]
    if dry_run:
        if personal != entries:
            print(f"DRY-RUN would remove the ORRERY entry from {manifest}")
        return
    if personal != entries:
        config["entries"] = personal
        if not personal and not owned.get("had_entries"):
            del config["entries"]
        if not config and owned.get("created_manifest"):
            if read_json(manifest)[1] != raw:
                raise ValueError(f"{manifest} changed during uninstall")
            manifest.unlink()
        else:
            write_json(manifest, config, raw)
    receipt.unlink()


if __name__ == "__main__":
    mode, source, target, ownership = sys.argv[1:]
    source = str(Path(source).resolve())
    manifest, receipt = Path(target), Path(ownership)
    try:
        if mode == "install":
            install(manifest, receipt, source)
        elif mode in {"uninstall", "dry-uninstall"}:
            uninstall(manifest, receipt, source, mode == "dry-uninstall")
        else:
            raise SystemExit(f"Unknown Antigravity skill operation: {mode}")
    except (OSError, ValueError) as exc:
        print(f"Antigravity skills: {exc}", file=sys.stderr)
