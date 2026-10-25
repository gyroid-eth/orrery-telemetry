"""Real HTTP on ephemeral loopback fixtures; no Tailscale or production service."""
from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading

import pytest

from dashboard import mobile_relay as relay


@contextmanager
def serving(server):
    worker=threading.Thread(target=server.serve_forever,daemon=True)
    worker.start()
    try:yield server.server_address[1]
    finally:
        server.shutdown();server.server_close();worker.join(3)
        assert not worker.is_alive()


class Store:
    def __init__(self):self.calls=[]
    def snapshot(self,window):
        self.calls.append(window)
        return {'schema':1,'sources':{'agents':{'status':'available'}},'agents':[]}


@pytest.fixture
def reader():
    config=relay.Config('owner@example.test','https://viewer.example.ts.net:9443')
    store=Store();server=relay.make_server(config,store=store,test_port=0)
    with serving(server) as port:yield port,store


def request(port,path='/view/snapshot',method='GET',headers=None):
    con=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
    try:
        con.request(method,path,headers=headers if headers is not None else {
            'Host':'viewer.example.ts.net:9443','Tailscale-User-Login':'owner@example.test'})
        response=con.getresponse();return response.status,dict(response.getheaders()),response.read()
    finally:con.close()


def test_owner_and_outer_authority_accept_originless_get_and_assets(reader):
    port,store=reader
    status,headers,_=request(port)
    assert status==200 and store.calls==['day']
    assert headers['Cache-Control']=='no-store'
    assert "connect-src 'self'" in headers['Content-Security-Policy']
    status,_,body=request(port,'/network_node.js')
    assert status==200 and b'OrreryNetworkNode' in body
    status,_,css=request(port,'/network.css')
    assert status==200 and b'--serif:' in css and b'--ln-local:' in css
    for font in ('fraunces-normal','fraunces-italic','ibm-plex-mono-400','ibm-plex-mono-500','ibm-plex-mono-600'):
        status,headers,body=request(port,'/fonts/'+font+'.woff2')
        assert status==200 and headers['Content-Type']=='font/woff2' and body[:4]==b'wOF2'
        assert request(port,'/fonts/'+font+'.woff2',headers={'Host':'viewer.example.ts.net:9443'})[0]==403
    assert request(port,'/network_node.js',method='HEAD')[0]==200


def test_mobile_default_is_one_day_and_fine_window_is_allowlisted(reader):
    port,store=reader
    assert request(port)[0]==200 and store.calls==['day']
    assert request(port,'/view/snapshot?window=m120')[0]==200
    assert store.calls[-1]=='m120'
    for window in ('m31','m00015','m43215'):
        assert request(port,'/view/snapshot?window='+window)[0]==400


def test_every_fixed_logo_is_authenticated_svg_and_matches_canonical_bytes(reader):
    from pathlib import Path
    port,store=reader;root=Path(__file__).resolve().parents[1]
    for logo in ('orbit','anthropic','openai','google','deepseek','qwen'):
        path='/assets/'+logo+'.svg'
        source=(root/'integrations/dsh/assets/deepseek.svg' if logo=='deepseek'
                else root/'dashboard/assets'/(logo+'.svg'))
        status,headers,body=request(port,path)
        assert status==200,logo
        assert headers['Content-Type']=='image/svg+xml' and body and body==source.read_bytes(),logo
        assert request(port,path,headers={'Host':'viewer.example.ts.net:9443'})[0]==403
    assert store.calls==[]


@pytest.mark.parametrize('headers',[
    {'Host':'viewer.example.ts.net:9443'},
    {'Host':'viewer.example.ts.net:9443','Tailscale-User-Login':'other@example.test'},
    {'Host':'127.0.0.1:8781','Tailscale-User-Login':'owner@example.test'},
    {'Host':'attacker.test','Tailscale-User-Login':'owner@example.test'},
    {'Host':'viewer.example.ts.net','Tailscale-User-Login':'owner@example.test'},
    {'Host':'viewer.example.ts.net:9443','Tailscale-User-Login':'owner@example.test','Origin':'https://other.example.ts.net:9443'},
    {'Host':'viewer.example.ts.net:9443','Tailscale-User-Login':'owner@example.test','Origin':'null'},
    {'Host':'viewer.example.ts.net:9443','Tailscale-User-Login':'owner@example.test','Sec-Fetch-Site':'cross-site'},
    {'Host':'viewer.example.ts.net:9443','Tailscale-User-Login':'owner@example.test','X-HTTP-Method-Override':'POST'},
    {'Host':'viewer.example.ts.net:9443','Tailscale-User-Login':'owner@example.test','Upgrade':'websocket'},
])
def test_auth_and_cross_origin_refusals_do_not_observe_upstream(reader,headers):
    port,store=reader
    assert request(port,headers=headers)[0]==403
    assert store.calls==[]


@pytest.mark.parametrize('field',['Host','Origin','Tailscale-User-Login'])
def test_duplicate_headers_fail_closed(reader,field):
    port,store=reader
    lines=['GET /view/snapshot HTTP/1.1','Host: viewer.example.ts.net:9443',
           'Tailscale-User-Login: owner@example.test','Origin: https://viewer.example.ts.net:9443',
           field+': '+{'Host':'viewer.example.ts.net:9443','Origin':'https://viewer.example.ts.net:9443','Tailscale-User-Login':'owner@example.test'}[field],
           'Connection: close','','']
    with socket.create_connection(('127.0.0.1',port),timeout=3) as sock:
        sock.sendall('\r\n'.join(lines).encode());answer=sock.recv(1024)
    assert b' 403 ' in answer and store.calls==[]


@pytest.mark.parametrize('path,status',[
    ('/api/agents',404),('/api/quotas',404),('/api/ptty?session=Any',404),
    ('/view/snapshot?window=unknown',400),('/view/snapshot?window=all&window=day',400),
    ('/view/snapshot?url=http://elsewhere/',400),('/../index.html',404),
    ('/fonts/../index.html',404),('/%2e%2e/index.html',403),
    ('http://elsewhere.test/view/snapshot',403),('//elsewhere.test/',403),
    ('/network_node.js?any=value',404),
    # API30 admits the fixed detail route, but never an unissued event.
    ('/view/message?event='+'a'*32,410),('/view/mail',400),('/api/mobile-cache-page',404),
])
def test_fixed_routes_and_query(reader,path,status):
    port,store=reader
    assert request(port,path)[0]==status and store.calls==[]


@pytest.mark.parametrize('method',['POST','PUT','PATCH','DELETE','OPTIONS','TRACE','CONNECT'])
def test_no_operation_method(reader,method):
    port,store=reader
    assert request(port,method=method)[0]==405 and store.calls==[]


def test_default_off_and_missing_enabled_config_never_listen(monkeypatch):
    monkeypatch.setattr(relay,'make_server',lambda *a,**k:pytest.fail('must not listen'))
    assert relay.main([])==0
    with pytest.raises(SystemExit):relay.main(['--enable'])
    assert relay.main(['--check','--owner-login','owner@example.test','--public-origin','https://viewer.example.ts.net:9443'])==0
    with pytest.raises(ValueError):relay.Config('owner@example.test','http://viewer.example.ts.net')
    with pytest.raises(ValueError):relay.Config('owner@example.test','https://untrusted.test')


def test_single_message_and_page_dispatch_only_after_owner_admission():
    calls=[]
    class Content:
        def message(self,window,event):
            calls.append((window,event))
            return {'status':'available','subject':'SYNTHETIC-SUBJECT','body':'<img src="https://example.invalid/no">'}
        def page(self,window,cursor):
            calls.append(('page',window,cursor));return {'status':'available','events':[],'next_cursor':None}
        def close(self):pass
    server=relay.make_server(relay.Config('owner@example.test','https://viewer.example.ts.net:9443'),store=Store(),test_port=0)
    server.mobile_mail=Content()
    with serving(server) as port:
        target='/view/message?window=day&event='+'a'*32
        assert request(port,target,headers={'Host':'viewer.example.ts.net:9443'})[0]==403
        assert calls==[]
        status,headers,body=request(port,target)
        assert status==200 and json.loads(body)['subject']=='SYNTHETIC-SUBJECT'
        assert headers['Cache-Control']=='no-store' and calls==[('day','a'*32)]
        assert request(port,target,method='HEAD')[0]==200 and len(calls)==1
        assert request(port,target+'&url=elsewhere')[0]==400 and len(calls)==1
        assert request(port,'/view/mail?window=day&cursor='+'b'*32)[0]==200
        assert request(port,'/api/mobile-cache-page')[0]==404


def test_content_worker_cannot_start_before_valid_owner_snapshot():
    class Content:
        def __init__(self):self.calls=[]
        def observe(self,p):self.calls.append('observe')
        def start_warm(self):self.calls.append('warm')
        def progress(self):return {'status':'idle','messages':0}
        def close(self):self.calls.append('close')
        def page(self,*args,**kwargs):pytest.fail('no page before admission')
        def message(self,*args):pytest.fail('no detail before admission')
    class Snapshot(Store):
        def snapshot(self,window):return {'sources':{'mail':{'status':'available'}},'timeline':{}}
    content=Content();server=relay.make_server(relay.Config('owner@example.test','https://viewer.example.ts.net:9443'),store=Snapshot(),mail_store=content,test_port=0)
    with serving(server) as port:
        for path,method,headers in [('/view/snapshot','GET',{'Host':'viewer.example.ts.net:9443'}),
                                    ('/view/snapshot','HEAD',None),('/mobile.js','GET',None),
                                    ('/view/snapshot?bad=1','GET',None),('/api/mobile-cache-page','GET',None)]:
            request(port,path,method,headers);assert content.calls==[]
        assert request(port)[0]==200 and content.calls==['observe','warm']
    assert content.calls[-1]=='close'


def test_pair_name_direction_query_validation_precedes_metadata_reader():
    class Known(Store):
        def known_agent(self,window,n):return n in {'Alpha','Beta'}
    class Content:
        def __init__(self):self.calls=[]
        def page(self,window,cursor,**kwargs):
            self.calls.append((window,cursor,kwargs));return {'events':[]}
        def close(self):pass
    content=Content();server=relay.make_server(relay.Config('owner@example.test','https://viewer.example.ts.net:9443'),store=Known(),mail_store=content,test_port=0)
    with serving(server) as port:
        for query,status in [('a=Alpha&b=Beta&direction=both',200),
                             ('a=Alpha&b=Other&direction=both',404),
                             ('a=Alpha&b=Beta',400),('a=Alpha&b=Beta&direction=bcc',400),
                             ('a=Alpha&b=Beta&direction=both&cursor=bad',400),
                             ('a=Alpha&b=Beta&direction=both&a=Beta',400)]:
            assert request(port,'/view/mail?window=day&'+query)[0]==status
        assert content.calls==[('day',None,{'pair':('Alpha','Beta'),'direction':'both'})]


def test_upstream_expiry_remains_explicit_without_reading_error_body():
    class Expired(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            self.send_response(410);self.send_header('Content-Length','0');self.end_headers()
    backend=ThreadingHTTPServer(('127.0.0.1',0),Expired)
    with serving(backend) as port:
        source=relay.Upstream(port)
        with pytest.raises(LookupError):source.mail_page('day','a'*32)
        with pytest.raises(LookupError):source.message('day','a'*32,'content')
        # Portrait failures keep their existing unavailable-image behavior.
        with pytest.raises(ValueError):source.portrait('Alpha')


def test_upstream_redirect_wrong_type_size_and_headers(monkeypatch):
    seen=[];mode=['ok']
    class Fixture(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path,dict(self.headers)))
            if mode[0]=='redirect':
                self.send_response(302);self.send_header('Location','http://127.0.0.1:1/private');self.end_headers();return
            self.send_response(200)
            self.send_header('Content-Type','text/html' if mode[0]=='mime' else 'application/json')
            if mode[0]=='large':self.send_header('Content-Length',str(relay.MAX_UPSTREAM_BYTES+1))
            self.end_headers()
            if mode[0]!='large':self.wfile.write(b'{"agents":[]}')
        def log_message(self,*a):pass
    with serving(ThreadingHTTPServer(('127.0.0.1',0),Fixture)) as port:
        upstream=relay.Upstream(port)
        assert upstream('agents')=={'agents':[]}
        assert seen[0][0]=='/api/agents'
        assert seen[0][1]['Host']==f'127.0.0.1:{port}'
        assert 'Origin' not in seen[0][1] and 'Tailscale-User-Login' not in seen[0][1]
        for case in ['redirect','mime','large']:
            mode[0]=case
            with pytest.raises(ValueError):upstream('agents')
        assert len(seen)==4
        with pytest.raises(ValueError):upstream('http://untrusted.test/')


def test_trickling_upstream_cannot_extend_total_deadline(monkeypatch):
    import time
    stop=threading.Event()
    class Trickle(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length','1000');self.end_headers()
            try:
                while not stop.wait(.025):self.wfile.write(b' ');self.wfile.flush()
            except (OSError,ValueError):pass
        def log_message(self,*args):pass
    monkeypatch.setattr(relay,'UPSTREAM_TIMEOUT',.2)
    server=ThreadingHTTPServer(('127.0.0.1',0),Trickle);server.daemon_threads=True
    try:
        with serving(server) as port:
            start=time.monotonic()
            with pytest.raises((OSError,ValueError)):
                relay.Upstream(port)('agents')
            assert time.monotonic()-start<1
    finally:stop.set()


def test_named_portrait_requires_owner_and_published_period_roster(reader,monkeypatch):
    from test_portraits import _fixture_png
    port,store=reader;calls=[];png=_fixture_png()
    store.known_agent=lambda window,name:window=='day' and name=='FixtureBot'
    def portrait(self,name):calls.append(name);return png
    monkeypatch.setattr(relay.Upstream,'portrait',portrait,raising=False)
    status,headers,body=request(port,'/view/portrait?name=FixtureBot')
    assert status==200 and body==png and headers['Content-Type']=='image/png'
    assert headers['Cache-Control']=='no-store' and calls==['FixtureBot']
    for path,status in [('/view/portrait?name=UnknownFixture',404),('/view/portrait?name=../bad',400),
                        ('/view/portrait?name=FixtureBot&window=m31',400),
                        ('/view/portrait?name=FixtureBot&name=Other',400)]:
        assert request(port,path)[0]==status
    assert request(port,'/view/portrait?name=FixtureBot',headers={'Host':'viewer.example.ts.net:9443'})[0]==403
    assert calls==['FixtureBot']


def test_portrait_upstream_is_fixed_png_bounded_without_redirect_or_identity_headers():
    from test_portraits import _fixture_png
    png=_fixture_png();seen=[];mode=['png']
    class Backend(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            seen.append((self.path,dict(self.headers)))
            self.send_response(302 if mode[0]=='redirect' else 200)
            self.send_header('Content-Type','image/svg+xml' if mode[0]=='svg' else 'image/png')
            if mode[0]=='redirect':self.send_header('Location','http://elsewhere.test/')
            if mode[0]=='huge':self.send_header('Content-Length','524289')
            self.end_headers()
            if mode[0]!='huge':self.wfile.write(png if mode[0]!='invalid' else b'not a PNG')
    with serving(ThreadingHTTPServer(('127.0.0.1',0),Backend)) as port:
        upstream=relay.Upstream(port)
        assert upstream.portrait('FixtureBot')==png
        assert seen[0][0]=='/api/mobile-portrait?name=FixtureBot'
        assert seen[0][1]['Host']==f'127.0.0.1:{port}'
        assert 'Tailscale-User-Login' not in seen[0][1] and 'Origin' not in seen[0][1]
        for case in ('redirect','svg','huge','invalid'):
            mode[0]=case
            with pytest.raises(ValueError):upstream.portrait('FixtureBot')
        for name in ('../bad','http://elsewhere/','a%2fb'):
            with pytest.raises(ValueError):upstream.portrait(name)
        assert len(seen)==5
