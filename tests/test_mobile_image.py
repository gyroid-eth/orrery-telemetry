"""Image requests use the same owner and fixed bridge transport as text."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json

import pytest

from dashboard import mobile_input, mobile_relay
from test_mobile_relay import Store, serving


@pytest.mark.parametrize('case',['host','origin','identity_missing','identity_other','opt_in_off'])
def test_image_route_denies_outsiders_before_any_save(tmp_path,case):
    saved=tmp_path/'must-not-be-written.png'
    class Input:
        def call(self,*args,**kwargs):
            saved.write_bytes(b'unexpected')
            raise AssertionError('unauthenticated image reached the bridge')
    cfg=mobile_relay.Config('owner@example.test','https://viewer.example.ts.net',input_enabled=case!='opt_in_off',phone_policy_reviewed=True)
    server=mobile_relay.make_server(cfg,store=Store(),input_client=Input(),test_port=0)
    assert server.server_address[0]=='127.0.0.1'
    with serving(server) as port:
        headers={'Host':'viewer.example.ts.net','Origin':'https://viewer.example.ts.net','Tailscale-User-Login':'owner@example.test','Content-Type':'application/json'}
        if case=='host':headers['Host']='outsider.example.test'
        if case=='origin':headers['Origin']='https://outsider.example.test'
        if case=='identity_missing':del headers['Tailscale-User-Login']
        if case=='identity_other':headers['Tailscale-User-Login']='outsider@example.test'
        con=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
        try:
            con.request('POST','/view/terminal/image',body=json.dumps(dict(epoch='a'*32,choice='b'*64,revision='c'*64,image='YWJj')).encode(),headers=headers)
            response=con.getresponse();response.read()
            assert response.status in {403,405}
        finally:con.close()
    assert not saved.exists()


@pytest.mark.parametrize('case',['probe','upload','wrong_owner','oversized_text','error'])
def test_image_relay_and_client_keep_scope_and_existing_text_limit(case):
    calls=[]
    target=dict(name='FixtureCurie',pane_label='Window 0 · pane 0',choice='b'*64,revision='c'*64)
    class Adapter(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path,body))
            result=dict(contract=mobile_input.CONTRACT,epoch='a'*32,target=target,enabled=True)
            if body.get('image') is not None:result['path']='/workspace/.orrery-input-images/'+'d'*32+'.png'
            if case=='error':result={'error':'image_folder_full'}
            data=json.dumps(result).encode()
            self.send_response(403 if case=='error' else 200)
            for key,value in [('Content-Type','application/json'),('Content-Length',str(len(data))),('Cache-Control','no-store')]:self.send_header(key,value)
            self.end_headers();self.wfile.write(data)
    with serving(ThreadingHTTPServer(('127.0.0.1',0),Adapter)) as bridge_port:
        client=mobile_input.InputClient(bridge_port,'owner@example.test')
        cfg=mobile_relay.Config('owner@example.test','https://viewer.example.ts.net',input_enabled=True,phone_policy_reviewed=True)
        with serving(mobile_relay.make_server(cfg,store=Store(),input_client=client,test_port=0)) as port:
            value=dict(epoch='a'*32,choice='b'*64,revision='c'*64,image=None if case=='probe' else 'YQ=='*5000)
            headers={'Host':'viewer.example.ts.net','Origin':'https://viewer.example.ts.net','Tailscale-User-Login':'other@example.test' if case=='wrong_owner' else 'owner@example.test','Content-Type':'application/json'}
            operation='image'
            if case=='oversized_text':operation='prepare';value=dict(epoch='a'*32,choice='b'*64,revision='c'*64,text='x'*20000)
            con=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
            try:
                con.request('POST','/view/terminal/'+operation,body=json.dumps(value).encode(),headers=headers)
                response=con.getresponse();result=json.loads(response.read())
                assert response.status==(403 if case in {'wrong_owner','oversized_text'} else 503 if case=='error' else 200)
            finally:con.close()
            if case in {'wrong_owner','oversized_text'}:assert calls==[]
            else:
                assert calls[0][0]=='/_mobile/terminal/image'
                assert calls[0][1]['owner']=='owner@example.test' and calls[0][1]['reader_epoch']==client.reader_epoch
                if case=='error':assert result['error']=='image_folder_full'
                if case=='upload':assert result['path'].endswith('.png')
