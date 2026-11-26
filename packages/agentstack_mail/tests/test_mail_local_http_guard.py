"""Private real MCP listener provenance and alias admission."""
import http.client
import json
import os
import socket
import subprocess
import sys
import time

import pytest


@pytest.fixture
def mail_listener(tmp_path):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('AGENTSTACK_', 'MCP_AGENT_MAIL_'))}
    env.update(HOME=str(tmp_path), AGENTSTACK_MAIL_ENV_FILE=str(tmp_path/'absent.env'),
               AGENTSTACK_MAIL_AGENT_NAME_ENFORCEMENT_MODE='passthrough',
               AGENTSTACK_MAIL_DATABASE_URL='sqlite+aiosqlite:///'+str(tmp_path/'fixture.sqlite3'),
               AGENTSTACK_MAIL_STORAGE_ROOT=str(tmp_path/'archive'),
               AGENTSTACK_MAIL_NOTIFICATIONS_SIGNALS_DIR=str(tmp_path/'signals'))
    with (tmp_path/'server.log').open('w+') as log:
        process = subprocess.Popen([sys.executable, '-m', 'agentstack_mail.cli',
                                    '--host', '127.0.0.1', '--port', str(port)],
                                   env=env, stdout=log, stderr=log)
        try:
            for _ in range(300):
                if process.poll() is not None:
                    log.seek(0)
                    pytest.fail(log.read())
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.1):
                        break
                except OSError:
                    time.sleep(.05)
            else:
                pytest.fail('MCP listener did not start')
            yield port
        finally:
            process.terminate()
            try:
                process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def request(port, path, extra=(), method='POST', content_type='application/json', host=None,
            *, rpc_method='tools/call', params=None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    if params is None:
        params = {'name': 'health_check', 'arguments': {}}
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': rpc_method, 'params': params}).encode()
    try:
        connection.putrequest(method, path, skip_host=True)
        connection.putheader('Host', host or f'127.0.0.1:{port}')
        connection.putheader('Content-Type', content_type)
        connection.putheader('Accept', 'application/json, text/event-stream')
        connection.putheader('Content-Length', str(len(body)))
        for key, value in extra:
            connection.putheader(key, value)
        connection.endheaders(body)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_real_mcp_own_page_and_local_processes(mail_listener):
    port = mail_listener
    for path in ('/mcp', '/api', '/api/'):
        for headers in ([], [('Origin', f'http://127.0.0.1:{port}')]):
            status, body = request(port, path, headers)
            assert status == 200, body
            assert 'result' in json.loads(body)
    for host in (f'localhost:{port}', f'[::1]:{port}'):
        assert request(port, '/mcp', [('Origin', 'http://' + host)], host=host)[0] == 200


def test_direct_mcp_show_files_lists_and_checks_only_absolute_paths(mail_listener, tmp_path):
    status, body = request(mail_listener, '/mcp', rpc_method='tools/list', params={})
    assert status == 200, body
    tools = {tool['name']: tool for tool in json.loads(body)['result']['tools']}
    schema = tools['show_files']['inputSchema']
    assert set(schema['properties']) == {'paths'}
    assert schema['required'] == ['paths']
    description = tools['show_files']['description']
    assert 'working folder or a configured workspace root' in description
    assert 'unverified and omits them from present' in description

    report = tmp_path / 'report.md'
    content = b'# Only metadata should return'
    report.write_bytes(content)
    image = tmp_path / 'figure.png'
    image.write_bytes(b'fixture image')
    link = tmp_path / 'link.md'
    link.symlink_to(report)
    missing = tmp_path / 'missing.png'

    def show(paths):
        status, body = request(mail_listener, '/mcp',
                               params={'name': 'show_files', 'arguments': {'paths': paths}})
        assert status == 200, body
        assert content not in body
        return json.loads(body)['result']

    response = show([str(report), str(image), str(link), str(missing)])
    assert not response.get('isError')
    value = response['structuredContent']
    assert value['present'] == [str(image)]
    assert value['files'] == [
        {'path': str(report), 'exists': True, 'kind': 'file', 'bytes': len(content),
         'status': 'unverified',
         'reason': "The cockpit previews text files only inside the conversation's working folder "
                   "or a configured workspace root. This direct Mail connection cannot verify those roots."},
        {'path': str(image), 'exists': True, 'kind': 'image', 'bytes': len(b'fixture image')},
        {'path': str(link), 'exists': True, 'kind': 'other', 'bytes': None},
        {'path': str(missing), 'exists': False},
    ]
    assert show([str(report)])['structuredContent']['present'] == []
    assert 'Unverified paths are omitted from present' in value['note']

    rejected = show([str(report), report.name])
    assert rejected['isError']
    assert 'requires absolute paths on a direct Mail connection' in rejected['content'][0]['text']
    assert 'structuredContent' not in rejected


def test_real_mcp_external_and_ambiguous_provenance(mail_listener):
    port = mail_listener
    cases = [[('Origin', value)] for value in ('null', '', 'https://external.example',
              f'http://127.0.0.1:{port+1}', f'http://localhost:{port}')]
    cases += [[('Origin', f'http://127.0.0.1:{port}'), ('Origin', 'null')],
              [('Sec-Fetch-Site', 'cross-site'), ('Sec-Fetch-Mode', 'cors')],
              [('Sec-Fetch-Site', 'same-origin'), ('Sec-Fetch-Site', 'cross-site')]]
    for path in ('/mcp', '/api', '/api/', '/future-operation'):
        for headers in cases:
            assert request(port, path, headers)[0] == 403, (path, headers)
        for host in (f'external.example:{port}', f'localhost:{port+1}', 'localhost'):
            assert request(port, path, host=host)[0] == 403
        assert request(port, path, [('Host', f'localhost:{port}')])[0] in (400, 403)
        assert request(port, path, [('Origin', 'null')], content_type='application/x-www-form-urlencoded')[0] == 403
        assert request(port, path, [('Origin', 'null')], method='OPTIONS')[0] == 403
