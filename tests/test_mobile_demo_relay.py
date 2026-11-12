"""The owner-only Demo reader has literal loopback routes and bounded bodies."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json

import pytest

from dashboard import mobile_relay as relay
from test_mobile_relay import Store, request, serving


OFF=dict(version=1,on=False,words=[],identity=None)
ON=dict(version=1,on=True,words=['Orchid'],identity=dict(users=['mira'],hosts=['mira-node']))


@pytest.mark.parametrize('path',['/view/demo','/demo-mask.js','/demo-mode.js'])
def test_new_routes_keep_owner_admission_and_head_does_not_fetch(path):
    calls=[]
    class Demo:
        def status(self):calls.append('state');return dict(OFF,supported=True)
        def asset(self,name):calls.append(name);return b'/* synthetic */'
    server=relay.make_server(relay.Config('owner@example.test','https://viewer.example.ts.net:9443'),store=Store(),demo_client=Demo(),test_port=0)
    with serving(server) as port:
        assert request(port,path,headers={'Host':'viewer.example.ts.net:9443','Tailscale-User-Login':'other@example.test'})[0]==403
        assert request(port,path,method='HEAD')[0]==200
        assert calls==[]
        status,headers,data=request(port,path)
        assert status==200 and headers['Cache-Control']=='no-store'
        assert len(calls)==1
        if path=='/view/demo':assert json.loads(data)==dict(OFF,supported=True)
        else:assert headers['Content-Type'].startswith('text/javascript') and data==b'/* synthetic */'
        assert request(port,path+'?path=/etc/passwd')[0]==404
        assert len(calls)==1


@pytest.mark.parametrize('status,value,expected',[
    (200,OFF,'off'),(200,ON,'on'),(404,{},'unsupported'),(503,{},'error'),
    (200,dict(ON,on=1),'error'),(200,dict(ON,version=True),'error'),
    (200,dict(ON,identity=dict(users=['mira'],hosts=['mira-node'],home='/Users/mira')),'error'),
    (200,dict(ON,identity=dict(users=[],hosts=[])),'error'),
    (200,dict(OFF,words=['Orchid']),'error'),
    (200,dict(ON,words=['x'*5000]),'on'),
    (200,dict(ON,words=['ab']*300),'on'),
    (200,dict(ON,words=['x'*16385]),'error'),
    (200,dict(ON,extra='secret'),'error'),
])
def test_http_errors_never_silently_disable_demo(status,value,expected):
    seen=[]
    class Backend(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_GET(self):
            seen.append((self.path,self.headers['Host'],self.headers['Accept']))
            data=json.dumps(value).encode();self.send_response(status)
            self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)))
            self.end_headers();self.wfile.write(data)
    with serving(ThreadingHTTPServer(('127.0.0.1',0),Backend)) as port:
        client=relay.DemoClient(port)
        if expected=='error':
            with pytest.raises(ValueError):client.status()
        else:
            result=client.status()
            assert result['supported']==(expected!='unsupported')
            assert result['on']==(expected=='on')
        assert seen==[('/telemetry/demo',f'127.0.0.1:{port}','application/json')]


def test_reader_without_a_running_desktop_keeps_its_original_view():
    server=ThreadingHTTPServer(('127.0.0.1',0),BaseHTTPRequestHandler)
    port=server.server_port;server.server_close()
    assert relay.DemoClient(port).status()==dict(OFF,supported=False)


@pytest.mark.parametrize('mime',['text/javascript','application/javascript'])
def test_only_the_two_maintained_scripts_can_be_read(mime):
    seen=[]
    class Backend(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_GET(self):
            seen.append(self.path);self.send_response(200);self.send_header('Content-Type',mime)
            self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'//')
    with serving(ThreadingHTTPServer(('127.0.0.1',0),Backend)) as port:
        client=relay.DemoClient(port)
        for name in ('/demo-mask.js','/demo-mode.js'):assert client.asset(name)==b'//'
        for name in ('/etc/passwd','http://outside.test/','/demo-mode.js?path=other'):
            with pytest.raises(ValueError):client.asset(name)
        assert seen==['/demo_mask.js','/demo_mode.js']
