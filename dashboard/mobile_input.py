"""Finite authenticated-reader client for the opt-in cockpit input adapter.

No connection or process starts on import. No request text is kept on this
object, written to disk, or placed in a URL. The reader supplies owner/scope;
phone payloads cannot replace them. A failed commit is never retried here.
"""
from __future__ import annotations

import http.client
import json
import re
import secrets
import socket
import threading
import time

CONTRACT='orrery-mobile-pane-v1'
MAX_JSON_BYTES=16384
HEX=re.compile(r'[a-f0-9]{64}\Z')
EPOCH=re.compile(r'[a-f0-9]{32}\Z')
NAME=re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,95}\Z')
OPERATIONS={'targets':{'epoch','name'},'preview':{'epoch','choice'},'prepare':{'epoch','choice','revision','text'},
            'commit':{'epoch','intent','confirmation'},'cancel':{'epoch','intent'},'status':{'epoch','intent'}}


class InputUnavailable(ValueError):
    pass


def decode_json(data):
    if not isinstance(data,bytes) or len(data)>MAX_JSON_BYTES: raise InputUnavailable('input_json_invalid')
    def pairs(items):
        result={}
        for key,value in items:
            if key in result: raise InputUnavailable('input_json_invalid')
            result[key]=value
        return result
    def constant(value): raise InputUnavailable('input_json_invalid')
    try: value=json.loads(data.decode('utf-8'),object_pairs_hook=pairs,parse_constant=constant)
    except (ValueError,UnicodeError,RecursionError): raise InputUnavailable('input_json_invalid') from None
    if not isinstance(value,dict): raise InputUnavailable('input_json_invalid')
    return value


def _matches(pattern,value):
    return isinstance(value,str) and pattern.fullmatch(value) is not None


def _public_target(value,*,proof=True):
    expected={'name','pane_label','choice'}|({'revision'} if proof else set())
    if (not isinstance(value,dict) or set(value)!=expected or not _matches(NAME,value['name'])
            or not isinstance(value['pane_label'],str) or not 1<=len(value['pane_label'])<=96
            or any(ord(c)<32 or ord(c)==127 for c in value['pane_label'])
            or not _matches(HEX,value['choice']) or proof and not _matches(HEX,value['revision'])):
        raise InputUnavailable('input_response_invalid')
    return dict(value)


class InputClient:
    def __init__(self,port,owner):
        if type(port) is not int or not 1<=port<=65535: raise ValueError('input_port')
        self.port=port; self.owner=owner; self.reader_epoch=secrets.token_hex(16)

    def call(self,operation,payload=None):
        payload={} if payload is None else payload
        fields=set() if operation=='capability' else OPERATIONS.get(operation)
        if fields is None or not isinstance(payload,dict) or set(payload)!=fields:
            raise InputUnavailable('input_request_invalid')
        request=dict(payload,contract=CONTRACT,owner=self.owner,reader_epoch=self.reader_epoch)
        try: data=json.dumps(request,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
        except (ValueError,UnicodeError): raise InputUnavailable('input_request_invalid') from None
        if len(data)>MAX_JSON_BYTES: raise InputUnavailable('input_request_invalid')
        value=self._post(operation,data)
        if value.get('contract')!=CONTRACT or not _matches(EPOCH,value.get('epoch')):
            raise InputUnavailable('input_capability_incompatible')
        if operation=='capability':
            if (set(value)!={'contract','api','epoch','enabled','supported','ack'} or type(value['api']) is not int
                    or value['api']!=31 or type(value['enabled']) is not bool
                    or value['supported']!='legacy-managed-pane' or value['ack']!='tmux-command-accepted'):
                raise InputUnavailable('input_capability_incompatible')
            return value
        if value['epoch']!=payload['epoch']: raise InputUnavailable('input_epoch_changed')
        if operation=='targets':
            if (set(value)!={'contract','epoch','targets','truncated'} or not isinstance(value['targets'],list)
                    or len(value['targets'])>128 or type(value['truncated']) is not bool):raise InputUnavailable('input_response_invalid')
            targets=[_public_target(v,proof=False) for v in value['targets']]
            if len({t['choice'] for t in targets})!=len(targets) or any(t['name']!=payload['name'] for t in targets):raise InputUnavailable('input_response_invalid')
            value['targets']=targets;return value
        if operation=='preview':
            expected={'contract','epoch','target','text','truncated','controls_removed','observed_at','max_lines','max_bytes'}
            if set(value)!=expected:raise InputUnavailable('input_response_invalid')
            value['target']=_public_target(value['target'])
            if (value['target']['choice']!=payload['choice'] or not isinstance(value['text'],str)
                    or len(value['text'].encode('utf-8'))>6144 or len(value['text'].splitlines())>40
                    or type(value['truncated']) is not bool or type(value['controls_removed']) is not bool
                    or type(value['observed_at']) is not int or value['observed_at']<=0
                    or value['max_lines']!=40 or value['max_bytes']!=6144):raise InputUnavailable('input_response_invalid')
            return value
        expected={'contract','epoch','target','state','intent'}
        if operation=='prepare':expected|={'confirmation','expires_in','text'}
        if set(value)!=expected:raise InputUnavailable('input_response_invalid')
        value['target']=_public_target(value['target'])
        if (not _matches(HEX,value['intent']) or value['state'] not in {'prepared','pending','accepted','refused','unknown','cancelled','expired'}
                or operation!='prepare' and value['intent']!=payload['intent']):raise InputUnavailable('input_response_invalid')
        if operation=='prepare':
            if (value['text']!=payload['text'] or value['target']['choice']!=payload['choice']
                    or value['target']['revision']!=payload['revision'] or not _matches(HEX,value['confirmation'])
                    or type(value['expires_in']) is not int or value['expires_in']!=30 or value['state']!='prepared'):
                raise InputUnavailable('input_response_invalid')
        return value

    def _post(self,operation,data):
        # Every operation has one fixed path. This is not the cockpit proxy or
        # terminal WebSocket. No redirects, environment proxy or auto-retry.
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=7)
        timer=None; deadline=time.monotonic()+7
        try:
            connection.connect(); sock=connection.sock
            def expire():
                try: sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass
            timer=threading.Timer(max(.001,deadline-time.monotonic()),expire); timer.daemon=True; timer.start()
            connection.request('POST','/_mobile/terminal/'+operation,body=data,headers={
                'Host':f'127.0.0.1:{self.port}','Origin':f'http://127.0.0.1:{self.port}',
                'X-ORRERY-Mobile-Contract':CONTRACT,'Content-Type':'application/json','Connection':'close'})
            response=connection.getresponse(); lengths=response.headers.get_all('Content-Length',[])
            if (response.status!=200 or response.getheader('Content-Type','').split(';')[0]!='application/json'
                    or response.headers.get_all('Transfer-Encoding',[]) or response.headers.get_all('Content-Encoding',[])
                    or response.getheader('Cache-Control')!='no-store' or len(lengths)!=1
                    or re.fullmatch(r'[0-9]{1,5}',lengths[0]) is None or not 0<int(lengths[0])<=MAX_JSON_BYTES):
                raise InputUnavailable('input_unavailable')
            body=response.read(MAX_JSON_BYTES+1)
            if len(body)!=int(lengths[0]) or time.monotonic()>=deadline:
                raise InputUnavailable('input_unavailable')
            return decode_json(body)
        except (OSError,http.client.HTTPException,ValueError):
            raise InputUnavailable('input_unavailable') from None
        finally:
            if timer is not None: timer.cancel()
            connection.close()
