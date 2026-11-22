"""One operator capability admission for all non-static global HTTP routes.

TCP does not prove peer UID. Browser provenance is additional admission,
never a replacement for the private capability. No service auto-provisions it.
"""
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
from urllib.parse import urlsplit

KIND = 'orrery-http-operator-v1'


class AdmissionError(RuntimeError):
    def __init__(self, reason, status=403):
        super().__init__(reason)
        self.status = status


def capability_path(home):
    home = Path(home)
    if not home.is_absolute() or home.resolve() != home:
        raise AdmissionError('HTTP_OPERATOR_UNAVAILABLE', 503)
    return home / 'runtime' / 'http-operator.json'


def read_capability(home):
    path = capability_path(home)
    try:
        for parent in (path.parent.parent, path.parent):
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            current = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1
                    or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
                raise ValueError
            raw = stream.read(4097)
        value = json.loads(raw)
        if len(raw)>4096 or set(value) != {'kind','capability'} or value['kind'] != KIND or not re.fullmatch(r'[A-Za-z0-9_-]{43}', value['capability']):
            raise ValueError
        return value['capability']
    except (OSError, ValueError, TypeError, KeyError):
        raise AdmissionError('HTTP_OPERATOR_UNAVAILABLE',503) from None


def configure(home):
    """Explicit local operator action; O_EXCL refuses overwrite/rotation."""
    path = capability_path(home)
    for parent in (path.parent.parent, path.parent):
        parent.mkdir(mode=0o700, exist_ok=True)
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
            raise AdmissionError('HTTP_OPERATOR_UNAVAILABLE',503)
    fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd,'w') as out:
        json.dump(dict(kind=KIND,capability=secrets.token_urlsafe(32)),out)
        out.flush();os.fsync(out.fileno())
    return path


class Admission:
    def __init__(self, home, origin):
        self.home = Path(home)
        self.origin = origin
        url = urlsplit(origin)
        if url.scheme!='http' or url.hostname not in ('127.0.0.1','localhost','::1') or not url.port or url.path or url.query or url.fragment or url.username:
            raise AdmissionError('HTTP_OPERATOR_ORIGIN_INVALID',503)
        self.hosts = {f'127.0.0.1:{url.port}',f'localhost:{url.port}',f'[::1]:{url.port}'}

    def network(self, headers, peer, *, websocket=False):
        reason = 'HTTP_OPERATOR_REQUIRED'
        try:
            if not ipaddress.ip_address(peer).is_loopback:
                raise ValueError
            for key in ('Host','Origin','Referer','Sec-Fetch-Site','Authorization'):
                if len(headers.get_all(key, []))>1:
                    raise ValueError
            reason = 'HTTP_HOST_FORBIDDEN'
            if headers.get('Host') not in self.hosts:
                raise ValueError
            reason = 'HTTP_ORIGIN_FORBIDDEN'
            origin = headers.get('Origin')
            if (websocket and origin is None) or (origin is not None and origin != self.origin):
                raise ValueError
            site = headers.get('Sec-Fetch-Site')
            if site is not None and site!='same-origin':
                raise ValueError
            referer = headers.get('Referer')
            if referer is not None:
                ref=urlsplit(referer)
                if f'{ref.scheme}://{ref.netloc}'!=self.origin:
                    raise ValueError
        except ValueError:
            raise AdmissionError(reason) from None
        # Validate availability even for a WS upgrade; no side effects.
        read_capability(self.home)

    def authenticate(self, capability):
        expected=read_capability(self.home)
        # Only the issued URL-safe alphabet is admitted; this also prevents
        # malformed Unicode from escaping as a handler exception.
        if (not isinstance(capability,str) or re.fullmatch(r'[A-Za-z0-9_-]{43}',capability) is None
                or not hmac.compare_digest(capability.encode('ascii'),expected.encode('ascii'))):
            raise AdmissionError('HTTP_OPERATOR_REQUIRED')

    def http(self, headers, peer):
        self.network(headers,peer)
        value=headers.get('Authorization','')
        if not value.startswith('Bearer '):
            raise AdmissionError('HTTP_OPERATOR_REQUIRED')
        self.authenticate(value[7:])


def probe(home, *, service, telemetry_url, cockpit_port, config_path):
    """Read admitted service proof; never print capability or owner credentials."""
    import runpy
    from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
    root = Path(__file__).resolve().parents[1]
    operator = runpy.run_path(str(root/'bin/lib/runtime_operator.py'))['RuntimeOperator'](config_path)
    token = read_capability(home)
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise AdmissionError('HTTP_OPERATOR_PROBE_FAILED',503)
    def get(origin, path):
        # Reuse the exact existing loopback origin validation.
        Admission(home, origin)
        request = Request(origin+path, headers={'Authorization':'Bearer '+token,'Origin':origin})
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=5) as response:
                return json.loads(response.read(1048577))
        except Exception as exc:
            raise AdmissionError('HTTP_OPERATOR_PROBE_FAILED',503) from exc
    version = get(telemetry_url, '/api/version')
    required = {'global_runtime_observation':1, 'operator_session':1}
    caps = version.get('capabilities',{})
    if version.get('runtime_mode') != 'global' or any(caps.get(k)!=v for k,v in required.items()):
        raise AdmissionError('COCKPIT_GLOBAL_CAPABILITY_REQUIRED',503)
    health = get(telemetry_url, '/api/mail-watcher-health')
    if health.get('binding') != operator.binding or health.get('data',{}).get('server_ready') is not True:
        raise AdmissionError('MANAGEMENT_BINDING_MISMATCH',503)
    cockpit_ready = False
    if service == 'cockpit':
        origin = 'http://127.0.0.1:'+str(int(cockpit_port))
        cockpit = get(origin, '/telemetry/health')
        upstream = cockpit.get('upstream',{})
        if (cockpit.get('runtime_mode') != 'global' or upstream.get('binding') != operator.binding
                or any(cockpit.get('capabilities',{}).get(k)!=1 for k in (
                    'operator_http_admission','global_mail_bridge','instruction_migration',
                    'websocket_first_frame_auth','global_runtime_observation'))
                or any(upstream.get('capabilities',{}).get(k)!=v for k,v in required.items())):
            raise AdmissionError('COCKPIT_GLOBAL_CAPABILITY_REQUIRED',503)
        cockpit_ready = True
    return {'ok':True,'runtime_mode':'global','telemetry_api':version['api'],
            'capabilities':caps,'binding':operator.binding,'cockpit_ready':cockpit_ready,
            'cutover_ready':False}
