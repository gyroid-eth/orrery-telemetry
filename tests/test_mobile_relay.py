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


@pytest.mark.parametrize('case',['valid','origin_missing','origin_other','owner_other','host_other',
    'duplicate_json','owner_payload','method_override','content_encoding','length_non_ascii','oversize','unknown_route'])
def test_input_post_requires_owner_fixed_origin_policy_and_finite_json(case):
    calls=[]
    class Input:
        def call(self,operation,payload=None):
            calls.append((operation,payload));return {'state':'available'}
    config=relay.Config('owner@example.test','https://viewer.example.ts.net:9443',
                        input_enabled=True,phone_policy_reviewed=True)
    server=relay.make_server(config,store=Store(),input_client=Input(),test_port=0)
    with serving(server) as port:
        headers={'Host':'viewer.example.ts.net:9443','Tailscale-User-Login':'owner@example.test',
                 'Origin':'https://viewer.example.ts.net:9443','Content-Type':'application/json'}
        value={'name':'FixtureCurie','epoch':'a'*32}
        if case=='origin_missing': del headers['Origin']
        elif case=='origin_other': headers['Origin']='https://other.example.ts.net:9443'
        elif case=='owner_other': headers['Tailscale-User-Login']='other@example.test'
        elif case=='host_other': headers['Host']='other.example.ts.net:9443'
        elif case=='owner_payload': value['owner']='other@example.test'
        elif case=='method_override': headers['X-HTTP-Method-Override']='GET'
        elif case=='content_encoding': headers['Content-Encoding']='gzip'
        elif case=='length_non_ascii': headers['Content-Length']='²'
        body=json.dumps(value).encode()
        if case=='duplicate_json': body=b'{"name":"a","name":"b","epoch":"' + b'a'*32+b'"}'
        elif case=='oversize': body=b'x'*16385
        path='/view/terminal/targets' if case!='unknown_route' else '/view/terminal/proxy'
        con=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
        try:
            con.request('POST',path,body=body,headers=headers)
            response=con.getresponse(); response.read()
            assert response.status==(200 if case=='valid' else 405 if case=='unknown_route' else 400 if case in {'duplicate_json','owner_payload'} else 403)
            assert response.getheader('Cache-Control')=='no-store'
        finally: con.close()
        assert len(calls)==(1 if case=='valid' else 0)


def test_input_policy_is_an_explicit_gate_and_normal_reader_stays_read_only():
    with pytest.raises(ValueError):
        relay.Config('owner@example.test','https://viewer.example.ts.net',input_enabled=True)
    assert relay.Config('owner@example.test','https://viewer.example.ts.net').input_enabled is False


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


def shortcut_request(port,path='/',method='GET',*,replace=None,omit=(),extra=()):
    """Send the exact request-target and duplicate headers without normalization."""
    fields=[('Host','viewer.example.ts.net:9443'),('Tailscale-User-Login','owner@example.test'),
            ('Sec-Fetch-Site','cross-site'),('Sec-Fetch-Mode','navigate'),('Sec-Fetch-Dest','document')]
    fields=[(key,(replace or {}).get(key,value)) for key,value in fields if key not in omit]
    fields.extend(extra)
    wire='\r\n'.join([f'{method} {path} HTTP/1.1',*[f'{key}: {value}' for key,value in fields],
                       'Connection: close','','']).encode('ascii')
    with socket.create_connection(('127.0.0.1',port),timeout=3) as sock:
        sock.sendall(wire);chunks=[]
        while data:=sock.recv(65536):chunks.append(data)
    headers,body=b''.join(chunks).split(b'\r\n\r\n',1)
    lines=headers.decode('ascii').split('\r\n')
    return int(lines[0].split()[1]),dict(line.split(': ',1) for line in lines[1:]),body


@pytest.fixture
def shortcut_reader(monkeypatch):
    calls=[];store=Store()
    def upstream(*args,**kwargs):
        calls.append('upstream');raise AssertionError('root navigation must not read upstream')
    monkeypatch.setattr(relay.Upstream,'_read',upstream)
    server=relay.make_server(relay.Config('owner@example.test','https://viewer.example.ts.net:9443'),
                             store=store,test_port=0)
    server.mobile_mail.observe=lambda *args:calls.append('observe')
    server.mobile_mail.start_warm=lambda:calls.append('warm')
    with serving(server) as port:yield port,store,calls
    assert store.calls==calls==[]


@pytest.mark.parametrize('method',['GET','HEAD'])
@pytest.mark.parametrize('origin',[None,'https://viewer.example.ts.net:9443'])
def test_shortcut_root_document_navigation_accepts_only_the_authenticated_shell(shortcut_reader,method,origin):
    from pathlib import Path
    port,store,calls=shortcut_reader
    extra=[] if origin is None else [('Origin',origin)]
    status,headers,body=shortcut_request(port,method=method,extra=extra)
    assert status==200
    expected=(Path(relay.__file__).parent/'mobile/index.html').read_bytes()
    assert headers['Content-Type']=='text/html; charset=utf-8'
    assert int(headers['Content-Length'])==len(expected)
    assert body==(expected if method=='GET' else b'')
    assert headers['Cache-Control']=='no-store' and headers['Content-Security-Policy']==relay.CSP
    assert headers['X-Frame-Options']=='DENY' and store.calls==calls==[]


@pytest.mark.parametrize('path',[
    '/?','/?window=day','/index.html','/./','//','//viewer.example.ts.net/',
    'http://viewer.example.ts.net:9443/','/%2f','/\\','/#fragment',
    '/view/snapshot','/view/message?window=day&event='+'a'*32,
    '/view/mail?window=day','/view/portrait?name=FixtureCurie',
    '/assets/orbit.svg','/network_node.js','/network.css','/mobile.js','/fonts/fraunces-normal.woff2'])
def test_shortcut_navigation_cannot_open_data_assets_or_ambiguous_root(shortcut_reader,path):
    port,store,calls=shortcut_reader
    assert shortcut_request(port,path)[0]==403,path
    assert store.calls==calls==[]


@pytest.mark.parametrize('case',[
    'missing_mode','missing_dest','cors','no_cors','iframe','embed','mode_case','dest_case',
    'unknown_site','site_list','duplicate_site','duplicate_mode','duplicate_dest',
    'missing_owner','owner_other','missing_host','host_other','origin_other','origin_null',
    'duplicate_owner','duplicate_host','duplicate_origin','authority','override','upgrade',
    'body_length','duplicate_length','transfer_encoding'])
def test_shortcut_root_keeps_fetch_metadata_auth_origin_and_framing_guards(shortcut_reader,case):
    port,store,calls=shortcut_reader;replace={};omit=[];extra=[]
    changes={
        'missing_mode':('omit','Sec-Fetch-Mode',None),'missing_dest':('omit','Sec-Fetch-Dest',None),
        'cors':('replace','Sec-Fetch-Mode','cors'),'no_cors':('replace','Sec-Fetch-Mode','no-cors'),
        'iframe':('replace','Sec-Fetch-Dest','iframe'),'embed':('replace','Sec-Fetch-Dest','embed'),
        'mode_case':('replace','Sec-Fetch-Mode','NAVIGATE'),'dest_case':('replace','Sec-Fetch-Dest','DOCUMENT'),
        'unknown_site':('replace','Sec-Fetch-Site','unknown'),'site_list':('replace','Sec-Fetch-Site','cross-site, none'),
        'duplicate_site':('extra','Sec-Fetch-Site','none'),'duplicate_mode':('extra','Sec-Fetch-Mode','navigate'),
        'duplicate_dest':('extra','Sec-Fetch-Dest','document'),'missing_owner':('omit','Tailscale-User-Login',None),
        'owner_other':('replace','Tailscale-User-Login','other@example.test'),'missing_host':('omit','Host',None),
        'host_other':('replace','Host','other.example.ts.net:9443'),
        'origin_other':('extra','Origin','https://other.example.ts.net:9443'),'origin_null':('extra','Origin','null'),
        'duplicate_owner':('extra','Tailscale-User-Login','owner@example.test'),
        'duplicate_host':('extra','Host','viewer.example.ts.net:9443'),
        'duplicate_origin':('extra','Origin','https://viewer.example.ts.net:9443'),
        'authority':('extra','Authority','viewer.example.ts.net:9443'),
        'override':('extra','X-HTTP-Method-Override','POST'),'upgrade':('extra','Upgrade','websocket'),
        'body_length':('extra','Content-Length','1'),'duplicate_length':('extra','Content-Length','0'),
        'transfer_encoding':('extra','Transfer-Encoding','chunked')}
    kind,key,value=changes[case]
    if kind=='omit':omit.append(key)
    elif kind=='replace':replace[key]=value
    else:extra.append((key,value))
    if case in {'duplicate_origin','duplicate_length'}:extra.append((key,value))
    assert shortcut_request(port,replace=replace,omit=omit,extra=extra)[0]==403,case
    assert store.calls==calls==[]


@pytest.mark.parametrize('method',['POST','PUT','PATCH','DELETE','OPTIONS','TRACE','CONNECT'])
def test_shortcut_document_headers_never_enable_an_operation(shortcut_reader,method):
    assert shortcut_request(shortcut_reader[0],method=method)[0]==405


@pytest.mark.parametrize('site',[None,'none','same-origin'])
def test_existing_root_navigation_without_the_shortcut_exception_stays_available(shortcut_reader,site):
    omit=['Sec-Fetch-Mode','Sec-Fetch-Dest'];replace={}
    if site is None:omit.append('Sec-Fetch-Site')
    else:replace['Sec-Fetch-Site']=site
    assert shortcut_request(shortcut_reader[0],omit=omit,replace=replace)[0]==200


@pytest.mark.parametrize('case',['valid','old_api','wrong_contract','duplicate_json','redirect',
                                 'wrong_mime','encoded','duplicate_length','oversize','lost_commit'])
def test_input_client_checks_adapter_contract_and_never_retries_lost_commit(case):
    from dashboard.mobile_input import CONTRACT,InputClient,InputUnavailable
    calls=[]
    class Adapter(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path,dict(self.headers),payload))
            value=dict(contract='other' if case=='wrong_contract' else CONTRACT,
                       api=30 if case=='old_api' else 31,epoch='a'*32,enabled=True,
                       supported='legacy-managed-pane',ack='tmux-command-accepted')
            data=json.dumps(value).encode()
            if case=='duplicate_json':data=data[:-1]+b',"api":31}'
            if case=='oversize':data=b'x'*16385
            self.send_response(302 if case=='redirect' else 200)
            self.send_header('Content-Type','text/html' if case=='wrong_mime' else 'application/json')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(data)))
            if case=='duplicate_length':self.send_header('Content-Length',str(len(data)))
            if case=='encoded':self.send_header('Content-Encoding','gzip')
            if case=='redirect':self.send_header('Location','http://elsewhere.invalid/input')
            self.end_headers()
            if case=='lost_commit':self.close_connection=True;return
            self.wfile.write(data)
    with serving(ThreadingHTTPServer(('127.0.0.1',0),Adapter)) as port:
        client=InputClient(port,'owner@example.test')
        operation='commit' if case=='lost_commit' else 'capability'
        payload=dict(epoch='a'*32,intent='b'*64,confirmation='c'*64) if operation=='commit' else {}
        if case=='valid':assert client.call(operation,payload)['enabled'] is True
        else:
            with pytest.raises(InputUnavailable):client.call(operation,payload)
        assert len(calls)==1 and calls[0][0]=='/_mobile/terminal/'+operation
        assert calls[0][1]['Host']==f'127.0.0.1:{port}'
        assert calls[0][1]['Origin']==f'http://127.0.0.1:{port}'
        assert 'Tailscale-User-Login' not in calls[0][1]
        assert calls[0][2]['owner']=='owner@example.test' and calls[0][2]['reader_epoch']==client.reader_epoch


def test_disabled_terminal_capability_does_not_contact_cockpit():
    class NoInput:
        def call(self,*a,**kw):raise AssertionError('disabled terminal must not contact cockpit')
    server=relay.make_server(relay.Config('owner@example.test','https://viewer.example.ts.net:9443'),store=Store(),input_client=NoInput(),test_port=0)
    with serving(server) as port:
        status,headers,body=request(port,'/view/terminal')
        assert status==200 and json.loads(body)['enabled'] is False
        assert request(port,'/view/terminal',headers={'Host':'viewer.example.ts.net:9443'})[0]==403
        assert request(port,'/view/terminal/preview',method='POST')[0]==405


def test_terminal_flags_require_policy_acknowledgement_without_starting_listener(monkeypatch):
    monkeypatch.setattr(relay,'make_server',lambda *a,**kw:pytest.fail('option check must not listen'))
    base=['--owner-login','owner@example.test','--public-origin','https://viewer.example.ts.net:9443']
    with pytest.raises(SystemExit):relay.main(['--enable','--enable-terminal',*base])
    assert relay.main(['--check','--enable-terminal','--phone-policy-reviewed',*base])==0
