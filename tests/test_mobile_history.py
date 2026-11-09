"""Opt-in mobile conversation history on ephemeral loopback fixtures.

A synthetic cockpit stands in for the fixed conversation route. No real
cockpit, transcript, Tailscale, Mail database or production port is touched.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
from pathlib import Path
import shutil
import subprocess
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

from dashboard import mobile_history as history
from dashboard import mobile_relay as relay

ROOT = Path(__file__).resolve().parents[1]
OWNER = {'Host': 'viewer.example.ts.net:9443', 'Tailscale-User-Login': 'owner@example.test'}


@contextmanager
def serving(server):
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown(); server.server_close(); worker.join(3)


class Store:
    def __init__(self, names=('Vega', 'Atlas')):
        self.names = set(names)
    def snapshot(self, window):
        return {'schema': 1, 'sources': {'agents': {'status': 'available'}}, 'agents': []}
    def known_name(self, nm):
        return nm in self.names


class Recorder:
    def __init__(self):
        self.calls = []
    def page(self, name, token=None):
        self.calls.append((name, token))
        return {'ok': True, 'agent': name, 'provider': 'claude', 'events': [], 'older': None,
                'limited': False, 'tail_only': False}


def get(port, path, headers=OWNER):
    con = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        con.request('GET', path, headers=headers)
        response = con.getresponse()
        return response.status, response.read()
    finally:
        con.close()


def reader(enabled=True, client=None, store=None):
    config = relay.Config('owner@example.test', 'https://viewer.example.ts.net:9443',
                          phone_policy_reviewed=enabled, history_enabled=enabled)
    return relay.make_server(config, store=store or Store(), history_client=client, test_port=0)


# --- opt-in gate -----------------------------------------------------------

def test_history_needs_the_policy_acknowledgement(monkeypatch):
    # Phone access turns history on by default once the policy is reviewed
    # (tests/test_mobile_settings.py); without the review it stays off.
    assert relay.Config('owner@example.test', 'https://viewer.example.ts.net').history_enabled is False
    with pytest.raises(ValueError):
        relay.Config('owner@example.test', 'https://viewer.example.ts.net', history_enabled=True)
    monkeypatch.setattr(relay, 'make_server', lambda *a, **k: pytest.fail('option check must not listen'))
    base = ['--owner-login', 'owner@example.test', '--public-origin', 'https://viewer.example.ts.net:9443']
    with pytest.raises(SystemExit):
        relay.main(['--enable', '--enable-history', *base])
    with pytest.raises(SystemExit):
        relay.main(['--enable-history', *base])
    assert relay.main(['--check', '--enable-history', '--phone-policy-reviewed', *base]) == 0


@pytest.mark.parametrize('path', ['/view/conversation', '/view/conversation?name=Vega',
                                  '/view/conversation?name=Vega&page=' + 'a' * 32])
def test_disabled_reader_has_no_conversation_route_and_never_calls_cockpit(path):
    client = Recorder()
    server = reader(enabled=False, client=client)
    assert server.mobile_config.history_enabled is False
    with serving(server) as port:
        assert get(port, path)[0] == 404
        status, body = get(port, '/view/history')
        assert status == 200 and json.loads(body)['enabled'] is False
    assert client.calls == []


def test_default_reader_builds_no_history_client():
    server = relay.make_server(relay.Config('owner@example.test', 'https://viewer.example.ts.net:9443'),
                               store=Store(), test_port=0)
    try:
        assert server.mobile_history is None
    finally:
        server.server_close()


# --- the phone supplies a name and an issued token only ------------------------

@pytest.mark.parametrize('query,status', [
    ('name=Vega&path=/tmp/x.jsonl', 400), ('name=/tmp/x.jsonl', 400), ('name=../Vega', 400),
    ('name=Vega&before=abc:0', 400), ('name=Vega&cursor=abc:0', 400), ('name=Vega&transcript_path=/x', 400),
    ('name=Vega&name=Atlas', 400), ('name=Vega&page=abc:0', 400), ('name=Vega&page=%2Ftmp', 403),
    ('', 400), ('name=Nobody', 404),
])
def test_paths_cursors_and_unknown_names_are_refused_before_cockpit(query, status):
    client = Recorder()
    with serving(reader(client=client)) as port:
        assert get(port, '/view/conversation?' + query)[0] == status
    assert client.calls == []


def test_conversation_route_requires_owner_and_outer_host():
    client = Recorder()
    with serving(reader(client=client)) as port:
        assert get(port, '/view/conversation?name=Vega', headers={'Host': 'viewer.example.ts.net:9443'})[0] == 403
        assert get(port, '/view/conversation?name=Vega', headers={**OWNER, 'Tailscale-User-Login': 'other@example.test'})[0] == 403
        assert get(port, '/view/conversation?name=Vega', headers={**OWNER, 'Origin': 'https://other.example.ts.net:9443'})[0] == 403
        assert get(port, '/view/conversation?name=Vega')[0] == 200
    assert client.calls == [('Vega', None)]


# --- fixed cockpit route and bounded projection -------------------------------

class Cockpit(BaseHTTPRequestHandler):
    requests = []
    reply = None
    def log_message(self, *a):
        pass
    def do_GET(self):
        type(self).requests.append((self.path, dict(self.headers)))
        status, payload = type(self).reply(parse_qs(urlsplit(self.path).query))
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@contextmanager
def cockpit(reply):
    Cockpit.requests = []
    Cockpit.reply = staticmethod(reply)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Cockpit)
    with serving(server) as port:
        yield port


SECRET = '/Users/someone/private/session.jsonl'


def event(i, **extra):
    return {'id': str(i), 'kind': 'text', 'role': 'assistant' if i % 2 else 'user', 'text': 'line %d' % i,
            'ts': '2026-11-04T01:00:00Z', 'cursor_before': 'f:%d' % i, 'cursor_after': 'f:%d' % (i + 1),
            'transcript_path': SECRET, **extra}


def test_client_calls_one_fixed_route_and_projects_only_displayed_fields():
    raw = [event(1, diff='--- ' + SECRET, references=[{'id': 'ref-token', 'label': 'chart.png', 'kind': 'image', 'path': SECRET}]),
           {'id': '2', 'kind': 'tool_result', 'role': 'assistant', 'text': 'tool output'},
           {'id': '3', 'kind': 'tool', 'role': 'assistant', 'text': 'Read\nsecond line'},
           {'id': '4', 'kind': 'mail', 'role': 'user', 'text': 'Subject', 'mail': [
               {'direction': 'received', 'sender': 'Lead', 'recipients': [], 'subject': 'Subject', 'importance': 'high',
                'coverage': 'complete', 'body': 'x' * (40 * 1024), 'truncated': False, 'source': 'recorded', 'path': SECRET}]},
           {'id': '5', 'kind': 'text', 'role': 'system', 'text': 'hidden'}]
    with cockpit(lambda q: (200, {'ok': True, 'agent': q['agent'][0], 'provider': 'claude', 'events': raw,
                                  'before': 'f:1', 'after': 'f:9', 'has_older': True, 'has_newer': False,
                                  'transcript_path': SECRET})) as port:
        page = history.HistoryClient(port).page('Vega')
        path, headers = Cockpit.requests[0]
    assert urlsplit(path).path == '/telemetry/conversation'
    assert parse_qs(urlsplit(path).query) == {'agent': ['Vega'], 'limit': [str(history.PAGE_EVENTS)]}
    assert headers['Host'] == '127.0.0.1:%d' % port
    assert not {'Origin', 'Tailscale-User-Login', 'Cookie'} & set(headers)
    dumped = json.dumps(page)
    assert SECRET not in dumped and 'ref-token' not in dumped and 'cursor' not in dumped and 'f:1' not in dumped
    assert [e['id'] for e in page['events']] == ['1', '3', '4']
    assert set(page['events'][0]) == {'id', 'kind', 'role', 'text', 'ts', 'truncated', 'files'}
    assert page['events'][0]['files'] == [{'label': 'chart.png', 'kind': 'image'}]
    assert page['events'][1]['text'] == 'Readsecond line'
    mail = page['events'][2]['mail'][0]
    assert 'path' not in mail and 'source' not in mail
    assert len(mail['body'].encode()) == history.MAX_MAIL_BODY and mail['truncated'] is True
    assert isinstance(page['older'], str) and history.TOKEN.fullmatch(page['older'])


def test_page_bounds_event_count_text_and_serialized_bytes():
    with cockpit(lambda q: (200, {'ok': True, 'events': [event(i) for i in range(300)], 'has_older': False})) as port:
        page = history.HistoryClient(port).page('Vega')
    assert len(page['events']) == history.PAGE_EVENTS and page['events'][-1]['id'] == '299' and page['limited'] is True
    # JSON escapes each é to six bytes, keeping this fixture under the upstream cap.
    raw = [event(i, text='é' * (30 * 1024)) for i in range(36)]
    with cockpit(lambda q: (200, {'ok': True, 'events': raw, 'has_older': False})) as port:
        page = history.HistoryClient(port).page('Vega')
    assert all(len(e['text'].encode()) <= history.MAX_TEXT and e['truncated'] for e in page['events'])
    assert len(json.dumps(page['events'], ensure_ascii=False).encode()) <= history.MAX_PAGE_BYTES
    assert page['events'][-1]['id'] == '35' and len(page['events']) < 36
    # The trimmed older events stay reachable through Show older.
    assert page['limited'] is True and page['older'] is not None and page['omitted'] is False


def test_issued_tokens_bind_the_agent_and_expire():
    now = [0.0]
    with cockpit(lambda q: (200, {'ok': True, 'events': [event(1)], 'before': 'f:%s' % q.get('before', ['77'])[0].split(':')[-1],
                                  'has_older': True})) as port:
        client = history.HistoryClient(port, clock=lambda: now[0])
        token = client.page('Vega')['older']
        with pytest.raises(LookupError):
            client.page('Atlas', token)
        client.page('Vega', token)
        assert parse_qs(urlsplit(Cockpit.requests[-1][0]).query)['before'] == ['f:77']
        now[0] += history.TOKEN_SECONDS + 1
        with pytest.raises(LookupError):
            client.page('Vega', token)


def test_unbound_records_return_only_a_fixed_code_and_reason_code():
    with cockpit(lambda q: (400, {'ok': False, 'error': 'history_unconfirmed', 'reason': 'see ' + SECRET,
                                  'reason_code': 'binding_missing'})) as port:
        with pytest.raises(history.HistoryUnavailable) as caught:
            history.HistoryClient(port).page('Atlas')
    assert caught.value.code == 'history_unconfirmed' and caught.value.reason_code == 'binding_missing'
    with cockpit(lambda q: (400, {'ok': False, 'error': SECRET, 'reason_code': '../' + SECRET})) as port:
        with pytest.raises(history.HistoryUnavailable) as caught:
            history.HistoryClient(port).page('Atlas')
    assert caught.value.code == 'history_unavailable' and caught.value.reason_code == ''


def test_relay_end_to_end_reports_reason_without_path():
    def reply(q):
        if q['agent'] == ['Atlas']:
            return 400, {'ok': False, 'error': 'history_stale', 'reason': SECRET, 'reason_code': 'receipt_predates_session'}
        return 200, {'ok': True, 'events': [event(1)], 'has_older': False}
    with cockpit(reply) as cport:
        with serving(reader(client=history.HistoryClient(cport))) as port:
            status, body = get(port, '/view/conversation?name=Atlas')
            assert status == 200 and SECRET.encode() not in body
            assert json.loads(body) == {'ok': False, 'error': 'history_stale', 'reason_code': 'receipt_predates_session'}
            status, body = get(port, '/view/conversation?name=Vega')
            assert status == 200 and json.loads(body)['events'][0]['text'] == 'line 1'
            assert get(port, '/view/conversation?name=Vega&page=' + 'a' * 32)[0] == 410


def test_cockpit_offline_or_wrong_type_is_unavailable():
    with pytest.raises(history.HistoryUnavailable):
        history.HistoryClient(1).page('Vega')
    class Html(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def do_GET(self):
            self.send_response(200); self.send_header('Content-Type', 'text/html'); self.end_headers()
            self.wfile.write(b'<script>alert(1)</script>')
    with serving(ThreadingHTTPServer(('127.0.0.1', 0), Html)) as port:
        with pytest.raises(history.HistoryUnavailable):
            history.HistoryClient(port).page('Vega')


# --- phone module: inert text, clears on close -------------------------------

HARNESS = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
class El{
 constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.parent=null;this._text='';this.className='';this.dataset={};this.hidden=false;this.attrs={};this.listeners={};this.scrollTop=0;this.style={};this.classList={add:(...xs)=>{this.className=[this.className,...xs].filter(Boolean).join(' ');}};}
 get isConnected(){let n=this;while(n.parent)n=n.parent;return n===body;}
 get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
 set textContent(v){this._text=String(v);this.children.forEach(c=>c.parent=null);this.children=[];}
 set innerHTML(v){throw Error('innerHTML must not be used');}
 get scrollHeight(){return 1000;} get clientHeight(){return 200;}
 appendChild(x){this.append(x);return x;}
 append(...xs){for(const x of xs){x.parent?.children.splice(x.parent.children.indexOf(x),1);x.parent=this;this.children.push(x);}}
 insertBefore(x,ref){if(!ref)return this.append(x);x.parent=this;this.children.splice(this.children.indexOf(ref),0,x);}
 remove(){if(this.parent){this.parent.children.splice(this.parent.children.indexOf(this),1);this.parent=null;}}
 replaceChildren(...xs){this.children.forEach(c=>c.parent=null);this.children=[];this._text='';this.append(...xs);}
 setAttribute(k,v){this.attrs[k]=String(v);} addEventListener(k,f){this.listeners[k]=f;}
 matches(sel){if(sel.startsWith('.'))return this.className.split(' ').includes(sel.slice(1));const m=/^\[data-id(?:="(.*)")?\]$/.exec(sel);if(m)return m[1]===undefined?'id' in this.dataset:this.dataset.id===m[1];return this.tagName===sel.toUpperCase();}
 querySelectorAll(sel){const scoped=sel.startsWith(':scope > ');const s=scoped?sel.slice(9):sel;const out=[];const walk=n=>{for(const c of n.children){if(c.matches(s))out.push(c);if(!scoped)walk(c);}};walk(this);return out;}
 querySelector(sel){return this.querySelectorAll(sel)[0]||null;}
 all(){return [this,...this.children.flatMap(c=>c.all())];}
}
const body=new El('body');const timers=new Map();let seq=0;const fetches=[];let docListeners={};
const doc={hidden:false,createTextNode:t=>{const n=new El('#text');n.textContent=t;return n;},createElement:t=>new El(t),addEventListener(k,f){docListeners[k]=f;}};
let reply=null;
const context={document:doc,console,TextEncoder,AbortController,URL,CSS:{escape:s=>s},Date,JSON,
 setTimeout(f,ms){timers.set(++seq,{f,ms});return seq;},clearTimeout(id){timers.delete(id);},
 addEventListener(){},
 fetch:async(url,opts)=>{fetches.push({url,opts});
  const aborted=new Promise((_,no)=>{if(context.honorAbort&&opts.signal)opts.signal.addEventListener('abort',()=>{const e=Error('aborted');e.name='AbortError';no(e);});});
  const v=await Promise.race([reply(url),aborted]);return {ok:true,status:v.status||200,text:async()=>JSON.stringify(v.body),json:async()=>v.body};}};
context.window=context;
vm.runInNewContext(MARKDOWN,context);
vm.runInNewContext(SOURCE,context);
const C=context.OrreryMobileConversation;
const settle=async()=>{for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r));};
const ev=i=>({id:String(i),kind:'text',role:'user',text:'m'+i,ts:'',truncated:false});
const range=(a,b)=>Array.from({length:b-a},(_,i)=>ev(a+i));
const page=(events,older=null)=>({body:{ok:true,agent:'Vega',events,older,limited:false}});
const ids=root=>root.all().filter(e=>e.dataset.id!==undefined).map(e=>e.dataset.id);
const olderButton=root=>root.all().find(e=>e.className==='conversation-older');
const pollTimer=()=>{const e=[...timers.entries()].find(([,t])=>t.ms===5000);assert.ok(e,'poll scheduled');timers.delete(e[0]);return e[1].f;};
const tick=async()=>{await pollTimer()();await settle();};
(async()=>{BODY;console.log('ok');})().catch(e=>{console.error(e);process.exitCode=1;});
'''


def run(body):
    node = shutil.which('node')
    if not node:
        pytest.skip('node is unavailable')
    source = (ROOT / 'dashboard/mobile/conversation.js').read_text()
    script = HARNESS.replace('MARKDOWN', json.dumps((ROOT / 'dashboard/mobile/conversation_markdown.js').read_text())).replace('SOURCE', json.dumps(source)).replace('BODY', body)
    result = subprocess.run([node, '-'], input=script, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.strip().endswith('ok')


def test_conversation_source_has_no_html_sinks():
    import re
    source = re.sub(r'/\*.*?\*/', '', (ROOT / 'dashboard/mobile/conversation.js').read_text(), flags=re.S)
    # The one image source is the checked PNG data URL; closing clears it.
    assert source.count('.src=') == 2 and "img.src=pngSource(v);" in source and "img.src='';" in source
    assert source.count("'data:image/png;base64,'+v.data") == 1
    source = source.replace('img.src=pngSource(v);', '').replace("img.src='';", '')
    # The one link is Save's hidden download anchor to the object URL it made
    # from the bytes the preview already shows.
    assert source.count('.href') == 1 and source.count('createObjectURL') == 1
    assert 'a.href=saveUrl;' in source and 'saveUrl=URL.createObjectURL(blob());' in source
    source = source.replace('a.href=saveUrl;', '').replace('saveUrl=URL.createObjectURL(blob());', '')
    for sink in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', '.href', '.src',
                 'srcset', 'blob:', 'createObjectURL', 'setAttribute(\'src', 'iframe',
                 'localStorage', 'sessionStorage', 'indexedDB', 'eval(', 'Function('):
        assert sink not in source, sink


def test_recorded_html_stays_text_and_tables_are_built_as_dom():
    run(r'''
      const evil='<img src=x onerror=alert(1)><script>alert(2)</script>';
      reply=async()=>({body:{ok:true,agent:'Vega',events:[
        {id:'1',kind:'text',role:'user',text:evil,ts:'2026-11-04T01:00:00Z',truncated:false},
        {id:'2',kind:'text',role:'assistant',text:'Results:\n| a | b |\n|---|---|\n| <b>1</b> | 2 |\n```\n<i>code</i>\n```',ts:'',truncated:false,files:[{label:'<svg onload=x>.png',kind:'image'}]},
        {id:'3',kind:'mail',role:'user',text:'s',ts:'',truncated:false,mail:[{direction:'received',sender:'Lead',recipients:[],subject:'<a href=x>s</a>',importance:'normal',coverage:'complete',body:evil,truncated:false}]}],
        older:null,limited:false}});
      const root=C.mount('Vega');body.append(root);await settle();
      const tags=new Set(root.all().map(e=>e.tagName));
      for(const bad of ['IMG','SCRIPT','A','SVG','IFRAME','B','I'])assert.ok(!tags.has(bad),bad);
      assert.ok(root.textContent.includes(evil));
      assert.ok(root.textContent.includes('<svg onload=x>.png'));
      const table=root.all().find(e=>e.tagName==='TABLE');assert.ok(table);
      assert.deepEqual(table.all().filter(e=>e.tagName==='TD').map(e=>e.textContent),['<b>1</b>','2']);
      assert.equal(root.all().find(e=>e.tagName==='PRE'&&e.className==='markdown-code').textContent,'<i>code</i>\n');
      assert.ok(fetches.every(f=>f.url.startsWith('/view/conversation?name=Vega')&&!/path|cursor|before/.test(f.url)));
    ''')


def test_close_and_background_clear_dom_requests_timers_and_pending():
    run(r'''
      reply=async()=>({body:{ok:true,agent:'Vega',events:[{id:'1',kind:'text',role:'assistant',text:'kept?',ts:'',truncated:false}],older:'a'.repeat(32),limited:false}});
      let root=C.mount('Vega');body.append(root);await settle();
      C.addPending('draft words');assert.ok(root.textContent.includes('draft words'));
      assert.ok([...timers.values()].some(t=>t.ms===5000));
      C.unmount();
      assert.equal(root.children.length,0);assert.equal(root.textContent,'');assert.equal(C.element(),null);
      assert.equal(timers.size,0);
      // A late response from before the close cannot restore content.
      let release;reply=()=>new Promise(r=>release=r);
      root=C.mount('Vega');body.append(root);await settle();C.unmount();
      release({body:{ok:true,agent:'Vega',events:[{id:'9',kind:'text',role:'user',text:'late',ts:'',truncated:false}],older:null,limited:false}});
      await settle();assert.ok(!root.textContent.includes('late'));assert.equal(timers.size,0);
      reply=async()=>({body:{ok:true,agent:'Vega',events:[{id:'1',kind:'text',role:'assistant',text:'again',ts:'',truncated:false}],older:null,limited:false}});
      root=C.mount('Vega');body.append(root);await settle();assert.ok(root.textContent.includes('again'));
      doc.hidden=true;docListeners.visibilitychange();
      assert.equal(root.textContent,'');assert.equal(timers.size,0);
    ''')


def test_unbound_reason_is_shown_without_any_record():
    run(r'''
      reply=async()=>({body:{ok:false,error:'history_unconfirmed',reason_code:'binding_missing'}});
      const root=C.mount('Atlas');body.append(root);await settle();
      assert.ok(root.textContent.includes('not verified'));assert.ok(root.textContent.includes('(binding_missing)'));
      assert.equal(root.all().filter(e=>e.dataset.id!==undefined).length,0);
    ''')


def test_pending_row_is_local_and_settles_when_the_record_has_it():
    run(r'''
      let events=[];
      reply=async()=>({body:{ok:true,agent:'Vega',events,older:null,limited:false}});
      const root=C.mount('Vega');body.append(root);await settle();
      C.addPending('update the changelog');
      assert.equal(root.all().filter(e=>e.className.includes('conversation-pending')).length,1);
      assert.ok(fetches.every(f=>!f.opts.method||f.opts.method==='GET'));
      events=[{id:'7',kind:'text',role:'user',text:'update the changelog',ts:'',truncated:false}];
      const poll=[...timers.entries()].find(([,t])=>t.ms===5000);timers.delete(poll[0]);await poll[1].f();await settle();
      assert.equal(root.all().filter(e=>e.className.includes('conversation-pending')).length,0);
      C.unmount();
    ''')


# --- sheet integration: tabs reuse the existing Terminal review/send path ------

def run_sheet(body, terminal=True):
    import importlib.util
    node = shutil.which('node')
    if not node:
        pytest.skip('node is unavailable')
    spec = importlib.util.spec_from_file_location('scroll_harness', ROOT / 'tests/test_mobile_terminal_scroll.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    harness = module.HARNESS
    for old, new in [
        ("enabled:true,ack", "enabled:TERMINAL,ack"),
        ("vm.runInNewContext(SOURCE,context);", """let convRoot=null;const mounted=[],pend=[];let unmounts=0;
context.queueMicrotask=queueMicrotask;
context.OrreryMobileConversation={enabled:async()=>true,mount(n){convRoot=new Element('div','conversation');convRoot.id='conversation';mounted.push(n);return convRoot;},
 element(){return convRoot;},unmount(){if(convRoot)unmounts++;convRoot=null;},addPending(t){pend.push(t);},restore(){}};
vm.runInNewContext(SOURCE,context);"""),
        ("(async()=>{await settle();await open();BODY;context.OrreryMobileTerminal.close();console.log('ok');})()",
         "(async()=>{await settle();BODY;console.log('ok');})()"),
    ]:
        assert harness.count(old) == 1, old
        harness = harness.replace(old, new)
    harness = harness.replace("setAttribute(){}", "setAttribute(k,v){this[k]=v;}")
    harness = harness.replace("createElement:t=>new Element(t)", "createElement:t=>new Element(t),createElementNS:(_ns,t)=>new Element(t)")
    harness = harness.replace("document:doc,console", "history:{state:null,pushState(v){this.state=v;},back(){}},document:doc,console")
    source = (ROOT / 'dashboard/mobile/mobile.js').read_text()
    source = '/* Common, opt-in pane terminal.' + source.split('/* Common, opt-in pane terminal.', 1)[1]
    script = (harness.replace('TERMINAL', 'true' if terminal else 'false')
              .replace('SOURCE', json.dumps(source)).replace('BODY', body))
    result = subprocess.run([node, '-'], input=script, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.strip() == 'ok'


def test_sheet_opens_on_conversation_and_sends_only_through_the_reviewed_terminal_path():
    run_sheet(r'''
      await context.OrreryMobileTerminal.open('ScrollFixture');await settle();
      assert.deepEqual(mounted,['ScrollFixture']);assert.ok(q('#conversation'));
      assert.equal(q('#terminal-output'),null,'conversation tab hides the terminal tail');
      assert.equal(q('#terminal-text'),null,'no compose before a pane is chosen');
      q('.reader-bar').children.find(b=>b['aria-label']==='Terminal').onclick();await settle();
      button('Pane 1ScrollFixture').onclick();await settle();
      assert.ok(q('#terminal-output'));
      q('.reader-bar').children.find(b=>b['aria-label']==='Conversation').onclick();await settle();
      assert.ok(q('#conversation'));assert.equal(q('#terminal-output'),null);
      q('.reader-compose-toggle').onclick();
      const field=q('#terminal-text');field.value='synthetic line';field.oninput();
      await button('Review input').onclick();await settle();
      assert.ok(calls.some(c=>c.op==='prepare'),'review uses the existing prepare step');
      assert.ok(!calls.some(c=>c.op==='commit'),'nothing is sent before Send once');
      await button('Send once').onclick();await settle();
      assert.deepEqual(calls.filter(c=>c.op==='commit').length,1);
      assert.deepEqual(pend,['synthetic line']);
      context.OrreryMobileTerminal.close();assert.ok(unmounts>=1);assert.equal(q('#conversation'),null);
    ''')


def test_history_without_terminal_opt_in_is_read_only():
    run_sheet(r'''
      await context.OrreryMobileTerminal.open('ScrollFixture');await settle();
      assert.ok(q('#conversation'));
      assert.equal(q('#terminal-text'),null);assert.equal(button('Review input'),undefined);
      assert.equal(button('Terminal'),undefined,'no Terminal tab without its own opt-in');
      assert.ok(!calls.some(c=>c.op&&c.op!=='terminal'),'no pane operation is requested: '+JSON.stringify(calls.map(c=>c.op)));
      context.OrreryMobileTerminal.close();
    ''', terminal=False)


# --- review fixes: wire size, token lifetime, request isolation, order ------

def phone_cap():
    import re
    source = (ROOT / 'dashboard/mobile/conversation.js').read_text()
    a, b = re.search(r'MAX_BYTES=(\d+)\*(\d+)', source).groups()
    return int(a) * int(b)


def test_japanese_page_fits_the_phone_cap_on_the_wire():
    # 40 long Japanese reports with emoji: about 480 KiB as UTF-8, twice that as \\u escapes.
    text = ('日本語の報告です。検証は済みました🙂' * 400)[:4000]
    raw = [event(i, text=text) for i in range(40)]
    with cockpit(lambda q: (200, {'ok': True, 'events': raw, 'has_older': False})) as cport:
        with serving(reader(client=history.HistoryClient(cport))) as port:
            status, body = get(port, '/view/conversation?name=Vega')
    page = json.loads(body.decode('utf-8'))
    assert status == 200 and len(page['events']) >= 30
    assert len(body) > 384 * 1024, 'fixture must be large enough to exercise the cap'
    assert len(body) <= phone_cap()
    assert page['events'][-1]['text'].startswith('日本語の報告です')


def test_lone_surrogates_from_cockpit_do_not_break_the_page():
    with cockpit(lambda q: (200, {'ok': True, 'events': [event(1, text='ok \ud800 end')], 'has_older': False})) as cport:
        with serving(reader(client=history.HistoryClient(cport))) as port:
            status, body = get(port, '/view/conversation?name=Vega')
    assert status == 200 and json.loads(body.decode('utf-8'))['events'][0]['text'].startswith('ok ')


def test_latest_page_reuses_one_token_and_keeps_it_alive_while_polled():
    now = [0.0]
    with cockpit(lambda q: (200, {'ok': True, 'events': [event(5)], 'before': 'f:5', 'has_older': True})) as port:
        client = history.HistoryClient(port, clock=lambda: now[0])
        token = client.page('Vega')['older']
        for _ in range(3):
            now[0] += history.TOKEN_SECONDS - 60
            assert client.page('Vega')['older'] == token
        assert len(client._tokens) == 1
        client.page('Vega', token)
        assert parse_qs(urlsplit(Cockpit.requests[-1][0]).query)['before'] == ['f:5']


def test_upstream_total_deadline_stops_a_trickling_cockpit(monkeypatch):
    stop = threading.Event()
    class Trickle(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def do_GET(self):
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', '200'); self.end_headers()
            try:
                for _ in range(200):
                    if stop.wait(.05):
                        return
                    self.wfile.write(b' '); self.wfile.flush()
            except OSError:
                pass
    monkeypatch.setattr(history, 'UPSTREAM_TIMEOUT', 1)
    import time
    with serving(ThreadingHTTPServer(('127.0.0.1', 0), Trickle)) as port:
        started = time.monotonic()
        try:
            with pytest.raises(history.HistoryUnavailable):
                history.HistoryClient(port).page('Vega')
            elapsed = time.monotonic() - started
        finally:
            stop.set()
    assert elapsed < 2.5, elapsed


def test_real_snapshot_store_knows_only_published_roster_names():
    import importlib.util
    spec = importlib.util.spec_from_file_location('projection_tests', ROOT / 'tests/test_mobile_projection.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    from dashboard.mobile_projection import SnapshotStore
    data = module.inputs()
    store = SnapshotStore(lambda key: data[key], clock=lambda: 100.0)
    present = data['agents']['agents'][0]['name']
    assert not store.known_name(present), 'nothing is published before a snapshot'
    store.snapshot()
    assert store.known_name(present) and not store.known_name('NotInRoster')


def test_show_older_uses_the_latest_token_and_recovers_after_expiry():
    run(r'''
      const tok=c=>c.repeat(32);let latest=tok('a');const expired=new Set();
      reply=async url=>{const m=/&page=([a-f0-9]{32})/.exec(url);
        if(!m)return page(range(10,12),latest);
        if(expired.has(m[1]))return {status:410,body:{ok:false,error:'page_expired'}};
        return page(range(8,10));};
      let root=C.mount('Vega');body.append(root);await settle();
      assert.equal(olderButton(root).hidden,false);
      latest=tok('b');await tick();expired.add(tok('a'));
      await olderButton(root).onclick();await settle();
      assert.ok(fetches.at(-1).url.endsWith('&page='+tok('b')),fetches.at(-1).url);
      assert.deepEqual(ids(root),['8','9','10','11']);
      C.unmount();
      // A token that expired anyway brings back the latest page and its button.
      latest=tok('c');root=C.mount('Vega');body.append(root);await settle();
      expired.add(tok('c'));latest=tok('d');
      await olderButton(root).onclick();await settle();
      assert.ok(root.textContent.includes('expired'));
      assert.deepEqual(ids(root),['10','11']);
      assert.equal(olderButton(root).hidden,false);
      await olderButton(root).onclick();await settle();
      assert.deepEqual(ids(root),['8','9','10','11']);
      C.unmount();
    ''')


def test_poll_and_show_older_do_not_abort_each_other():
    run(r'''
      let hold=null;
      reply=url=>{if(hold&&hold.test(url))return new Promise(r=>reply.release=r);
        return Promise.resolve(url.endsWith('e'.repeat(32))?page(range(6,8)):url.includes('&page=')?page(range(8,10),'e'.repeat(32)):page(range(10,12),'a'.repeat(32)));};
      const root=C.mount('Vega');body.append(root);await settle();
      hold=/&page=/;const loading=olderButton(root).onclick();await settle();
      const olderFetch=fetches.at(-1);hold=null;await tick();
      assert.equal(olderFetch.opts.signal.aborted,false,'poll aborted Show older');
      reply.release(page(range(8,10),'e'.repeat(32)));await loading;await settle();
      assert.deepEqual(ids(root),['8','9','10','11']);
      hold=/name=Vega$/;const polling=pollTimer()();await settle();const pollFetch=fetches.at(-1);
      hold=null;await olderButton(root).onclick();await settle();
      assert.equal(pollFetch.opts.signal.aborted,false,'Show older aborted the poll');
      reply.release(page(range(10,13),'a'.repeat(32)));await polling;await settle();
      assert.ok(!root.textContent.includes('Retrying'));
      assert.deepEqual(ids(root),['6','7','8','9','10','11','12']);
      C.unmount();
    ''')


def test_older_pages_stay_in_order_and_the_cap_leaves_no_gap():
    run(r'''
      const tok=n=>n.toString(16).padStart(32,'0');
      reply=async url=>{const m=/&page=([a-f0-9]{32})/.exec(url);
        if(!m)return page(range(latestFrom,500+extra),tok(latestFrom));
        const end=parseInt(m[1],16);return page(range(end-40,end),tok(end-40));};
      let latestFrom=470,extra=0;
      const root=C.mount('Vega');body.append(root);await settle();
      for(let i=0;i<5;i++){await olderButton(root).onclick();await settle();}
      const expect=(a,b)=>range(a,b).map(e=>e.id);
      assert.deepEqual(ids(root),expect(300,500));
      assert.equal(olderButton(root).hidden,true,'no older page past the kept limit');
      extra=1;latestFrom=471;await tick();
      assert.deepEqual(ids(root),expect(301,501));
      C.unmount();
    ''')


def test_reopening_the_same_agent_ignores_the_previous_sheets_response():
    run(r'''
      const held=[];reply=()=>new Promise(r=>held.push(r));
      let root=C.mount('Vega');body.append(root);await settle();C.unmount();
      root=C.mount('Vega');body.append(root);await settle();
      held[0](page([{...ev(1),text:'stale'}]));await settle();
      assert.ok(!root.textContent.includes('stale'));
      held[1](page([{...ev(2),text:'fresh'}]));await settle();
      assert.ok(root.textContent.includes('fresh'));
      C.unmount();
    ''')


def test_review_from_the_conversation_tab_shows_the_terminal_tail_before_send():
    run_sheet(r'''
      await context.OrreryMobileTerminal.open('ScrollFixture');await settle();
      q('.reader-bar').children.find(b=>b['aria-label']==='Terminal').onclick();await settle();
      button('Pane 1ScrollFixture').onclick();await settle();
      const tail=q('#terminal-output').textContent;assert.ok(tail.includes('line'));
      q('.reader-bar').children.find(b=>b['aria-label']==='Conversation').onclick();await settle();
      q('.reader-compose-toggle').onclick();
      const field=q('#terminal-text');field.value='synthetic line';field.oninput();
      await button('Review input').onclick();await settle();
      const shown=q('#terminal-output');
      assert.ok(shown,'review shows the terminal tail');
      assert.ok(shown.textContent.includes('line'),'tail text is visible: '+shown.textContent);
      assert.match(q('.terminal-observed').textContent,/40 lines.*6 KiB/);
      assert.ok(button('Send once'));
      context.OrreryMobileTerminal.close();
    ''')



# --- review r2: trimmed pages never skip messages ----------------------------

def paged_cockpit(total, text, cursors=True):
    """Cursor-honouring synthetic cockpit: cursor c:N means 'before event N'."""
    def reply(q):
        end = int(q['before'][0].split(':')[1]) if 'before' in q else total
        start = max(0, end - int(q['limit'][0]))
        events = [{'id': 'e%d' % i, 'kind': 'text', 'role': 'assistant', 'text': text, 'ts': '',
                   **({'cursor_before': 'c:%d' % i} if cursors else {})} for i in range(start, end)]
        return 200, {'ok': True, 'events': events, 'before': 'c:%d' % start, 'has_older': start > 0}
    return reply


def walk(client):
    pages, token = [], None
    while True:
        page = client.page('Vega', token)
        pages.append(page)
        token = page['older']
        if token is None:
            return pages


def test_size_trimmed_pages_continue_from_the_first_kept_message():
    with cockpit(paged_cockpit(200, 'あ' * 6000)) as port:
        pages = walk(history.HistoryClient(port))
    assert len(pages[0]['events']) < history.PAGE_EVENTS, 'fixture must trim the latest page'
    shown = [e['id'] for p in reversed(pages) for e in p['events']]
    assert shown == ['e%d' % i for i in range(200)]
    assert not any(p['omitted'] for p in pages)


def test_trimmed_page_without_cursors_reports_the_omission():
    with cockpit(paged_cockpit(200, 'あ' * 6000, cursors=False)) as port:
        page = history.HistoryClient(port).page('Vega')
    assert page['omitted'] is True and page['older'] is None


def test_one_oversized_message_is_shortened_not_dropped():
    bodies = [{'direction': 'received', 'sender': 'Lead', 'recipients': [], 'subject': 's', 'importance': 'normal',
               'coverage': 'complete', 'body': '\x01' * (16 * 1024)} for _ in range(8)]
    raw = [event(1, text='\x01' * (16 * 1024), mail=bodies, cursor_before='c:1')]
    with cockpit(lambda q: (200, {'ok': True, 'events': raw, 'has_older': False})) as port:
        page = history.HistoryClient(port).page('Vega')
    assert [e['id'] for e in page['events']] == ['1'] and page['events'][0]['truncated'] is True
    assert len(history.encode(page['events'])) <= history.MAX_PAGE_BYTES and page['omitted'] is False


def test_phone_marks_where_messages_are_omitted():
    run(r"""
      reply=async url=>url.includes('&page=')?{body:{...page(range(5,7)).body,omitted:true}}:page(range(10,12),'a'.repeat(32));
      const root=C.mount('Vega');body.append(root);await settle();
      assert.ok(!root.textContent.includes('omitted'));
      await olderButton(root).onclick();await settle();
      const kids=root.all().filter(e=>e.dataset.id!==undefined||e.className==='conversation-omitted').map(e=>e.dataset.id||'gap');
      assert.deepEqual(kids,['gap','5','6','10','11']);
      assert.ok(root.textContent.includes('Some messages are omitted here'));
      C.unmount();
    """)


def test_a_replaced_poll_is_not_counted_as_a_failure():
    run(r"""
      context.honorAbort=true;let hold=false;const tok=c=>c.repeat(32);
      reply=url=>{if(url.includes('&page='))return Promise.resolve({status:410,body:{ok:false,error:'page_expired'}});
        if(hold)return new Promise(()=>{});return Promise.resolve(page(range(10,12),tok('a')));};
      const root=C.mount('Vega');body.append(root);await settle();
      hold=true;const polling=pollTimer()();await settle();hold=false;
      await olderButton(root).onclick();await polling;await settle();
      assert.ok(!root.textContent.includes('Retrying'),root.textContent);
      assert.deepEqual([...timers.values()].map(t=>t.ms),[5000]);
      C.unmount();
    """)


def test_omitted_marker_survives_later_polls():
    run(r"""
      const kids=root=>root.all().filter(e=>e.dataset.id!==undefined||e.className==='conversation-omitted').map(e=>e.dataset.id||'gap');
      let first={...page(range(10,12)).body,omitted:true};
      reply=async url=>({body:first});
      let root=C.mount('Vega');body.append(root);await settle();
      assert.deepEqual(kids(root),['gap','10','11']);
      first={...page(range(10,13)).body,omitted:true};await tick();await tick();
      assert.deepEqual(kids(root),['gap','10','11','12'],'first-page marker kept after polls');
      C.unmount();
      reply=async url=>url.includes('&page=')?{body:{...page(range(5,7)).body,omitted:true}}:page(range(10,12),'a'.repeat(32));
      root=C.mount('Vega');body.append(root);await settle();
      await olderButton(root).onclick();await settle();await tick();
      assert.deepEqual(kids(root),['gap','5','6','10','11'],'Show older marker kept after a poll');
      C.unmount();
    """)


# --- slow cockpit under load ------------------------------------------------------

def slow_cockpit(seconds):
    stop = threading.Event()
    class Slow(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def do_GET(self):
            if stop.wait(seconds):
                return
            data = json.dumps({'ok': True, 'events': [event(1)], 'has_older': False}).encode()
            try:
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
            except OSError:
                pass
    return stop, ThreadingHTTPServer(('127.0.0.1', 0), Slow)


def test_a_cockpit_that_takes_six_seconds_under_load_still_answers():
    stop, server = slow_cockpit(6)
    with serving(server) as port:
        try:
            page = history.HistoryClient(port).page('Vega')
        finally:
            stop.set()
    assert [e['id'] for e in page['events']] == ['1']


def test_running_out_of_time_is_its_own_code_on_the_wire(monkeypatch):
    monkeypatch.setattr(history, 'UPSTREAM_TIMEOUT', .5)
    stop, server = slow_cockpit(3)
    with serving(server) as port:
        try:
            with pytest.raises(history.HistoryUnavailable) as caught:
                history.HistoryClient(port).page('Vega')
            assert caught.value.code == 'history_slow'
            with serving(reader(client=history.HistoryClient(port))) as relay_port:
                status, body = get(relay_port, '/view/conversation?name=Vega')
        finally:
            stop.set()
    assert status == 503 and json.loads(body) == {'ok': False, 'error': 'history_slow', 'reason_code': ''}


def test_the_phone_waits_longer_than_the_relay():
    import re
    source = (ROOT / 'dashboard/mobile/conversation.js').read_text()
    # Either a literal per request or one named wait shared by both requests.
    named = re.search(r'\bWAIT=(\d+)', source)
    waits = [int(x) for x in re.findall(r'c\.abort\(\);\},(\d+)\)', source)]
    if named:
        assert 'c.abort();},WAIT)' in source
        waits.append(int(named.group(1)))
    assert waits and min(waits) > history.UPSTREAM_TIMEOUT * 1000 + 2000, (waits, history.UPSTREAM_TIMEOUT)


def test_phone_says_a_slow_record_is_worth_waiting_for_and_offers_retry():
    run(r'''
      let slow=true;
      reply=async()=>slow?{status:503,body:{ok:false,error:'history_slow',reason_code:''}}:page(range(1,3));
      const root=C.mount('Vega');body.append(root);await settle();
      assert.ok(root.textContent.includes('Taking longer than usual'),root.textContent);
      assert.ok(!root.textContent.includes('unavailable'),root.textContent);
      const retry=root.all().find(e=>e.tagName==='BUTTON'&&e.textContent==='Retry');assert.ok(retry&&!retry.hidden,'retry button');
      slow=false;const before=fetches.length;await retry.onclick();await settle();
      assert.equal(fetches.length,before+1,'retry asks at once');
      assert.deepEqual(ids(root),['1','2']);assert.ok(retry.hidden,'retry hides once it works');
      // Already shown messages stay while a later poll is slow.
      slow=true;await tick();
      assert.deepEqual(ids(root),['1','2']);assert.ok(root.textContent.includes('Taking longer than usual'));
      C.unmount();
    ''')


def test_phone_treats_its_own_wait_running_out_as_slow():
    run(r'''
      context.honorAbort=true;
      reply=()=>new Promise(()=>{});
      const root=C.mount('Vega');body.append(root);await settle();
      assert.ok(root.textContent.includes('Loading the record'),'loading stays while waiting');
      const wait=[...timers.entries()].find(([,t])=>t.ms>5000);assert.ok(wait,'request deadline');
      timers.delete(wait[0]);wait[1].f();await settle();
      assert.ok(root.textContent.includes('Taking longer than usual'),root.textContent);
      assert.ok(root.all().some(e=>e.tagName==='BUTTON'&&e.textContent==='Retry'&&!e.hidden));
      C.unmount();
    ''')


def test_generated_notification_is_a_short_activity_not_a_user_message():
    raw = dict(id='notification', kind='system', role='system', text='Background task completed · check',
               system_detail='Status: completed\nOutput: /fixture/private-output.txt', ts='')
    event = history.project_event(raw)
    assert event['kind'] == 'tool' and event['role'] == 'assistant'
    assert event['text'] == 'Background task completed · check'
    assert 'system_detail' not in event and '/fixture/' not in json.dumps(event)
