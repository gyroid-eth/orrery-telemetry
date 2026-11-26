"""Shared path checks for naming files in the cockpit's History.

The recorded call is what the cockpit turns into preview chips, under its own
checks (the working folder, the kind and the size). The proxy only gives the
agent a hint about each path: it sends nothing, writes nothing and never
opens, reads or follows a path. Keep the description and `result` identical to
integrations/codex_app/.../mcp_server.py (tests compare them).

`base` is the agent's working folder, and only when the binding knows it (for
example a Codex App snapshot). The proxy's own cwd is never used: a plugin
proxy runs in its cache folder, where every relative path would look missing.
Without a base, relative paths are not checked here (`checked: false`); with
one, a path outside it is reported as such and not even lstat'ed.
The direct Mail server requires absolute paths and reports non-image regular
files as unverified when it has no session working folder.
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
    "usually right before your final message. Use paths inside your working "
    "folder, relative or absolute. Nothing is sent or written, and the file is "
    "not read into your context."
)
DIRECT_DESCRIPTION = (
    "Show files or images as preview chips in the ORRERY cockpit's History. "
    "Pass 1 to 8 absolute paths. The cockpit previews text files only inside "
    "the conversation's working folder or a configured workspace root. "
    "Direct Mail cannot verify that boundary, so it reports non-image regular "
    "files as unverified and omits them from present. The cockpit applies its "
    "preview checks to recorded images. "
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
PREVIEW_NOTE = ("Each path is shown as a chip in the cockpit's History; the cockpit checks it against your "
                "working folder, kind and size when it is opened.")
NOTE = (PREVIEW_NOTE + " checked:false means this connection does "
        "not know your working folder, so a relative path was not checked here; an absolute path is.")
DIRECT_FILE_REASON = (
    "The cockpit previews text files only inside the conversation's working folder "
    "or a configured workspace root. This direct Mail connection cannot verify those roots."
)
DIRECT_NOTE = (
    "Unverified paths are omitted from present. The cockpit uses the recorded call "
    "and checks each preview against its file policy."
)


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
        if roots and not any(_inside(target, item) for item in roots):
            files.append({'path': value, 'outside_working_folder': True})
            continue
        try:
            info = os.lstat(target)
        except OSError:
            files.append({'path': value, 'exists': False})
            continue
        regular = stat.S_ISREG(info.st_mode)
        kind = ('image' if os.path.splitext(target)[1].lower() in IMAGE_SUFFIXES else 'file') if regular else 'other'
        item = {'path': value, 'exists': True, 'kind': kind, 'bytes': info.st_size if regular else None}
        if absolute_only and root is None and kind == 'file':
            item.update(status='unverified', reason=DIRECT_FILE_REASON)
        files.append(item)
    return {'present': [item['path'] for item in files
                        if item.get('kind') in {'file', 'image'} and item.get('status') != 'unverified'],
            'files': files, 'note': DIRECT_NOTE if absolute_only else NOTE}
