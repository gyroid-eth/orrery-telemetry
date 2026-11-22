"""Literal regression cases for Mail's one authoritative PathSpec matcher.

The removed hook parity suite tested agreement with a second implementation.
These cases pin the server's behavior directly; no matcher copy is retained.
"""
import re
import pytest
from agentstack_mail.app import _patterns_overlap


@pytest.mark.parametrize('pattern,target,expected',[
    ('docs/**/*.md','docs/note.md',True),
    ('docs/**/*.md','docs/deep/note.md',True),
    ('docs/**/*.md','docs/note.txt',False),
    ('docs/**/deep/*.md','docs/x/deep/a.md',True),
    ('docs/**/deep/*.md','docs/deep/a.md',True),
    ('docs/','docs/deep/note.md',True),
    ('**/note.md','note.md',True),
    ('**/note.md','other/note.txt',False),
    ('/docs/*.md','docs/note.md',True),
    ('docs\\*.md','docs/note.md',True),
    ('cafe\u0301/**','caf\u00e9/note.md',True),
    ('caf\u00e9/**','cafe\u0301/note.md',True),
    ('猫/**','猫/note.md',True),
    ('猫/**','犬/note.md',False),
    ('docs/**','other/note.md',False),
    ('docs/**','DOCS/note.md',False),
    ('docs/?ote.md','docs/note.md',True),
    ('docs/?ote.md','docs/noote.md',False),
    ('docs/[ab].md','docs/a.md',True),
    ('docs/[ab].md','docs/c.md',False),
    ('docs/[a-c].md','docs/b.md',True),
    ('docs/[a-c].md','docs/z.md',False),
    ('docs/[!a].md','docs/b.md',True),
    ('docs/[!a].md','docs/a.md',False),
    ('docs/[^a].md','docs/b.md',True),
    ('docs/[!x][!x][!x].md','docs/a/b.md',True),
    ('docs/[^x][^x][^x].md','docs/a/b.md',True),
    ('docs/[.-0][.-0][.-0].md','docs/0/0.md',True),
    ('!docs/**','docs/note.md',False),
    ('#[z-a]','docs/note.md',False),
    ('note.md','note.md',True),
    ('note.md','other.txt',False),
])
def test_authoritative_mail_patterns(pattern,target,expected):
    assert _patterns_overlap(pattern,target) is expected


@pytest.mark.parametrize('pattern,target',[
    ('!','docs/note.md'),('![z-a]','docs/note.md'),
    ('!docs/[z-a].md','docs/note.md'),
    ('docs/**','docs/[z-a].md'),('*','[z-a]'),
])
def test_invalid_patterns_fail_before_a_successful_match(pattern,target):
    with pytest.raises((ValueError,re.error)):_patterns_overlap(pattern,target)
