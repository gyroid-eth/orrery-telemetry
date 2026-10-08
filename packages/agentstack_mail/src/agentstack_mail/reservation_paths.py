"""Shared absolute reservation contract, used only by explicit candidate callers.

No project key, protected root or process cwd is an implicit anchor. Filesystem
names retain their spelling on case-sensitive filesystems. Existing components
are canonicalized by the filesystem (including symlinks and missing leaves).
"""

from __future__ import annotations

import fnmatch
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Mapping, Sequence

VIRTUAL_PREFIXES = ("tool://", "resource://", "service://")
GLOB_MARKERS = "*?["


class ReservationError(ValueError):
    """Stable non-secret diagnostic for a candidate reservation request."""


@dataclass(frozen=True)
class ReservationPath:
    kind: str
    value: str
    prefix: str | None = None
    case_insensitive: bool = False
    unicode_rule: str | None = None

    @property
    def is_glob(self) -> bool:
        return self.kind == "filesystem" and any(c in self.value for c in GLOB_MARKERS)


def _canonical(path: Path) -> Path:
    # realpath(strict=ALLOW_MISSING) is unavailable on supported Python 3.11.
    # Resolve existing components strictly, so permission errors/loops never
    # masquerade as a successfully canonicalized missing leaf.
    current = Path(path.anchor)
    for part in path.parts[1:]:
        if part == "..":
            try:
                if current.exists() and not current.is_dir():
                    raise ReservationError("PATH_ANCHOR_UNKNOWN")
            except OSError as exc:
                raise ReservationError("PATH_ANCHOR_UNKNOWN") from exc
            current = current.parent
            continue
        current = current / part
        try:
            current.lstat()
            current = current.resolve(strict=True)
        except FileNotFoundError:
            # A dangling symlink has an unknown anchor, rather than a new leaf.
            if current.is_symlink():
                raise ReservationError("PATH_ANCHOR_UNKNOWN")
        except (OSError, RuntimeError) as exc:
            raise ReservationError("PATH_ANCHOR_UNKNOWN") from exc
    return current


def normalize_path(
    raw: str,
    *,
    anchor: Path | None = None,
    wsl_drives: Mapping[str, str] | None = None,
) -> ReservationPath:
    if not isinstance(raw, str) or not raw or len(raw) > 4096 or "\x00" in raw:
        raise ReservationError("INVALID_PATH")
    if raw.startswith(VIRTUAL_PREFIXES):
        if raw in VIRTUAL_PREFIXES:
            raise ReservationError("INVALID_VIRTUAL_PATH")
        return ReservationPath("virtual", raw)
    drive = re.match(r"^([A-Za-z]):[\\/]", raw)
    if drive:
        root = (wsl_drives or {}).get(drive[1].lower())
        if root is None or not Path(root).is_absolute():
            raise ReservationError("WSL_MAPPING_REQUIRED")
        raw = str(Path(root) / raw[3:].replace("\\", "/"))
    elif "\\" in raw:
        # Do not silently turn a literal POSIX backslash into a separator.
        raise ReservationError("AMBIGUOUS_PATH_SEPARATOR")
    if "://" in raw or raw.startswith("~"):
        raise ReservationError("ABSOLUTE_PATH_REQUIRED")
    path = Path(raw)
    if not path.is_absolute():
        if anchor is None or not anchor.is_absolute():
            raise ReservationError("ABSOLUTE_PATH_REQUIRED")
        path = anchor / path
    parts = path.parts
    if len(parts) > 128:
        raise ReservationError("PATH_COMPONENT_LIMIT")
    first_glob = next(
        (i for i, part in enumerate(parts) if any(c in part for c in GLOB_MARKERS)),
        len(parts),
    )
    if first_glob < len(parts):
        tail = parts[first_glob:]
        if ".." in tail:
            raise ReservationError("GLOB_PARENT_AMBIGUOUS")
        prefix = _canonical(Path(*parts[:first_glob]))
        value = str(prefix.joinpath(*tail))
    else:
        resolved = _canonical(path)
        prefix = resolved.parent
        value = str(resolved)
    unicode_rule = unicode_rule_at(prefix)
    _unicode_key(value, unicode_rule)  # Unknown Unicode rules cannot grant a lease.
    case_insensitive = case_insensitive_at(prefix)
    _case_key(value, case_insensitive)
    return ReservationPath(
        "filesystem", value, str(prefix), case_insensitive, unicode_rule
    )


def _existing_directory(directory: Path) -> Path:
    while True:
        try:
            directory.stat()
            return directory
        except FileNotFoundError:
            if directory.is_symlink() or directory.parent == directory:
                raise ReservationError("PATH_ANCHOR_UNKNOWN")
            directory = directory.parent
        except OSError as exc:
            raise ReservationError("PATH_ANCHOR_UNKNOWN") from exc


def unicode_rule_at(directory: Path) -> str | None:
    """Read-only filesystem evidence, never an OS-wide Unicode assumption.

    APFS uses canonical-equivalence comparison (Unicode 9); HFS+ uses 3.2
    with exclusions. Unknown mounts/APIs remain unknown. Linux's normalizing
    casefold directories are deliberately not guessed from an ext4 mount name.
    """
    import ctypes

    directory = _existing_directory(directory)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if sys.platform == "darwin":
            # Darwin statfs64 ABI: bsize/iosize, five uint64 counters, fsid,
            # owner/type/flags/subtype, then the 16-byte filesystem type name.
            # https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/mount.h
            statfs = libc.statfs64
            statfs.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
            statfs.restype = ctypes.c_int
            buffer = ctypes.create_string_buffer(4096)
            if statfs(os.fsencode(directory), buffer) != 0:
                return None
            filesystem = buffer.raw[72:88].split(b"\0", 1)[0]
            if filesystem == b"hfs":
                import struct

                # Distinguish HFS+/HFSX from legacy HFS by volume signature.
                # ATTR_VOL_INFO | ATTR_VOL_SIGNATURE, no contents or disk reads.
                attrs = ctypes.create_string_buffer(
                    struct.pack("=HHIIIII", 5, 0, 0, 0x80000002, 0, 0, 0)
                )
                result = ctypes.create_string_buffer(16)
                getattrlist = libc.getattrlist
                getattrlist.argtypes = [
                    ctypes.c_char_p,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    ctypes.c_size_t,
                    ctypes.c_ulong,
                ]
                getattrlist.restype = ctypes.c_int
                if (
                    getattrlist(os.fsencode(directory), attrs, result, len(result), 0)
                    != 0
                ):
                    return None
                size, signature = struct.unpack_from("=II", result)
                return "hfs" if size >= 8 and signature in {0x482B, 0x4858} else None
            # Early APFS previews were normalization-sensitive; do not apply
            # the modern contract to an old or unidentifiable kernel.
            if filesystem == b"apfs" and int(os.uname().release.split(".")[0]) >= 17:
                return "apfs"
        elif sys.platform == "linux":
            statfs = libc.statfs
            statfs.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
            statfs.restype = ctypes.c_int
            buffer = ctypes.create_string_buffer(4096)
            if statfs(os.fsencode(directory), buffer) != 0:
                return None
            magic = ctypes.c_long.from_buffer(buffer).value
            # Known Linux filesystems require both legacy and modern directory
            # flags: FS_CASEFOLD_FL and FS_XFLAG_CASEFOLD. Unknown APIs fail closed.
            # https://github.com/torvalds/linux/blob/master/include/uapi/linux/fs.h
            if magic in {0x58465342, 0x9123683E, 0xEF53, 0x01021994, 0x794C7630}:
                import array
                import fcntl

                flags = array.array("L", [0])
                extended = array.array("I", [0] * 7)
                descriptor = os.open(
                    directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
                )
                try:
                    fcntl.ioctl(
                        descriptor, 0x80006601 | (flags.itemsize << 16), flags, True
                    )
                    fcntl.ioctl(descriptor, 0x801C581F, extended, True)
                finally:
                    os.close(descriptor)
                if not flags[0] & 0x40000000 and not extended[0] & 0x00040000:
                    return "exact"
                raise ReservationError("FILESYSTEM_RULES_UNKNOWN")
    except ReservationError:
        raise
    except (OSError, AttributeError, ValueError):
        return None
    return None


def _unicode_key(value: str, rule: str | None) -> str:
    if value.isascii() or rule == "exact":
        return value
    if rule == "apfs":
        # Normalization stability makes the common pre-3.2 repertoire safe on
        # APFS Unicode 9. Newer decomposable/combining characters need a pinned
        # Unicode-9 table; refuse them rather than using a newer Python table.
        for character in value:
            if unicodedata.ucd_3_2_0.category(character) == "Cn" and (
                unicodedata.combining(character)
                or unicodedata.normalize("NFD", character) != character
            ):
                raise ReservationError("UNICODE_RULES_UNKNOWN")
        return unicodedata.normalize("NFC", value)
    if rule == "hfs":
        # Apple HFS+ excludes these ranges from canonical decomposition. Keep
        # them as segment boundaries so NFC cannot accidentally alias them.
        result, pending = [], []
        for character in value:
            code = ord(character)
            if (
                0x2000 <= code <= 0x2FFF
                or 0xF900 <= code <= 0xFAFF
                or 0x2F800 <= code <= 0x2FAFF
            ):
                result.append(unicodedata.ucd_3_2_0.normalize("NFC", "".join(pending)))
                pending = []
                result.append(character)
            else:
                pending.append(character)
        result.append(unicodedata.ucd_3_2_0.normalize("NFC", "".join(pending)))
        return "".join(result)
    raise ReservationError("UNICODE_RULES_UNKNOWN")


def case_insensitive_at(directory: Path) -> bool:
    try:
        while not directory.exists():
            directory = directory.parent
        device = directory.stat().st_dev
        # Read-only proof using an existing directory alias on the same volume.
        # Never infer from macOS alone: mounted case-sensitive volumes exist.
        for current in (directory, *directory.parents):
            if (
                current.stat().st_dev != device
                or current.parent.stat().st_dev != device
            ):
                break
            alternate = current.name.swapcase()
            if alternate != current.name:
                try:
                    return current.samefile(current.parent / alternate)
                except FileNotFoundError:
                    return False
    except OSError as exc:
        raise ReservationError("PATH_ANCHOR_UNKNOWN") from exc
    return False


def normalize_paths(values: Sequence[str], **kwargs: object) -> list[ReservationPath]:
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 100:
        raise ReservationError("BOUNDED_PATH_LIST_REQUIRED")
    return [normalize_path(value, **kwargs) for value in values]


def _case_key(value: str, fold: bool) -> str:
    if not fold:
        return value
    # An existing ASCII directory alias proves case-insensitivity, not the
    # filesystem's versioned non-ASCII case table. No Python-wide casefold.
    # Refuse acquisition and keep probes unknown until that table is supported.
    if not value.isascii():
        raise ReservationError("CASE_RULES_UNKNOWN")
    return value.translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    )


def component_matches(
    value: str, pattern: str, *, fold: bool = False, unicode_rule: str | None = None
) -> bool:
    value, pattern = _unicode_key(value, unicode_rule), _unicode_key(
        pattern, unicode_rule
    )
    value, pattern = _case_key(value, fold), _case_key(pattern, fold)
    return fnmatch.fnmatchcase(value, pattern)


def _component_overlap(
    left: str,
    right: str,
    *,
    fold: bool = False,
    unicode_rules: tuple[str | None, str | None] = (None, None),
) -> bool:
    left, right = _unicode_key(left, unicode_rules[0]), _unicode_key(
        right, unicode_rules[1]
    )
    left, right = _case_key(left, fold), _case_key(right, fold)
    left_glob = any(c in left for c in GLOB_MARKERS)
    right_glob = any(c in right for c in GLOB_MARKERS)
    if not left_glob:
        return fnmatch.fnmatchcase(left, right)
    if not right_glob:
        return fnmatch.fnmatchcase(right, left)
    # Wildcard/wildcard ambiguity is conservatively conflicting. Fixed
    # directory components still delimit the range; '*' never crosses '/'.
    return True


def _lexical_overlap(left: ReservationPath, right: ReservationPath) -> bool:
    if left.kind != right.kind:
        return False
    if left.kind == "virtual":
        return left.value == right.value
    if not left.is_glob and not right.is_glob:
        try:
            if Path(left.value).samefile(right.value):
                return True
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ReservationError("PATH_ANCHOR_UNKNOWN") from exc
    a, b = Path(left.value).parts, Path(right.value).parts
    if left.prefix and right.prefix:
        try:
            if Path(left.prefix).samefile(right.prefix):
                a = ("/", *Path(left.value).relative_to(left.prefix).parts)
                b = ("/", *Path(right.value).relative_to(right.prefix).parts)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ReservationError("PATH_ANCHOR_UNKNOWN") from exc

    @lru_cache(None)
    def visit(i: int, j: int) -> bool:
        if i == len(a) and j == len(b):
            return True
        if i < len(a) and a[i] == "**":
            if visit(i + 1, j):
                return True
            if j < len(b) and visit(i, j + 1):
                return True
        if j < len(b) and b[j] == "**":
            if visit(i, j + 1):
                return True
            if i < len(a) and visit(i + 1, j):
                return True
        return (
            i < len(a)
            and j < len(b)
            and _component_overlap(
                a[i],
                b[j],
                fold=left.case_insensitive and right.case_insensitive,
                unicode_rules=(left.unicode_rule, right.unicode_rule),
            )
            and visit(i + 1, j + 1)
        )

    return visit(0, 0)


def reservation_scopes(
    path: ReservationPath, *, step: Callable[[], None] | None = None
) -> list[ReservationPath]:
    """Resolve wildcard directory aliases, including future files below links.

    A changing/too-wide scope is unknown, never silently nonconflicting. This
    check reads directory metadata only and bounds both entries and duration.
    """
    if not path.is_glob:
        return [path]
    prefix = Path(path.prefix or "/")
    parts = Path(path.value).relative_to(prefix).parts
    result = [path]
    deadline = time.monotonic() + 0.25
    steps = 0
    visited: set[tuple[str, int]] = set()

    def walk(directory: Path, index: int) -> None:
        nonlocal steps
        steps += 1
        if step:
            step()
        if steps > 1000 or time.monotonic() >= deadline:
            raise ReservationError("GLOB_SCOPE_UNKNOWN")
        canonical = _canonical(directory)
        key = (str(canonical), index)
        if key in visited:
            return
        visited.add(key)
        result.append(normalize_path(str(canonical.joinpath(*parts[index:]))))
        segment = parts[index]
        fold = case_insensitive_at(directory)
        unicode_rule = unicode_rule_at(directory)
        _unicode_key(segment, unicode_rule)
        last = index == len(parts) - 1
        if last and not any(c in segment for c in GLOB_MARKERS):
            return
        if segment == "**" and not last:
            walk(directory, index + 1)
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    steps += 1
                    if step:
                        step()
                    if steps > 1000 or time.monotonic() >= deadline:
                        raise ReservationError("GLOB_SCOPE_UNKNOWN")
                    if segment == "**" or component_matches(
                        entry.name, segment, fold=fold, unicode_rule=unicode_rule
                    ):
                        if last:
                            result.append(normalize_path(entry.path))
                        if entry.is_dir(follow_symlinks=True) and (
                            not last or segment == "**"
                        ):
                            walk(
                                Path(entry.path),
                                index if segment == "**" else index + 1,
                            )
        except FileNotFoundError:
            # _canonical above already rejects dangling/inaccessible anchors.
            return
        except OSError as exc:
            raise ReservationError("GLOB_SCOPE_UNKNOWN") from exc

    walk(prefix, 0)
    return result


def paths_overlap(left: ReservationPath, right: ReservationPath) -> bool:
    if _lexical_overlap(left, right):
        return True
    if left.kind != "filesystem" or right.kind != "filesystem":
        return False
    left_aliases, right_aliases = reservation_scopes(left), reservation_scopes(right)
    deadline = time.monotonic() + 0.25
    count = 0
    for a in left_aliases:
        for b in right_aliases:
            count += 1
            if count > 10000 or time.monotonic() >= deadline:
                raise ReservationError("GLOB_SCOPE_UNKNOWN")
            if _lexical_overlap(a, b):
                return True
    return False
