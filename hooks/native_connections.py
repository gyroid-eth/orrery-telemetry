"""Canonical native connection IDs and kinds; stdlib, no runtime or key access.

IDs identify configured connections. Kinds select the transport family and must
never replace the ID when looking up routes, credentials, helpers or sessions.
"""
import json
from pathlib import Path
import re


LEGACY_KINDS = {'deepseek': 'deepseek', 'strata': 'strata'}
KINDS = frozenset(LEGACY_KINDS.values())
RESERVED_IDS = frozenset({'claude', 'claude-code', 'claudecode', 'codex', 'codex-cli',
                          'gemini', 'dsh', 'hermes', 'auto', 'main', 'same', 'custom'})
_ID = re.compile(r'[a-z][a-z0-9_-]{0,63}\Z', re.ASCII)
_MISSING = object()


def validate_id(value):
    if (not isinstance(value, str) or not _ID.fullmatch(value)
            or value in RESERVED_IDS):
        raise ValueError('invalid_connection_id')
    return value


def connection_kind(connection_id, kind=_MISSING):
    validate_id(connection_id)
    legacy = LEGACY_KINDS.get(connection_id)
    if kind is _MISSING:
        kind = legacy
    if not isinstance(kind, str) or kind not in KINDS or legacy is not None and kind != legacy:
        raise ValueError('invalid_connection_kind')
    return kind


def resolve_connection(manifest, connection_id):
    try:
        validate_id(connection_id)
    except ValueError:
        # Preserve the legacy generator's fixed, non-secret selector error.
        raise ValueError('unknown_backend') from None
    entries = manifest.get('backends') if isinstance(manifest, dict) else None
    spec = entries.get(connection_id) if isinstance(entries, dict) else None
    if not isinstance(spec, dict):
        raise ValueError('unknown_backend')
    kind = connection_kind(connection_id, spec.get('kind', _MISSING))
    if spec.get('model_source') == 'server' and kind != 'strata':
        raise ValueError('invalid_connection_kind')
    return kind, spec


def declared_connections(manifest):
    entries = manifest.get('backends') if isinstance(manifest, dict) else None
    if not isinstance(entries, dict):
        return []
    valid = []
    for connection_id in entries:
        try:
            kind, spec = resolve_connection(manifest, connection_id)
        except ValueError:
            continue
        valid.append((connection_id, kind, spec))
    # Keep the old two providers in their established order.
    return sorted(valid, key=lambda row: (0 if row[0] == 'deepseek' else
                                         1 if row[0] == 'strata' else 2, row[0]))


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('ambiguous_connection_configuration')
        result[key] = value
    return result


def read_manifest(path):
    # Do not silently select the last of two identically named connections.
    return json.loads(Path(path).read_text(), object_pairs_hook=_unique_pairs)
