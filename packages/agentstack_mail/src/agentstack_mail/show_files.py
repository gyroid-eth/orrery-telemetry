"""show_files: a local proxy tool that names files for the cockpit's History.

The recorded call is what the cockpit turns into preview chips, under its own
checks (the working folder, the kind and the size). The proxy only tells the
agent whether each path is there: it sends nothing, writes nothing and never
opens, reads or follows a path. Keep the description identical to
integrations/codex_app/.../mcp_server.py (a test compares them).
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

DESCRIPTION = (
    "Show files or images to the person in the ORRERY cockpit: each path becomes "
    "a chip in the conversation's History that opens a preview (Markdown is "
    "drawn, images are shown). Call it when you created or changed a file or "
    "image the person should look at (a report, a figure, a screenshot, a log), "
    "usually right before your final message. Use paths inside your working "
    "folder. Nothing is sent or written, and the file is not read into your "
    "context."
)
INPUT_SCHEMA = {
    'type': 'object',
    'properties': {'paths': {'type': 'array', 'minItems': 1, 'maxItems': 8,
                             'items': {'type': 'string', 'minLength': 1, 'maxLength': 4096}}},
    'required': ['paths'],
    'additionalProperties': False,
}
IMAGE_SUFFIXES = frozenset({'.png', '.jpg', '.jpeg', '.gif', '.webp'})


def result(values, cwd=None):
    """Raise ValueError for an argument that is not 1 to 8 plain paths."""
    if not isinstance(values, list) or not 1 <= len(values) <= 8:
        raise ValueError('paths must be a list of 1 to 8 paths')
    base = Path(cwd or os.getcwd())
    files = []
    for value in values:
        if (not isinstance(value, str) or not value or len(value) > 4096
                or re.search(r'[\x00-\x1f\x7f]', value) or '://' in value or value.startswith('~')):
            raise ValueError('paths must be plain file paths (no URL, ~ or control character)')
        path = Path(value) if os.path.isabs(value) else base / value
        try:
            info = path.lstat()
        except OSError:
            files.append({'path': value, 'exists': False})
            continue
        regular = stat.S_ISREG(info.st_mode)
        files.append({'path': value, 'exists': True,
                      'kind': ('image' if path.suffix.lower() in IMAGE_SUFFIXES else 'file') if regular else 'other',
                      'bytes': info.st_size if regular else None})
    return {'shown': [item['path'] for item in files if item.get('kind') in {'file', 'image'}],
            'files': files,
            'note': "Shown as chips in the cockpit's History when inside your working folder."}
