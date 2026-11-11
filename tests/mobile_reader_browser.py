"""Real phone page over opt-in CDP, with finite, synthetic APIs only."""
import json
import os
import re
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from dashboard.mobile_relay import static_assets
from test_dashboard_help_map import _WebSocket

ROOT = Path(__file__).resolve().parents[1]
RESULT = '**Benchmark result**\n\nThe larger cache improves throughput on long inputs. The short runs stay stable.\n\n| Input | Small | Large |\n|---|---:|---:|\n| 512 | 151 | 149 |\n| 4K | 573 | 588 |\n| 16K | 996 | 985 |\n\nRun the same inputs with `--repeat 3`:\n\n```sh\npython bench.py --repeat 3\n  --cache 512K\n```'


@pytest.fixture
def phone(request):
    cdp = os.environ.get('ORRERY_CDP_URL')
    if not cdp:
        pytest.skip('ORRERY_CDP_URL is required for the real phone page')
    assets = static_assets(ROOT / 'dashboard')
    css = re.search(r'<style id="agentstack-dashboard-styles">(.*?)</style>', (ROOT / 'dashboard/index.html').read_text(), re.S)[1].encode()
    agent = dict(name='SampleAgent', provider='openai', model='', running=True, state='run', act_state='work', retired=False, ctx_used=30, last_active=None)
    source = dict(status='available', observed_at=time.time(), age_seconds=0)
    snapshot = dict(schema=1, window='day', observed_at=time.time(), sources={k: source for k in ['agents', 'graph', 'mail']}, agents=[agent], period=dict(seconds=86400, activity_unknown=0), graph=dict(nodes=[agent], edges=[], spawn=[], degraded=False, unrepresented_edges=0), timeline=dict(scope='0'*32, mail=[], states=[], events_truncated=False, edges_truncated=False, states_truncated=False))
    events = [dict(id='result', kind='text', role='assistant', text=RESULT, ts='2026-01-01T08:45:00Z'), dict(id='request', kind='text', role='user', text='Please check the large cache once more.', ts='2026-01-01T08:45:30Z'), dict(id='question', kind='text', role='assistant', text='Use the same inputs for the next run?', ts='2026-01-01T08:46:00Z')]
    state = dict(events=events, error=None, preview_error=False, calls=[], get_calls=[], mail=None, draft='', snapshot=snapshot)
    contract = dict(contract='orrery-mobile-pane-v1', epoch='fixture-epoch')
    choice = dict(choice='fixture-choice', name='SampleAgent', pane_label='Window 0 · pane 0', revision='fixture-revision')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, data, mime='application/json', status=200):
            if not isinstance(data, bytes):
                data = json.dumps(data,ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split('?')[0]
            state['get_calls'].append(self.path)
            if path == '/view/message' and state['mail']:
                return self.send(dict(event=state['mail'], subject='Fixture subject', body='Fixture message body', subject_truncated=False, body_truncated=False))
            if path == '/view/mail' and state['mail']:
                return self.send(dict(window='day', direction='both', events=[state['mail']], next_cursor=None))
            if path == '/view/history':
                return self.send(dict(enabled=True))
            if path == '/view/snapshot':
                return self.send(snapshot)
            if path == '/view/conversation':
                return self.send(dict(ok=False, error=state['error']) if state['error'] else dict(ok=True, agent='SampleAgent', events=state['events'], older=None, limited=False, tail_only=False))
            if path == '/view/terminal':
                return self.send(dict(contract, api=31, supported='legacy-managed-pane', enabled=True, ack='tmux-command-accepted'))
            if path == '/view/conversation/preview':
                preview=state.get('file_preview',dict(ok=True, kind='text', text='File contents', notice='Current file, not a recorded snapshot.'))
                if 'thumbnail=1' in self.path and preview.get('kind')=='image':
                    from dashboard.mobile_history import thumbnail_preview
                    preview=thumbnail_preview(preview)
                return self.send(preview)
            if path == '/view/portrait' and state.get('portrait'):
                return self.send(state['portrait'], 'image/png')
            if path == '/network.css':
                return self.send(css, 'text/css')
            if path in assets:
                p, mime = assets[path]
                return self.send(p.read_bytes(), mime)
            return self.send(b'', status=404)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
            operation = self.path.rsplit('/', 1)[-1]
            state['calls'].append((operation, payload))
            selected = next((t for t in state.get('targets', [choice]) if t['choice'] == payload.get('choice')), choice)
            if operation == 'targets':
                return self.send(dict(contract, targets=state.get('targets', [choice])))
            if operation == 'preview':
                return self.send(dict(contract, target=selected, text=state.get('preview_text', 'Generated terminal tail\nReady.'), max_lines=state.get('preview_lines',120), max_bytes=state.get('preview_bytes',12288), observed_at=time.time()), status=503 if state['preview_error'] else 200)
            if operation == 'image':
                if payload.get('image') is None:
                    return self.send(dict(contract,target=selected,enabled=state.get('images',False)))
                if state.get('image_error'):
                    return self.send(dict(error=state['image_error']),status=403)
                return self.send(dict(contract,target=selected,enabled=True,path='/workspace/.orrery-input-images/'+'a'*32+'.png'))
            if operation == 'prepare':
                state['draft'] = payload['text']
                state['prepared_target'] = selected
                return self.send(dict(contract, state='prepared', target=selected, text=payload['text'], expires_in=30, intent='fixture-intent', confirmation='fixture-confirmation'))
            if operation == 'commit':
                return self.send(dict(contract, state='accepted', target=state.get('prepared_target',choice), intent='fixture-intent'))
            return self.send(dict(contract, state='cancelled'))

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    tab = client = None
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(30)
    try:
        tab = json.load(urllib.request.urlopen(urllib.request.Request(cdp + '/json/new?about:blank', method='PUT')))
        client = _WebSocket(tab['webSocketDebuggerUrl'])
        for method in ['Page.enable', 'Runtime.enable', 'Network.enable']:
            client.call(method)
        client.call('Network.setCacheDisabled', cacheDisabled=True)
        client.call('Emulation.setFocusEmulationEnabled', enabled=True)
        width, height = getattr(request, 'param', (390, 844))
        client.call('Emulation.setDeviceMetricsOverride', width=width, height=height, deviceScaleFactor=2, mobile=True)
        client.call('Page.navigate', url=f'http://127.0.0.1:{server.server_port}/')

        def js(expression):
            result = client.call('Runtime.evaluate', expression=expression, returnByValue=True, awaitPromise=True)
            assert not result.get('exceptionDetails'), result
            return result.get('result', {}).get('value')

        def wait(expression, timeout=6):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if js(expression):
                    return
                time.sleep(.03)
            raise AssertionError(expression + ': ' + str(js("document.querySelector('#terminal-sheet').innerText")))

        wait("typeof OrreryMobileTerminal!=='undefined'&&document.querySelector('body>header>span').textContent==='History · Terminal'")
        js("OrreryMobileTerminal.open('SampleAgent')")
        wait("document.querySelector('[data-id=question]')!==null")
        js('document.fonts.ready')
        yield client, js, wait, state
    finally:
        if client:
            client.close()
        if tab:
            urllib.request.urlopen(cdp + '/json/close/' + tab['id']).close()
        server.shutdown()
        server.server_close()
        socket.setdefaulttimeout(old_timeout)


AREA = """(()=>{const r=document.querySelector('.conversation-list').getBoundingClientRect(),w=innerWidth,h=innerHeight;let area=Math.max(0,Math.min(w,r.right)-Math.max(0,r.left))*Math.max(0,Math.min(h,r.bottom)-Math.max(0,r.top));for(const el of document.querySelectorAll('.reader-bar,.reader-input-bar')){const o=el.getBoundingClientRect();area-=Math.max(0,Math.min(r.right,o.right)-Math.max(r.left,o.left))*Math.max(0,Math.min(r.bottom,o.bottom)-Math.max(r.top,o.top));}return 100*area/(w*h);})()"""
