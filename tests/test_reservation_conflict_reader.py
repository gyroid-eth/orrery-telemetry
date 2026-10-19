"""Shared legacy resource parser, real pattern matcher, no HTTP or lease writes."""
from datetime import datetime, timezone
import json
from pathlib import Path
import runpy

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def reader():
    api = runpy.run_path(str(ROOT / 'hooks/check-reservation-conflicts.py'))
    assert callable(api.get('conflicting_owners')), 'SHARED_RESERVATION_READER_MISSING'
    matcher = runpy.run_path(str(ROOT / 'hooks/reservation-patterns.py'))['patterns_overlap']
    return api['conflicting_owners'], lambda: matcher


def document(rows): return {'result': {'contents': [{'text': json.dumps(rows)}]}}


def row(**changes):
    return {'agent': 'AnotherAgent', 'path_pattern': 'scratch/**', 'exclusive': True,
        'expires_ts': '2030-01-01T00:01:00Z', 'released_ts': None, **changes}


def read(reader, value, *, targets=('scratch/note.md', '/fixture/scratch/note.md')):
    function, factory = reader
    return function(value, agent='FixtureChild', targets=targets, now=NOW, matcher_factory=factory)


@pytest.mark.parametrize('changes', [{'agent': 'FixtureChild'}, {'exclusive': False},
    {'expires_ts': '2030-01-01T00:00:00Z'}, {'expires_ts': '2020-01-01T00:00:00Z'},
    {'released_ts': '2029-12-31T23:59:00Z'}, {'path_pattern': 'different/**'}])
def test_nonconflicting_rows_preserve_the_legacy_skip_rules(reader, changes):
    assert read(reader, document([row()])) == {'AnotherAgent'}
    assert read(reader, document([row(**changes)])) == set()


@pytest.mark.parametrize('mutation', ['rpc-error', 'result-list', 'is-error', 'non-bool-is-error',
    'contents-empty', 'contents-multiple', 'bad-json', 'rows-object', 'row-agent', 'row-pattern',
    'row-exclusive', 'expiry-naive', 'expiry-invalid', 'release-bool', 'release-naive', 'invalid-pattern'])
def test_malformed_resource_is_never_equated_with_no_conflicts(reader, mutation):
    assert read(reader, document([])) == set()
    assert read(reader, document([row()])) == {'AnotherAgent'}
    value = document([row()]); result = value['result']
    if mutation == 'rpc-error': value['error'] = {}
    elif mutation == 'result-list': value['result'] = []
    elif mutation == 'is-error': result['isError'] = True
    elif mutation == 'non-bool-is-error': result['isError'] = 1
    elif mutation == 'contents-empty': result['contents'] = []
    elif mutation == 'contents-multiple': result['contents'] *= 2
    elif mutation == 'bad-json': result['contents'][0]['text'] = '['
    elif mutation == 'rows-object': result['contents'][0]['text'] = '{}'
    else:
        changes = {'row-agent': {'agent': False}, 'row-pattern': {'path_pattern': ''},
            'row-exclusive': {'exclusive': 1}, 'expiry-naive': {'expires_ts': '2030-01-01T00:01:00'},
            'expiry-invalid': {'expires_ts': 'not-a-date'}, 'release-bool': {'released_ts': False},
            'release-naive': {'released_ts': '2030-01-01T00:00:00'},
            'invalid-pattern': {'path_pattern': '[z-a]'}}[mutation]
        value = document([row(**changes)])
    with pytest.raises((ValueError, KeyError, TypeError, AttributeError)):
        read(reader, value)


def test_both_path_spellings_and_nfc_use_the_same_real_mail_pattern_adapter(reader):
    assert read(reader, document([row(path_pattern='/fixture/scratch/**')])) == {'AnotherAgent'}
    assert read(reader, document([row(path_pattern='**/é.md')]),
        targets=('scratch/e\u0301.md', '/fixture/scratch/e\u0301.md')) == {'AnotherAgent'}
    assert read(reader, document([row(), row(agent='ThirdAgent'), row()])) == {'AnotherAgent', 'ThirdAgent'}


def test_matcher_stays_lazy_for_own_released_or_nonexclusive_rows(reader):
    function, _ = reader
    def forbidden(): pytest.fail('legacy skip rule unexpectedly loaded matcher')
    rows = [row(agent='FixtureChild'), row(exclusive=False), row(released_ts='2029-12-31T00:00:00Z')]
    assert function(document(rows), agent='FixtureChild', targets=('scratch/note.md', '/fixture/note.md'),
        now=NOW, matcher_factory=forbidden) == set()
