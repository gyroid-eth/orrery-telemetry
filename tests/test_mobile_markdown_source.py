"""Bundled parser drift is explicit, even without an upstream checkout."""
import hashlib
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def bundled():
    header, source = (ROOT / 'dashboard/mobile/conversation_markdown.js').read_bytes().split(b'\n', 1)
    assert header.startswith(b'// Source: gyroid-eth/orrery bridge/conversation_markdown.js @ d12a266 ')
    return source


def test_bundled_parser_has_the_reviewed_upstream_digest():
    assert hashlib.sha256(bundled()).hexdigest() == '9382246debb5ad63fdadf69e67c188537e1938e315264bfd40a28195632f7bc1'


def test_bundled_parser_matches_upstream_byte_for_byte():
    source = os.environ.get('ORRERY_MARKDOWN_SOURCE')
    if not source or not Path(source).is_file():
        pytest.skip('ORRERY_MARKDOWN_SOURCE is not available')
    assert bundled() == Path(source).read_bytes()
