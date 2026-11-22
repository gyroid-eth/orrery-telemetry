"""Task overrides and partial annotation updates use an isolated store."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import dashboard.server as server


@pytest.fixture
def annotations(monkeypatch, tmp_path):
    path = tmp_path / 'runtime' / 'annotations.json'
    monkeypatch.setattr(server, 'ANNOT_PATH', str(path))
    monkeypatch.setattr(server, 'LEGACY_ANNOT_PATH', str(tmp_path / 'legacy.json'))
    monkeypatch.setattr(server, '_ANNOT_CACHE', {'path': '', 'mtime': -1, 'data': {}})
    return path


def test_task_only_annotation_roundtrip_and_clear(annotations):
    result = server._write_annotation('BuilderFixture', task='  Review generated fixtures  ')
    assert result['ok']
    assert server._annotations()['BuilderFixture']['task'] == 'Review generated fixtures'
    assert json.loads(annotations.read_text())['BuilderFixture']['task'] == 'Review generated fixtures'
    assert server._write_annotation('BuilderFixture', task='') == {'ok': True, 'removed': 'BuilderFixture'}
    assert server._annotations() == {}


def test_partial_update_keeps_other_fields_and_explicit_clear_keeps_task(annotations):
    server._write_annotation('BuilderFixture', 'builder', '✳', 'fixtures', task='Original override')
    server._write_annotation('BuilderFixture', role='reviewer')
    assert server._annotations()['BuilderFixture'] == {
        'role': 'reviewer', 'emoji': '✳', 'group': 'fixtures', 'task': 'Original override',
    }
    server._write_annotation('BuilderFixture', '', '', '')
    assert server._annotations()['BuilderFixture']['task'] == 'Original override'
    assert server._write_annotation('BuilderFixture', task='')['removed'] == 'BuilderFixture'


def test_legacy_full_update_still_clears_all_legacy_fields(annotations):
    server._write_annotation('BuilderFixture', 'builder', '✳', 'fixtures')
    assert server._write_annotation('BuilderFixture', '', '', '') == {'ok': True, 'removed': 'BuilderFixture'}


def test_task_bound_is_unicode_characters_and_empty_override_is_removed(annotations):
    server._write_annotation('BuilderFixture', task='  ' + '界' * 201 + '  ')
    assert server._annotations()['BuilderFixture']['task'] == '界' * 200
    assert server._write_annotation('BuilderFixture', task=' \n\t')['removed'] == 'BuilderFixture'


def test_http_partial_update_preserves_omitted_fields(annotations):
    server._write_annotation('BuilderFixture', 'builder', '✳', 'fixtures')
    http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f'http://127.0.0.1:{http.server_port}/api/annotate',
            data=json.dumps({'name': 'BuilderFixture', 'task': 'User summary'}).encode(),
            headers={'Content-Type': 'application/json'}, method='POST',
        )
        with urllib.request.urlopen(request) as response:
            result = json.load(response)
        assert result['annot'] == {'name': 'BuilderFixture', 'role': 'builder',
                                   'emoji': '✳', 'group': 'fixtures', 'task': 'User summary'}
        with urllib.request.urlopen(f'http://127.0.0.1:{http.server_port}/api/annotations') as response:
            stored = json.load(response)['annotations']['BuilderFixture']
        assert stored == {key: value for key, value in result['annot'].items() if key != 'name'}
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('field', ['task', 'role', 'emoji', 'group'])
@pytest.mark.parametrize('alone', [False, True], ids=['keep-other-fields', 'remove-last-field'])
def test_http_null_clears_field_and_removes_annotation_only_when_empty(annotations, field, alone):
    values = {'role': 'builder', 'emoji': '✳', 'group': 'fixtures', 'task': 'User summary'}
    server._write_annotation('BuilderFixture', **({field: values[field]} if alone else values))
    http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f'http://127.0.0.1:{http.server_port}/api/annotate',
            data=json.dumps({'name': 'BuilderFixture', field: None}).encode(),
            headers={'Content-Type': 'application/json'}, method='POST',
        )
        with urllib.request.urlopen(request) as response:
            result = json.load(response)
        assert result['ok']
        if alone:
            assert result == {'ok': True, 'removed': 'BuilderFixture'}
            assert json.loads(annotations.read_text()) == {}
            assert server._annotations() == {}
        else:
            assert result['annot'].get(field, '') == ''
            for other, value in values.items():
                if other != field:
                    assert result['annot'][other] == value
            with urllib.request.urlopen(f'http://127.0.0.1:{http.server_port}/api/annotations') as response:
                stored = json.load(response)['annotations']['BuilderFixture']
            assert stored.get(field, '') == ''
            assert stored == json.loads(annotations.read_text())['BuilderFixture']
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)


def test_roster_annotation_does_not_replace_registered_task(annotations, monkeypatch):
    from test_dashboard_identity_link import _build
    server._write_annotation('BuilderFixture', task='User summary')
    _build(monkeypatch, 'BuilderFixture', 'BuilderFixture', attached=False)
    registered, instructions = server.agentmail_state()
    registered['BuilderFixture']['task'] = 'Registered description'
    monkeypatch.setattr(server, 'agentmail_state', lambda: (registered, instructions))
    row = server.build_agents()[0]
    assert row['task'] == 'Registered description'
    assert row['annot']['task'] == 'User summary'


def test_task_override_in_wrapped_legacy_store_migrates_without_losing_peers(annotations, monkeypatch, tmp_path):
    legacy = tmp_path / 'legacy.json'
    original = {'agents': {'BuilderFixture': {'role': 'builder', 'task': 'Stored summary'},
                           'PeerFixture': {'group': 'fixtures'}}}
    legacy.write_text(json.dumps(original))
    server._write_annotation('BuilderFixture', role='reviewer')
    stored = json.loads(annotations.read_text())['agents']
    assert stored['BuilderFixture']['task'] == 'Stored summary'
    assert stored['PeerFixture'] == original['agents']['PeerFixture']
    assert json.loads(legacy.read_text()) == original


def test_independent_concurrent_partial_updates_keep_both_fields(annotations):
    server._write_annotation('BuilderFixture', 'builder', '✳', 'fixtures')
    barrier = threading.Barrier(2)
    results = []
    def update(**fields):
        barrier.wait(timeout=3)
        results.append(server._write_annotation('BuilderFixture', **fields))
    threads = [threading.Thread(target=update, kwargs={'role': 'reviewer'}),
               threading.Thread(target=update, kwargs={'task': 'User summary'})]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
    assert len(results) == 2 and all(result['ok'] for result in results)
    assert server._annotations()['BuilderFixture'] == {
        'role': 'reviewer', 'emoji': '✳', 'group': 'fixtures', 'task': 'User summary',
    }


def test_graph_returns_task_override_alongside_registered_description(annotations, monkeypatch):
    server._write_annotation('BuilderFixture', task='User summary')
    monkeypatch.setattr(server, '_raw_graph', lambda: {'nodes': [{
        'name': 'BuilderFixture', 'last_active': 0, 'task': 'Registered description',
    }]})
    monkeypatch.setattr(server, '_graph_keep', lambda *_: ({'BuilderFixture'}, 0, {}, {}, None))
    monkeypatch.setattr(server, '_deliverables_index', lambda: {})
    monkeypatch.setattr(server, '_name_substitutions', lambda: {})
    monkeypatch.setattr(server, '_persistent_profiles', lambda: {})
    monkeypatch.setattr(server, '_resume_capability_for_row', lambda *_, **__: 'fixture')
    monkeypatch.setattr(server, '_claude_row_mail_fields', lambda *_, **__: {})
    row = server.graph_payload(1, True)['nodes'][0]
    assert row['task'] == 'Registered description'
    assert row['annot']['task'] == 'User summary'
