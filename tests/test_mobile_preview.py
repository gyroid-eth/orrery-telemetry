"""Opt-in file and image previews in the mobile conversation, on loopback fixtures.

The relay keeps cockpit reference IDs to itself and gives the phone its own
opaque IDs. Only a synthetic cockpit is used; no real file, transcript,
cockpit, Tailscale, Mail database or production port is touched.
"""
import base64
from contextlib import contextmanager
import json
import struct
import threading
import zlib
from urllib.parse import parse_qs, urlsplit

import pytest

from dashboard import mobile_history as history
from test_mobile_history import OWNER, Store, cockpit, Cockpit, event, get, reader, run, serving

COCKPIT_REF = 'Ck' + 'x' * 28 + '_-'
SECRET = '/Users/someone/private/notes.md'
SIDE = getattr(history, 'MAX_PREVIEW_SIDE', 8192)


def png(width=3, height=2, extra=()):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    raw = b''.join(b'\0' + b'\x10\x20\x30\xff' * width for _ in range(height))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
            + b''.join(chunk(k, d) for k, d in extra)
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def image(data=None, **extra):
    data = png() if data is None else data
    return {'ok': True, 'kind': 'image', 'mime': 'image/png', 'data': base64.b64encode(data).decode(),
            'width': 3, 'height': 2, 'label': 'chart.png', 'notice': 'Current file · may differ from the recorded edit',
            **extra}


def text(body, **extra):
    return {'ok': True, 'kind': 'text', 'mime': 'text/plain', 'text': body, 'label': 'notes.md',
            'notice': 'Current file · may differ from the recorded edit', 'path': SECRET, **extra}


def fake_cockpit(preview):
    """Conversation pages carry one cockpit reference; previews answer with ``preview``."""
    def reply(q):
        if 'ref' in q:
            return preview(q['ref'][0])
        return 200, {'ok': True, 'agent': q['agent'][0], 'provider': 'claude', 'has_older': False,
                     'events': [event(1, references=[{'id': COCKPIT_REF, 'label': 'notes.md', 'kind': 'file', 'path': SECRET},
                                                     {'id': 'not-a-cockpit-ref', 'label': 'other.md', 'kind': 'file'}])]}
    return cockpit(reply)


def chip_ref(page):
    return page['events'][0]['files'][0]['ref']


# --- reference IDs ------------------------------------------------------------

def test_page_gives_relay_issued_reference_ids_and_never_the_cockpit_ones():
    with fake_cockpit(lambda ref: (200, text('x'))) as port:
        client = history.HistoryClient(port)
        first, again = client.page('Vega'), client.page('Vega')
    dumped = json.dumps(first)
    assert COCKPIT_REF not in dumped and SECRET not in dumped
    files = first['events'][0]['files']
    assert history.TOKEN.fullmatch(files[0]['ref']) and files[0]['label'] == 'notes.md'
    assert files[1] == {'label': 'other.md', 'kind': 'file'}, 'a malformed cockpit reference is not previewable'
    assert chip_ref(again) == chip_ref(first), 'polling reuses the ID instead of growing the table'


def test_preview_calls_the_fixed_cockpit_route_with_only_the_kept_reference():
    with fake_cockpit(lambda ref: (200, text('# Notes\n<script>x</script>'))) as port:
        client = history.HistoryClient(port)
        with serving(reader(client=client)) as relay_port:
            status, body = get(relay_port, '/view/conversation?name=Vega')
            ref = chip_ref(json.loads(body))
            Cockpit.requests.clear()
            status, body = get(relay_port, '/view/conversation/preview?name=Vega&ref=' + ref)
        path, headers = Cockpit.requests[0]
    assert status == 200
    assert urlsplit(path).path == '/telemetry/conversation/preview'
    assert parse_qs(urlsplit(path).query) == {'ref': [COCKPIT_REF]}
    assert not {'Origin', 'Tailscale-User-Login', 'Cookie'} & set(headers)
    result = json.loads(body)
    assert result == {'ok': True, 'kind': 'text', 'label': 'notes.md', 'text': '# Notes\n<script>x</script>',
                      'truncated': False, 'notice': 'Current file · may differ from the recorded edit'}


@pytest.mark.parametrize('query,status', [
    ('name=Vega&ref=' + 'a' * 32 + '&path=/tmp/x', 400), ('name=Vega&ref=' + COCKPIT_REF, 400),
    ('name=Vega&ref=/Users/x/notes.md', 400), ('name=Vega', 400), ('ref=' + 'a' * 32, 400),
    ('name=Nobody&ref=' + 'a' * 32, 404), ('name=Vega&ref=' + 'a' * 32, 410),
])
def test_preview_takes_only_a_name_and_an_issued_id(query, status):
    with fake_cockpit(lambda ref: pytest.fail('cockpit must not be asked')) as port:
        with serving(reader(client=history.HistoryClient(port))) as relay_port:
            assert get(relay_port, '/view/conversation/preview?' + query)[0] == status
        assert Cockpit.requests == []


def test_an_id_issued_for_one_agent_does_not_open_another_agents_file():
    with fake_cockpit(lambda ref: pytest.fail('cockpit must not be asked')) as port:
        client = history.HistoryClient(port)
        ref = chip_ref(client.page('Vega'))
        with serving(reader(client=client)) as relay_port:
            Cockpit.requests.clear()
            assert get(relay_port, '/view/conversation/preview?name=Atlas&ref=' + ref)[0] == 410
        assert Cockpit.requests == []


def test_preview_route_is_absent_when_history_is_off():
    with serving(reader(enabled=False)) as port:
        assert get(port, '/view/conversation/preview?name=Vega&ref=' + 'a' * 32)[0] == 404


def test_preview_ids_expire_with_the_page_and_cockpit_expiry_is_410():
    now = [0.0]
    with fake_cockpit(lambda ref: (400, {'ok': False, 'error': 'reference_expired'})) as port:
        client = history.HistoryClient(port, clock=lambda: now[0])
        ref = chip_ref(client.page('Vega'))
        with pytest.raises(LookupError):
            client.preview('Vega', ref)
        now[0] = history.TOKEN_SECONDS + 1
        Cockpit.requests.clear()
        with pytest.raises(LookupError):
            client.preview('Vega', ref)
        assert Cockpit.requests == []


# --- bounded results ------------------------------------------------------------

def preview_of(answer):
    with fake_cockpit(lambda ref: answer) as port:
        client = history.HistoryClient(port)
        return client.preview('Vega', chip_ref(client.page('Vega')))


def test_text_preview_keeps_only_the_start():
    result = preview_of((200, text('あ' * history.MAX_PREVIEW_TEXT)))
    assert len(result['text'].encode()) <= history.MAX_PREVIEW_TEXT and result['truncated'] is True
    assert 'path' not in result


def test_image_preview_passes_only_a_plain_bounded_png():
    result = preview_of((200, image()))
    assert result == {'ok': True, 'kind': 'image', 'label': 'chart.png', 'width': 3, 'height': 2,
                      'data': base64.b64encode(png()).decode(), 'notice': 'Current file · may differ from the recorded edit'}


@pytest.mark.parametrize('answer,code', [
    (image(png(extra=[(b'tEXt', b'Comment\0' + SECRET.encode())])), 'preview_unavailable'),
    (image(png(), width=4), 'preview_unavailable'),
    (image(png()[:-4] + b'\0\0\0\0'), 'preview_unavailable'),
    (image(png(), mime='text/html'), 'preview_type_denied'),
    ({**image(), 'data': '<svg onload=x>'}, 'preview_unavailable'),
    (image(png(4000, 2001), width=4000, height=2001), 'preview_too_large'),
    (image(png(SIDE + 1, 1), width=SIDE + 1, height=1), 'preview_too_large'),
    ({'ok': True, 'kind': 'html', 'text': '<b>x</b>'}, 'preview_type_denied'),
])
def test_unsafe_or_oversized_images_are_refused(answer, code):
    with pytest.raises(history.HistoryUnavailable) as caught:
        preview_of((200, answer))
    assert caught.value.code == code


def test_an_oversized_cockpit_answer_is_refused_before_reading(monkeypatch):
    monkeypatch.setattr(history, 'MAX_PREVIEW_UPSTREAM', 4096)
    with pytest.raises(history.HistoryUnavailable) as caught:
        preview_of((200, text('x' * 8192)))
    assert caught.value.code == 'preview_too_large'


@pytest.mark.parametrize('error', ['preview_path_denied', 'preview_too_large', 'preview_type_denied',
                                   'preview_changed', 'history_unconfirmed', 'something /Users/x', ['preview_changed']])
def test_cockpit_refusals_reach_the_phone_as_fixed_codes(error):
    with fake_cockpit(lambda ref: (400, {'ok': False, 'error': error, 'path': SECRET})) as port:
        client = history.HistoryClient(port)
        with serving(reader(client=client)) as relay_port:
            ref = chip_ref(json.loads(get(relay_port, '/view/conversation?name=Vega')[1]))
            status, body = get(relay_port, '/view/conversation/preview?name=Vega&ref=' + ref)
    assert SECRET.encode() not in body
    assert json.loads(body)['error'] == (error if isinstance(error, str) and error.startswith(('preview_', 'history_')) else 'preview_unavailable')


# --- phone ---------------------------------------------------------------------

PNG64 = base64.b64encode(png()).decode()
REF = 'b' * 32


def phone(body):
    run(r'''
      const PNG64=%s,REF=%s;
      const withFile=(files)=>({body:{ok:true,agent:'Vega',older:null,limited:false,events:[{id:'1',kind:'text',role:'assistant',text:'see file',ts:'',truncated:false,files}]}});
      const chip=root=>root.all().find(e=>e.className.startsWith('conversation-chip'));
      const panel=root=>root.all().find(e=>e.className==='conversation-preview');
      ''' % (json.dumps(PNG64), json.dumps(REF)) + body)


def test_phone_shows_a_text_preview_as_text_and_closes_it():
    phone(r'''
      const evil='<img src=x onerror=alert(1)><script>alert(2)</script>';
      reply=async url=>url.startsWith('/view/conversation/preview?')
        ?{body:{ok:true,kind:'text',label:'notes.md',text:evil,truncated:true,notice:'Current file · may differ from the recorded edit'}}
        :withFile([{label:'notes.md',kind:'file',ref:REF},{label:'plain.md',kind:'file'}]);
      const root=C.mount('Vega');body.append(root);await settle();
      const chips=root.all().filter(e=>e.className.startsWith('conversation-chip'));
      assert.equal(chips[0].tagName,'BUTTON');assert.equal(chips[1].tagName,'SPAN','no ID, no preview');
      await chips[0].onclick();await settle();
      assert.equal(fetches.at(-1).url,'/view/conversation/preview?name=Vega&ref='+REF);
      const p=panel(root);assert.ok(p);
      const pre=p.all().find(e=>e.tagName==='PRE');assert.equal(pre.textContent,evil);
      assert.ok(!p.all().some(e=>['IMG','SCRIPT'].includes(e.tagName)));
      assert.ok(p.textContent.includes('Showing the start of the file'));
      assert.ok(p.textContent.includes('may differ'));
      p.all().find(e=>e.tagName==='BUTTON'&&e.textContent==='Close').onclick();
      assert.equal(panel(root),undefined);
      C.unmount();
    ''')


def test_phone_shows_only_a_checked_png_and_clears_it_on_close_and_background():
    phone(r'''
      let answer={ok:true,kind:'image',label:'chart.png',data:PNG64,width:3,height:2,notice:'n'};
      reply=async url=>url.startsWith('/view/conversation/preview?')?{body:answer}:withFile([{label:'chart.png',kind:'image',ref:REF}]);
      let root=C.mount('Vega');body.append(root);await settle();
      await chip(root).onclick();await settle();
      let img=panel(root).all().find(e=>e.tagName==='IMG');
      assert.equal(img.src,'data:image/png;base64,'+PNG64);
      // Polls keep the open preview.
      await tick();assert.ok(panel(root));
      doc.hidden=true;docListeners.visibilitychange();doc.hidden=false;
      assert.equal(img.src,'','background drops the image data');assert.equal(root.textContent,'');
      for(const bad of [{...answer,mime:'text/html'},{...answer,data:'<svg onload=x>'},{...answer,data:'iVBO'.repeat(3)},{...answer,width:0},{...answer,kind:'html'}]){
        answer=bad;root=C.mount('Vega');body.append(root);await settle();
        await chip(root).onclick();await settle();
        assert.ok(!panel(root).all().some(e=>e.tagName==='IMG'),JSON.stringify(bad));
        assert.ok(panel(root).textContent.includes('unavailable'));
        C.unmount();
      }
    ''')


def test_phone_ignores_a_preview_that_answers_after_close():
    phone(r'''
      let release;
      reply=url=>url.startsWith('/view/conversation/preview?')?new Promise(r=>release=r):Promise.resolve(withFile([{label:'chart.png',kind:'image',ref:REF}]));
      const root=C.mount('Vega');body.append(root);await settle();
      chip(root).onclick();await settle();
      panel(root).all().find(e=>e.tagName==='BUTTON'&&e.textContent==='Close').onclick();
      release({body:{ok:true,kind:'image',label:'chart.png',data:PNG64,width:3,height:2,notice:'n'}});await settle();
      assert.equal(panel(root),undefined);assert.ok(!root.all().some(e=>e.tagName==='IMG'));
      C.unmount();
    ''')


def test_phone_explains_expired_and_refused_previews():
    phone(r'''
      let answer={status:410,body:{ok:false,error:'preview_expired'}};
      reply=async url=>url.startsWith('/view/conversation/preview?')?answer:withFile([{label:'a.md',kind:'file',ref:REF}]);
      const root=C.mount('Vega');body.append(root);await settle();
      await chip(root).onclick();await settle();
      assert.ok(panel(root).textContent.includes('expired'));
      answer={body:{ok:false,error:'preview_path_denied'}};
      await chip(root).onclick();await settle();
      assert.equal(root.all().filter(e=>e.className==='conversation-preview').length,1,'one preview at a time');
      assert.ok(panel(root).textContent.includes('outside the agent’s folders'));assert.ok(!panel(root).textContent.includes('private'));
      C.unmount();
    ''')


# --- r2: review follow-ups ------------------------------------------------------

def test_a_cockpit_record_too_large_reads_as_a_preview_limit():
    with pytest.raises(history.HistoryUnavailable) as caught:
        preview_of((400, {'ok': False, 'error': 'record_too_large'}))
    assert caught.value.code == 'preview_too_large'


def test_png_bytes_after_iend_are_refused():
    with pytest.raises(history.HistoryUnavailable) as caught:
        preview_of((200, image(png() + b'hidden')))
    assert caught.value.code == 'preview_unavailable'


def test_issuing_ids_for_a_page_does_not_walk_the_whole_table():
    from collections import OrderedDict

    class Counting(OrderedDict):
        walked = 0
        def __iter__(self):
            for key in super().__iter__():
                type(self).walked += 1
                yield key
        def items(self):
            for key, value in super().items():
                type(self).walked += 1
                yield key, value
        def values(self):
            for value in super().values():
                type(self).walked += 1
                yield value

    client = history.HistoryClient(1)
    client._refs = Counting()
    for i in range(history.MAX_REFS):
        client._issue_ref('Vega', 'R%031d' % i)
    Counting.walked = 0
    page_refs = ['R%031d' % i for i in range(history.MAX_REFS - 320, history.MAX_REFS)]
    issued = [client._issue_ref('Vega', ref) for ref in page_refs]
    # At most a peek at the oldest entry per issue, not a walk of 1024.
    assert Counting.walked <= 2 * len(page_refs), Counting.walked
    assert issued == [client._issue_ref('Vega', ref) for ref in page_refs]


@contextmanager
def raw_cockpit(preview_bytes):
    """A cockpit that answers previews with hand-written bytes, headers and all."""
    import socket
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0)); listener.listen(8)
    stop = threading.Event()
    def serve():
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                request = conn.recv(65536).decode('latin-1')
                if 'preview?' in request:
                    head, body, hold = preview_bytes()
                    conn.sendall(head + body)
                    stop.wait(hold)
                    continue
                payload = json.dumps({'ok': True, 'has_older': False, 'events': [
                    event(1, references=[{'id': COCKPIT_REF, 'label': 'chart.png', 'kind': 'image'}])]}).encode()
                conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n'
                             b'Connection: close\r\n\r\n' % len(payload) + payload)
    worker = threading.Thread(target=serve, daemon=True); worker.start()
    try:
        yield listener.getsockname()[1]
    finally:
        stop.set(); listener.close(); worker.join(3)


def headers(length):
    return b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % length


def test_a_declared_oversized_answer_is_refused_without_waiting_for_its_body():
    import time
    with raw_cockpit(lambda: (headers(history.MAX_PREVIEW_UPSTREAM + 1), b'', 4)) as port:
        client = history.HistoryClient(port)
        ref = chip_ref(client.page('Vega'))
        started = time.monotonic()
        with pytest.raises(history.HistoryUnavailable) as caught:
            client.preview('Vega', ref)
        elapsed = time.monotonic() - started
    assert caught.value.code == 'preview_too_large' and elapsed < 2, (caught.value.code, elapsed)


def test_a_body_shorter_than_its_content_length_is_refused():
    body = json.dumps(image()).encode()
    with raw_cockpit(lambda: (headers(len(body) + 100), body, 0)) as port:
        client = history.HistoryClient(port)
        ref = chip_ref(client.page('Vega'))
        with pytest.raises(history.HistoryUnavailable) as caught:
            client.preview('Vega', ref)
    assert caught.value.code == 'history_unavailable'


def test_phone_closes_an_open_preview_when_history_is_hidden_or_changed():
    phone(r'''
      for(const [status,error] of [[200,'history_stale'],[200,'history_unconfirmed'],[409,'history_changed']]){
        let hidden=false;
        reply=async url=>url.startsWith('/view/conversation/preview?')
          ?{body:{ok:true,kind:'text',label:'notes.md',text:'secret-ish text',truncated:false,notice:'n'}}
          :hidden?{status,body:{ok:false,error,reason_code:''}}:withFile([{label:'notes.md',kind:'file',ref:REF}]);
        const root=C.mount('Vega');body.append(root);await settle();
        await chip(root).onclick();await settle();assert.ok(panel(root));
        hidden=true;await tick();
        assert.equal(panel(root),undefined,error);assert.ok(!root.textContent.includes('secret-ish text'),error);
        C.unmount();
      }
    ''')


def test_phone_builds_nothing_from_a_preview_that_answers_after_close():
    phone(r'''
      const made=[];const create=doc.createElement;doc.createElement=t=>{const e=create(t);made.push(e);return e;};
      let release;
      reply=url=>url.startsWith('/view/conversation/preview?')?new Promise(r=>release=r):Promise.resolve(withFile([{label:'chart.png',kind:'image',ref:REF}]));
      const root=C.mount('Vega');body.append(root);await settle();
      chip(root).onclick();await settle();
      panel(root).all().find(e=>e.tagName==='BUTTON'&&e.textContent==='Close').onclick();
      const before=made.length;
      release({body:{ok:true,kind:'image',label:'chart.png',data:PNG64,width:3,height:2,notice:'n'}});await settle();
      assert.equal(made.length,before,'no element is built for a closed preview');
      assert.ok(!made.some(e=>e.tagName==='IMG'));
      C.unmount();
    ''')


def test_a_slow_poll_leaves_an_open_preview_and_a_slow_preview_says_tap_again():
    phone(r'''
      let slowPoll=false,slowPreview=false;
      reply=async url=>url.startsWith('/view/conversation/preview?')
        ?(slowPreview?{status:503,body:{ok:false,error:'history_slow'}}:{body:{ok:true,kind:'text',label:'notes.md',text:'kept text',truncated:false,notice:'n'}})
        :slowPoll?{status:503,body:{ok:false,error:'history_slow',reason_code:''}}:withFile([{label:'notes.md',kind:'file',ref:REF}]);
      const root=C.mount('Vega');body.append(root);await settle();
      await chip(root).onclick();await settle();
      slowPoll=true;await tick();
      assert.ok(panel(root)&&panel(root).textContent.includes('kept text'),'a slow poll is only a delay');
      slowPreview=true;await chip(root).onclick();await settle();
      assert.ok(panel(root).textContent.includes('Tap the file again'),panel(root).textContent);
      C.unmount();
    ''')


# --- Save ------------------------------------------------------------------------

def save_phone(body):
    phone(r'''
      const made=[],revoked=[],clicks=[];
      context.Blob=Blob;context.atob=atob;
      context.URL={createObjectURL(b){made.push(b);return 'blob:saved-'+made.length;},revokeObjectURL(u){revoked.push(u);}};
      El.prototype.click=function(){clicks.push({download:this.download,href:this.href,attached:this.isConnected,tag:this.tagName});};
      const save=root=>panel(root)?.all().find(e=>e.tagName==='BUTTON'&&e.textContent==='Save');
      const close=root=>panel(root).all().find(e=>e.tagName==='BUTTON'&&e.textContent==='Close');
      ''' + body)


def test_save_writes_the_previewed_text_and_makes_no_request():
    save_phone(r'''
      const shown='line 1\n<script>alert(1)</script>\n';
      reply=async url=>url.startsWith('/view/conversation/preview?')
        ?{body:{ok:true,kind:'text',label:'notes.md',text:shown,truncated:false,notice:'n'}}
        :withFile([{label:'notes.md',kind:'file',ref:REF}]);
      const root=C.mount('Vega');body.append(root);await settle();
      await chip(root).onclick();await settle();
      const before=fetches.length;
      save(root).onclick();
      assert.equal(fetches.length,before,'Save sends nothing');
      assert.equal(clicks.length,1);assert.equal(clicks[0].tag,'A');assert.equal(clicks[0].download,'notes.md');
      assert.equal(clicks[0].href,'blob:saved-1');assert.ok(clicks[0].attached);
      assert.equal(made[0].type,'text/plain;charset=utf-8');assert.equal(await made[0].text(),shown);
      assert.ok(!panel(root).all().some(e=>e.tagName==='A'),'the anchor does not stay');
      // A second Save replaces the first URL; Close revokes the last one.
      save(root).onclick();assert.deepEqual(revoked,['blob:saved-1']);
      close(root).onclick();assert.deepEqual(revoked,['blob:saved-1','blob:saved-2']);
      C.unmount();
    ''')


def test_saved_text_that_a_viewer_could_render_gets_a_txt_name():
    save_phone(r'''
      let label;
      reply=async url=>url.startsWith('/view/conversation/preview?')
        ?{body:{ok:true,kind:'text',label,text:'<svg onload=x>',truncated:false,notice:'n'}}
        :withFile([{label,kind:'file',ref:REF}]);
      for(const [given,want] of [['page.html','page.html.txt'],['x.SVG','x.SVG.txt'],['a.xhtml','a.xhtml.txt'],['feed.xml','feed.xml.txt'],
                                 ['notes.md','notes.md'],['report\u202etxt.html','reporttxt.html.txt']]){
        label=given;clicks.length=0;const root=C.mount('Vega');body.append(root);await settle();
        await chip(root).onclick();await settle();save(root).onclick();
        assert.equal(clicks[0].download,want,given);C.unmount();
      }
    ''')


def test_save_writes_the_checked_png_bytes_with_a_png_name():
    save_phone(r'''
      let label='shots/photo.jpg';
      reply=async url=>url.startsWith('/view/conversation/preview?')
        ?{body:{ok:true,kind:'image',label,data:PNG64,width:3,height:2,notice:'n'}}
        :withFile([{label,kind:'image',ref:REF}]);
      let root=C.mount('Vega');body.append(root);await settle();
      await chip(root).onclick();await settle();
      save(root).onclick();
      assert.equal(clicks[0].download,'photo.jpg.png');
      assert.equal(made[0].type,'image/png');
      assert.deepEqual(Buffer.from(await made[0].arrayBuffer()),Buffer.from(PNG64,'base64'));
      // Background clears the preview and revokes the URL.
      doc.hidden=true;docListeners.visibilitychange();doc.hidden=false;
      assert.deepEqual(revoked,['blob:saved-1']);
      for(const [given,want] of [['..','image.png'],['a\\b\u0007c.PNG','bc.PNG'],['x'.repeat(300),'x'.repeat(120)+'.png'],
                                 ['shot\u202egnp.html','shotgnp.html.png'],['a\u0085\u200e\u2066b.png','ab.png']]){
        label=given;clicks.length=0;root=C.mount('Vega');body.append(root);await settle();
        await chip(root).onclick();await settle();save(root).onclick();
        assert.equal(clicks[0].download,want,given);C.unmount();
      }
    ''')


def test_no_save_without_a_shown_preview():
    save_phone(r'''
      let answer={ok:false,error:'preview_type_denied'};
      reply=async url=>url.startsWith('/view/conversation/preview?')?{body:answer}:withFile([{label:'chart.png',kind:'image',ref:REF}]);
      for(const bad of [answer,{ok:true,kind:'image',label:'chart.png',data:'<svg onload=x>',width:3,height:2},{ok:true,kind:'html',label:'a.html',text:'<b>'}]){
        answer=bad;const root=C.mount('Vega');body.append(root);await settle();
        await chip(root).onclick();await settle();
        assert.equal(save(root),undefined,JSON.stringify(bad));C.unmount();
      }
      assert.equal(made.length,0);
    ''')


def test_phone_shows_chips_on_a_tool_row_and_opens_the_preview():
    # The cockpit attaches references to file tool calls, not to text bubbles.
    phone(r'''
      reply=async url=>url.startsWith('/view/conversation/preview?')
        ?{body:{ok:true,kind:'text',label:'notes.md',text:'# Notes',truncated:false,notice:'n'}}
        :{body:{ok:true,agent:'Vega',older:null,limited:false,events:[
           {id:'1',kind:'tool',role:'assistant',text:'Write',ts:'',truncated:false,files:[{label:'notes.md',kind:'file',ref:REF}]}]}};
      const root=C.mount('Vega');body.append(root);await settle();
      const row=root.all().find(e=>e.className==='conversation-tool');
      const c=row.all().find(e=>e.className.startsWith('conversation-chip'));
      assert.ok(c,'the chip is inside the tool row');assert.equal(c.tagName,'BUTTON');
      await c.onclick();await settle();
      assert.equal(fetches.at(-1).url,'/view/conversation/preview?name=Vega&ref='+REF);
      assert.ok(panel(root));
      C.unmount();
    ''')


def test_a_recorded_claude_write_call_reaches_the_phone_as_a_previewable_chip():
    # The shape the cockpit returns for a Claude record (measured with the real
    # backend over a Claude transcript): the reference rides on the tool event.
    cockpit_page = {'ok': True, 'agent': 'Vega', 'provider': 'claude', 'mode': 'cursor', 'events': [
        {'kind': 'text', 'role': 'user', 'text': 'Write the notes and show them.', 'ts': '2026-11-06T03:20:00Z',
         'id': '0:0:0', 'cursor_before': 'e3a9b3c20f3ba97fb963d683:0:0:0', 'cursor_after': 'e3a9b3c20f3ba97fb963d683:394:0:0'},
        {'kind': 'tool', 'role': 'assistant', 'text': 'Write', 'ts': '2026-11-06T03:21:00Z',
         'id': '394:0:1', 'cursor_before': 'e3a9b3c20f3ba97fb963d683:394:0:1', 'cursor_after': 'e3a9b3c20f3ba97fb963d683:890:0:0',
         'references': [{'id': COCKPIT_REF, 'label': 'notes.md', 'kind': 'file'}]},
        {'kind': 'tool', 'role': 'user', 'text': 'Tool result', 'ts': '2026-11-06T03:21:01Z', 'id': '890:0:0',
         'cursor_before': 'e3a9b3c20f3ba97fb963d683:890:0:0', 'cursor_after': 'e3a9b3c20f3ba97fb963d683:1294:0:0'},
    ], 'before': 'e3a9b3c20f3ba97fb963d683:0:0:0', 'after': 'e3a9b3c20f3ba97fb963d683:1294:0:0', 'has_older': False}
    events = history.project_page(cockpit_page, lambda ref: REF)[0]
    tool = next(e for e in events if e['kind'] == 'tool' and e['text'] == 'Write')
    assert tool['files'] == [{'label': 'notes.md', 'kind': 'file', 'ref': REF}]
    phone(r'''
      const events=%s;
      reply=async url=>url.startsWith('/view/conversation/preview?')
        ?{body:{ok:true,kind:'text',label:'notes.md',text:'# Notes',truncated:false,notice:'n'}}
        :{body:{ok:true,agent:'Vega',older:null,limited:false,events}};
      const root=C.mount('Vega');body.append(root);await settle();
      const c=chip(root);assert.ok(c);assert.equal(c.tagName,'BUTTON');
      assert.ok(c.textContent.includes('notes.md'));
      await c.onclick();await settle();assert.ok(panel(root));
      C.unmount();
    ''' % json.dumps(events))
