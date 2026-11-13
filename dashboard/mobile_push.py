"""Opt-in, owner-bound Web Push. No optional imports or writes while disabled.

The observer reads the same bounded roster/history adapters as the phone. It
never reads Mail storage or terminal files, and never treats process exit as
turn completion. Network publication is injectable for offline tests.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
import hashlib
import http.client
import importlib.metadata
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

VERSION = '2.5.0'
MAX_JSON = 16384
MAX_DEVICES, MAX_AGENTS, MAX_PENDING, MAX_SENT = 8, 128, 64, 512
INTERVAL, MAX_AGE, MAX_ATTEMPTS = 5, 120, 3
NAME = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,95}\Z')
ENDPOINT = re.compile(r'https://fcm\.googleapis\.com/(?:fcm/send|wp)/[A-Za-z0-9_:-]{16,1024}\Z')


class PushError(ValueError):
    """Fixed public code; never contains subscription, keys, paths or text."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def identity(agent):
    if isinstance(agent.get('_push_id'),str) and re.fullmatch(r'[a-f0-9]{64}',agent['_push_id']):
        return agent['_push_id']
    instance, number = agent.get('mail_instance_id'), agent.get('mail_agent_id')
    if (isinstance(instance, str) and re.fullmatch(r'[a-f0-9]{32}', instance)
            and type(number) is int and number > 0):
        return digest([instance, number])
    return None


def project_roster(raw):
    try:
        from dashboard.mobile_projection import project_agents
    except ModuleNotFoundError:
        from mobile_projection import project_agents
    projected=project_agents(raw)
    for source, agent in zip(raw['agents'],projected):
        # Legacy Mail reserves names permanently; its roster has no I/A.
        # Keep choices by that canonical name, but reset observations whenever
        # the tmux creation generation changes. Fresh uses its actual I/A.
        if identity(agent) is None:
            scope=source.get('project_key')
            agent['_push_id']=digest(['legacy',scope if isinstance(scope,str) else None,agent['name']])
        agent['_generation']=source.get('created')
    return projected


def dependency_ready():
    try:
        return importlib.metadata.version('pywebpush') == VERSION
    except importlib.metadata.PackageNotFoundError:
        return False


def runtime_python(root):
    """Only the exact installer-owned optional environment; no arbitrary path."""
    root=Path(root)
    path=root/'mobile-push-runtime.json'
    try:info=path.lstat()
    except FileNotFoundError:return None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
            or info.st_mode&0o077 or info.st_size>4096):
        raise PushError('runtime_unsafe')
    try:value=json.loads(path.read_bytes())
    except (OSError,ValueError):raise PushError('runtime_invalid') from None
    if (not isinstance(value,dict) or set(value)!={'version','python','candidate'} or value['version']!=1
            or not isinstance(value['candidate'],str) or not re.fullmatch(r'[a-f0-9]{20}',value['candidate'])):
        raise PushError('runtime_invalid')
    expected=root/'deps/mobile-push'/value['candidate']/'bin/python'
    if value['python']!=str(expected) or not os.access(expected,os.X_OK):
        raise PushError('runtime_invalid')
    return str(expected)


def subscription(value):
    if (not isinstance(value, dict) or set(value) - {'endpoint', 'expirationTime', 'keys'}
            or not isinstance(value.get('endpoint'), str) or not ENDPOINT.fullmatch(value['endpoint'])
            or not isinstance(value.get('keys'), dict) or set(value['keys']) != {'auth', 'p256dh'}):
        raise PushError('subscription_invalid')
    for name, length in (('p256dh', 65), ('auth', 16)):
        encoded = value['keys'][name]
        if not isinstance(encoded, str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,100}={0,2}', encoded):
            raise PushError('subscription_invalid')
        try:
            raw = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
        except ValueError:
            raise PushError('subscription_invalid') from None
        if len(raw) != length or name == 'p256dh' and raw[0] != 4:
            raise PushError('subscription_invalid')
    return {'endpoint': value['endpoint'], 'keys': dict(value['keys'])}


def preview(text):
    """Conservative plain-text preview, never code, images or sensitive lines."""
    if not isinstance(text, str):
        return ''
    text = text[:4096]
    if any(not (c.isprintable() or c.isspace()) for c in text):
        return ''
    sensitive = r'(?i)(token|secret|password|credential|bearer|api[ _-]?key|private.key|sk-[\w-]+)'
    path = r'(^|[\s\x22\x27(=])(?:/\S|~[/\\]|[A-Za-z]:[/\\])'
    if (re.search(sensitive, text) or re.search(path, text) or re.search(r'[A-Za-z0-9_+/=-]{24,}', text)
            or '`' in text or '/' in text or '\\' in text or '![' in text or '<' in text):
        return ''
    return re.sub(r'\s+', ' ', ''.join(c for c in text if c.isprintable() or c.isspace())).strip()[:600]


class SafeTransport:
    """pywebpush's session protocol, with pinned public IP + original TLS SNI.

    No requests proxy/environment, second DNS lookup, redirects or retry. Even
    allowed provider DNS must contain only global addresses. The socket talks
    only to a checked address; the certificate is checked for the fixed host.
    """
    def post(self, url, *, headers, data, timeout=None, **kwargs):
        if not ENDPOINT.fullmatch(url):
            raise PushError('destination_denied')
        parsed = urlsplit(url)
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        def public(address):
            ip=ipaddress.ip_address(address)
            return ip.is_global and not ip.is_multicast and not ip.is_reserved
        if not addresses or any(not public(row[4][0]) for row in addresses):
            raise PushError('destination_denied')
        address = addresses[0][4][0]
        class Pinned(http.client.HTTPSConnection):
            def connect(self):
                raw = socket.create_connection((address, 443), timeout=5)
                try:
                    self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=parsed.hostname)
                except BaseException:
                    raw.close()
                    raise
        conn = Pinned(parsed.hostname, timeout=5)
        try:
            conn.request('POST', parsed.path, body=data, headers=headers)
            response = conn.getresponse()
            response.read(4096)  # Do not retain or log provider error prose.
            return SimpleNamespace(status_code=response.status, text='', reason='', headers={})
        finally:
            conn.close()


def publish(sub, payload, key_file, contact):
    """Only called for opted-in devices; cryptography/VAPID stay in pywebpush."""
    from pywebpush import webpush, WebPushException
    try:
        response = webpush(subscription(sub), json.dumps(payload, ensure_ascii=False),
                           vapid_private_key=str(key_file), vapid_claims={'sub': contact},
                           content_encoding='aes128gcm', ttl=MAX_AGE, timeout=5,
                           requests_session=SafeTransport())
        return response.status_code
    except WebPushException as exc:
        return exc.response.status_code if exc.response is not None else 503


class PushService:
    def __init__(self, root, owner, origin, roster, history, demo, *, enabled=False,
                 sender=publish, ready=dependency_ready, clock=time.time):
        self.root, self.owner, self.origin = Path(root), owner, origin
        self.roster, self.history, self.demo = roster, history, demo
        self.enabled, self.sender, self.ready, self.clock = enabled, sender, ready, clock
        self.lock = threading.RLock()
        self.observer_lock = threading.Lock()
        self.devices, self.sent = {}, OrderedDict()
        self.previous, self.pending = {}, OrderedDict()
        self.stop = threading.Event()
        self.thread = None
        self.error = False
        self.last_tick = None
        if enabled:
            try:
                self._load()
                if self.devices:self._read('vapid.pem')
            except (OSError, ValueError, KeyError, TypeError):
                self.error = True

    def _directory(self, create=False):
        if create:
            self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PushError('storage_unsafe')

    def _read(self, name):
        self._directory()
        fd = os.open(self.root / name, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > 256*1024):
                raise PushError('storage_unsafe')
            return os.read(fd, 256*1024+1)
        finally:
            os.close(fd)

    def _write(self, name, data):
        self._directory(create=True)
        target = self.root / name
        if target.exists() or target.is_symlink():
            self._read(name)  # Reject unsafe old files before replacing them.
        fd, temporary = tempfile.mkstemp(prefix='.push-', dir=self.root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _load(self):
        try:
            value = json.loads(self._read('subscriptions.json'))
        except FileNotFoundError:
            return
        if (set(value) != {'owner', 'devices', 'sent'} or value['owner'] != digest([self.owner,self.origin])
                or not isinstance(value['devices'], dict) or len(value['devices']) > MAX_DEVICES
                or not isinstance(value['sent'], list) or len(value['sent']) > MAX_SENT):
            raise PushError('storage_invalid')
        for key, device in value['devices'].items():
            sub = subscription(device['subscription'])
            if (key != digest(sub['endpoint']) or set(device) != {'subscription','reply_needed','previews','agents'}
                    or any(type(device[k]) is not bool for k in ('reply_needed','previews'))
                    or not isinstance(device['agents'],dict) or len(device['agents']) > MAX_AGENTS
                    or any(not re.fullmatch(r'[a-f0-9]{64}', k) or not NAME.fullmatch(v) for k,v in device['agents'].items())):
                raise PushError('storage_invalid')
        if any(not isinstance(k,str) or not re.fullmatch(r'[a-f0-9]{64}',k) for k in value['sent']):
            raise PushError('storage_invalid')
        self.devices = value['devices']
        self.sent = OrderedDict.fromkeys(value['sent'])

    def _save(self):
        try:
            self._write('subscriptions.json', json.dumps({'owner':digest([self.owner,self.origin]),
                        'devices':self.devices,'sent':list(self.sent)},separators=(',',':')).encode())
        except (OSError,ValueError):
            self.error = True
            raise PushError('storage_unavailable') from None

    def capability(self):
        available = (self.enabled and not self.error and self.ready()
                     and bool(re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',self.owner)))
        return {'available':available, 'reason': '' if available else 'push_unavailable'}

    def public_key(self):
        if not self.capability()['available']:
            raise PushError('push_unavailable')
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization as ser
        with self.lock:
            try:
                key = ser.load_pem_private_key(self._read('vapid.pem'),password=None)
                if not isinstance(key,ec.EllipticCurvePrivateKey) or not isinstance(key.curve,ec.SECP256R1):
                    raise PushError('key_invalid')
            except FileNotFoundError:
                if self.devices:raise PushError('key_unavailable') from None
                key = ec.generate_private_key(ec.SECP256R1())
                self._write('vapid.pem',key.private_bytes(ser.Encoding.PEM,ser.PrivateFormat.PKCS8,ser.NoEncryption()))
            raw = key.public_key().public_bytes(ser.Encoding.X962,ser.PublicFormat.UncompressedPoint)
            return base64.urlsafe_b64encode(raw).decode().rstrip('=')

    def _agent(self, name):
        if not isinstance(name,str) or not NAME.fullmatch(name):
            raise PushError('agent_unavailable')
        rows = [a for a in self.roster() if a.get('name') == name and not a.get('retired')]
        if len(rows) != 1 or not identity(rows[0]):
            raise PushError('agent_unavailable')
        return identity(rows[0])

    def call(self, operation, value):
        if not self.capability()['available']:
            raise PushError('push_unavailable')
        fields = {'key':set(),'status':{'subscription','agent'},'remove':{'subscription'},
                  'save':{'subscription','agent','reply_needed','finished','previews'}}
        if operation not in fields or not isinstance(value,dict) or set(value) != fields[operation]:
            raise PushError('request_invalid')
        key=self._agent(value['agent']) if operation in {'status','save'} else None
        with self.lock:
            if operation == 'key':
                return {'key':self.public_key()}
            sub = subscription(value['subscription'])
            device_id = digest(sub['endpoint'])
            device = self.devices.get(device_id)
            if device is not None and device['subscription'] != sub:
                raise PushError('subscription_invalid')
            if operation == 'remove':
                self.devices.pop(device_id,None)
                self.pending = OrderedDict((k,v) for k,v in self.pending.items() if v['device'] != device_id)
                self._save()
                return {'enabled':False}
            if operation == 'save':
                from cryptography.hazmat.primitives.asymmetric import ec
                try:
                    raw=base64.urlsafe_b64decode(sub['keys']['p256dh']+'='*(-len(sub['keys']['p256dh'])%4))
                    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(),raw)
                except ValueError:
                    raise PushError('subscription_invalid') from None
                if any(type(value[k]) is not bool for k in ('reply_needed','finished','previews')):
                    raise PushError('request_invalid')
                if device is None and len(self.devices) >= MAX_DEVICES:
                    raise PushError('device_limit')
                self._read('vapid.pem')  # A subscription cannot outlive/miss its signing key.
                device = device or {'subscription':sub,'agents':{}}
                device.update(reply_needed=value['reply_needed'],previews=value['previews'])
                if value['finished']:
                    if key not in device['agents'] and len(device['agents']) >= MAX_AGENTS:
                        raise PushError('agent_limit')
                    device['agents'][key] = value['agent']
                else:
                    device['agents'].pop(key,None)
                self.devices[device_id] = device
                self._save()
            return {'enabled':device is not None,'reply_needed':device['reply_needed'] if device else True,
                    'finished':key in device['agents'] if device else False,
                    'previews':device['previews'] if device else False}

    def _latest(self, name, *, require_tail=False):
        try:
            page = self.history(name)
            events = page['events']
            # A user message after the last answer is not a finished response.
            text = [e for e in events if e.get('kind') == 'text' and e.get('role') in {'user','assistant'}]
            if require_tail and (not text or text[-1].get('role') != 'assistant'):
                return None, ''
            answers=[e for e in text if e.get('role')=='assistant']
            if not answers:return '-', ''
            event = answers[-1]
            return event.get('id',''), preview(event.get('text',''))
        except (OSError, ValueError, KeyError, TypeError):
            return None, ''

    def _event(self, agent, kind, event_id, body, expected):
        key, now = identity(agent), self.clock()
        stamp = digest([key,kind,event_id])
        for did, device in self.devices.items():
            if not (device['reply_needed'] if kind == 'reply' else key in device['agents']):
                continue
            token = digest([did,stamp])
            if token in self.sent:
                continue
            self.pending[(did,key,kind)] = {'device':did,'agent':key,'expected':expected,
                'token':token,'name':agent['name'],'body':body,'created':now,'due':now,'attempts':0}
            while len(self.pending) > MAX_PENDING:
                self.pending.popitem(last=False)

    def _reset(self):
        with self.lock:
            self.previous.clear();self.pending.clear();self.last_tick=None

    def _fresh(self, observed):
        if 0 <= self.clock()-observed <= 30:
            return True
        # A blocking adapter or send may span sleep, even within one tick.
        # Discard its snapshot; only the next fresh observation is a baseline.
        self._reset()
        return False

    def tick(self):
        # Never hold the settings lock while adapters/DNS/TLS wait. A slow
        # provider must not prevent the phone from turning notifications off.
        with self.observer_lock:
            observed = self.clock()
            if not self.devices or not self.capability()['available']:
                self._reset();return
            if self.last_tick is None or not 0<=observed-self.last_tick<=30:
                self._reset()
            self.last_tick=observed
            try:
                if not self._demo_off():
                    raise PushError('demo_unknown')
                if not self._fresh(observed):return
                agents = self.roster()
            except (OSError,ValueError,TypeError):
                self._reset();return
            if not self._fresh(observed):return
            latest={};reads=[]
            with self.lock:devices=[{**d,'agents':dict(d['agents'])} for d in self.devices.values()]
            for agent in agents[:5000]:
                if self.stop.is_set():return
                key,act=identity(agent),agent.get('act_state')
                old=self.previous.get(key)
                watched=any(key in d['agents'] or d['reply_needed'] and d['previews'] for d in devices)
                want_preview=any(d['reply_needed'] and d['previews'] for d in devices)
                if (old and watched and act=='work' and (act!=old['act'] or old.get('baseline_pending'))
                        or old and watched and act=='wait' and old['armed']
                        or old and want_preview and act=='question' and old['count']<=2):
                    # Carry-over baselines first, least recently attempted first.
                    # Failed reads must not starve later agents either.
                    priority=0 if act=='work' and old.get('baseline_pending') else 1 if act=='work' else 2
                    reads.append((priority,old.get('baseline_attempted',0),agent))
            for _,_,agent in sorted(reads,key=lambda item:item[:2])[:8]:
                if self.stop.is_set():return
                latest[agent['name']]=self._latest(agent['name'],require_tail=agent['act_state']!='work')
                if not self._fresh(observed):return
            if not self._fresh(observed):return
            with self.lock:self._observe(agents,latest)
            self._send_pending({identity(a):a.get('act_state') for a in agents if a.get('running') is True},observed)

    def _observe(self,agents,latest):
        current = {}
        for agent in agents[:5000]:
            key, act = identity(agent), agent.get('act_state')
            if not key or agent.get('running') is not True or act not in {'work','wait','ask','question'}:
                continue
            current[key] = act
            old = self.previous.get(key)
            if old is None or old.get('generation')!=agent.get('_generation'):
                self.previous[key] = {'act':act,'count':2,'armed':False,'baseline':None,'cycle':'initial','generation':agent.get('_generation')}
                continue
            if act != old['act']:
                if act == 'work':
                    old['armed'] = True
                    old['cycle'] = str(self.clock())
                    old['baseline'] = latest.get(agent['name'],(None,''))[0]
                    old['baseline_pending'] = old['baseline'] is None
                    old['baseline_attempted'] = self.clock() if agent['name'] in latest else 0
                elif act=='wait' and old['armed']:
                    old['until']=self.clock()+MAX_AGE
                elif act in {'ask','question'}:
                    old['armed'] = False
                old.update(act=act,count=1)
            else:
                old['count'] += 1
                if act=='work' and old['armed'] and old.get('baseline_pending') and agent['name'] in latest:
                    old['baseline'] = latest[agent['name']][0]
                    old['baseline_pending'] = old['baseline'] is None
                    old['baseline_attempted'] = self.clock()
            if old['count'] != 2 and not (act=='wait' and old['armed'] and old['count']>=2):
                continue
            if act in {'ask','question'}:
                event_id, body = latest.get(agent['name'],(None,'')) if act=='question' else (None,'')
                if old['baseline'] is None or event_id==old['baseline']:body=''
                fresh_id=event_id if event_id and event_id!='-' and event_id!=old['baseline'] else old['cycle']
                self._event(agent,'reply',[act,fresh_id],body,act)
            elif act == 'wait' and old['armed']:
                event_id, body = latest.get(agent['name'],(None,''))
                if old['baseline'] is not None and event_id and event_id!='-' and event_id != old['baseline']:
                    self._event(agent,'finished',event_id,body,act)
                    old['armed'] = False
                elif old['baseline'] is None or self.clock()>old['until']:
                    old['armed'] = False
        self.previous = {k:v for k,v in self.previous.items() if k in current}

    def _send_pending(self,current,observed):
        budget=4
        with self.lock:pending=list(self.pending.items())
        for slot,event in pending:
            if not self._fresh(observed):return
            now=self.clock()
            if event['attempts']>=MAX_ATTEMPTS or now-event['created']>MAX_AGE:
                with self.lock:self.pending.pop(slot,None)
                continue
            if event['due']>now or not budget or self.stop.is_set():continue
            try:
                if not self._demo_off():
                    self._reset();return
                self._read('vapid.pem')
                if not self._fresh(observed):return
                with self.lock:
                    now=self.clock()
                    device=self.devices.get(event['device'])
                    if (self.error or self.pending.get(slot) is not event or not device
                            or current.get(event['agent'])!=event['expected']
                            or now-event['created']>MAX_AGE or event['attempts']>=MAX_ATTEMPTS):
                        self.pending.pop(slot,None);continue
                    allowed=device['reply_needed'] if slot[2]=='reply' else event['agent'] in device['agents']
                    if not allowed:self.pending.pop(slot,None);continue
                    fallback='Approval requested' if event['expected']=='ask' else 'Question' if slot[2]=='reply' else ''
                    payload={'title':event['name']+(' — Reply needed' if slot[2]=='reply' else ' — Work finished'),
                             'body':event['body'] if device['previews'] and event['body'] else fallback,
                             'tag':digest([event['agent'],slot[2]])[:32],'agent':event['name']}
                    sub=dict(device['subscription'])
                if not self._fresh(observed):return
                code=self.sender(sub,payload,self.root/'vapid.pem','mailto:'+self.owner)
            except (OSError,ValueError):code=503
            budget-=1
            with self.lock:
                if self.pending.get(slot) is event:
                    event['attempts']+=1
                    if code in (200,201,202):
                        self.sent[event['token']]=None
                        while len(self.sent)>MAX_SENT:self.sent.popitem(last=False)
                        self._save();self.pending.pop(slot,None)
                    elif code in (404,410):
                        self.devices.pop(event['device'],None);self._save();self.pending.pop(slot,None)
                    elif code==429 or code>=500:event['due']=self.clock()+5*3**event['attempts']
                    else:self.pending.pop(slot,None)
            if not self._fresh(observed):return

    def _demo_off(self):
        value=self.demo()
        return isinstance(value,dict) and value.get('supported') is True and value.get('on') is False

    def start(self):
        if not self.capability()['available'] or self.thread:
            return
        def run():
            while not self.stop.wait(INTERVAL):
                try:self.tick()
                except Exception:
                    # Never log provider exceptions or user content. Fail closed
                    # after a persistence/adapter bug rather than loop publication.
                    self.error = True
        self.thread = threading.Thread(target=run,name='mobile-push',daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(20)
