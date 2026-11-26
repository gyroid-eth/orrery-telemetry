"""Shared path checks for naming files in the cockpit's History.

The recorded call is what the cockpit turns into preview chips, under its own
checks (the working folder, the kind and the size). The proxy only gives the
agent metadata and a readability check without reading or sending the file.
Keep the description and bound-proxy result identical to
integrations/codex_app/.../mcp_server.py (tests compare them).

`base` is the agent's working folder, and only when the binding knows it (for
example a Codex App snapshot). The proxy's own cwd is never used: a plugin
proxy runs in its cache folder, where every relative path would look missing.
Without a base, relative paths are not checked here (`checked: false`); with
one, relative paths must stay inside the working folder. Absolute paths can
refer to files elsewhere. The direct Mail server requires absolute paths.
"""
from __future__ import annotations

import os
import re
import stat

DESCRIPTION = (
    "Show files or images to the person in the ORRERY cockpit: each path becomes "
    "a chip in the conversation's History that opens a preview (Markdown is "
    "drawn, images are shown). Call it when you created or changed a file or "
    "image the person should look at (a report, a figure, a screenshot, a log), "
    "usually right before your final message. Use an absolute path for files "
    "outside your working folder. Nothing is sent or written, and the file is "
    "not read into your context."
)
DIRECT_DESCRIPTION = (
    "Show files or images as preview chips in the ORRERY cockpit's History. "
    "Pass 1 to 8 absolute paths. Paths can be outside your working folder. "
    "Only readable regular files are listed in present. "
    "The cockpit checks supported file types, sizes and symbolic links. "
    "This checks file metadata without reading or sending the contents."
)
INPUT_SCHEMA = {
    'type': 'object',
    'properties': {'paths': {'type': 'array', 'minItems': 1, 'maxItems': 8,
                             'items': {'type': 'string', 'minLength': 1, 'maxLength': 4096}}},
    'required': ['paths'],
    'additionalProperties': False,
}
IMAGE_SUFFIXES = frozenset({'.png', '.jpg', '.jpeg', '.gif', '.webp'})
PREVIEW_NOTE = ("The cockpit uses the recorded call to show file chips. "
                "Previews check the file type, size, path and read permissions.")
NOTE = (PREVIEW_NOTE + " checked:false means this connection does "
        "not know your working folder, so a relative path was not checked here; an absolute path is.")
READ_DENIED = "OS permissions do not allow reading this path."


def _inside(path, root):
    return path == root or path.startswith(root.rstrip('/') + '/')


def result(values, base=None, *, absolute_only=False):
    """Raise ValueError for an argument that is not 1 to 8 plain paths."""
    if not isinstance(values, list) or not 1 <= len(values) <= 8:
        raise ValueError('paths must be a list of 1 to 8 paths')
    for value in values:
        if (not isinstance(value, str) or not value or len(value) > 4096
                or re.search(r'[\x00-\x1f\x7f]', value) or '://' in value or value.startswith('~')):
            raise ValueError('paths must be plain file paths (no URL, ~ or control character)')
        if absolute_only and not os.path.isabs(value):
            raise ValueError('show_files requires absolute paths on a direct Mail connection')
    root = os.path.normpath(base) if isinstance(base, str) and os.path.isabs(base) else None
    roots = {root, os.path.realpath(root)} if root else set()
    files = []
    for value in values:
        if os.path.isabs(value):
            target = os.path.normpath(value)
        elif root:
            target = os.path.normpath(os.path.join(root, value))
        else:
            files.append({'path': value, 'checked': False})
            continue
        if not os.path.isabs(value) and not any(_inside(target, item) for item in roots):
            files.append({'path': value, 'outside_working_folder': True})
            continue
        try:
            info = os.lstat(target)
        except PermissionError:
            files.append({'path': value, 'reason': READ_DENIED})
            continue
        except OSError:
            files.append({'path': value, 'exists': False})
            continue
        regular = stat.S_ISREG(info.st_mode)
        kind = ('image' if os.path.splitext(target)[1].lower() in IMAGE_SUFFIXES else 'file') if regular else 'other'
        item = {'path': value, 'exists': True, 'kind': kind, 'bytes': info.st_size if regular else None}
        if regular and not os.access(target, os.R_OK):
            item['reason'] = READ_DENIED
        files.append(item)
    return {'present': [item['path'] for item in files
                        if item.get('kind') in {'file', 'image'} and 'reason' not in item],
            'files': files, 'note': PREVIEW_NOTE if absolute_only else NOTE}
