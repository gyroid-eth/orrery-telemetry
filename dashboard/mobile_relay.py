#!/usr/bin/env python3
"""Opt-in mobile reader behind Tailscale Serve; never starts from normal install."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

try:
    from dashboard import legacy_http_admission as admission
    from dashboard.mobile_projection import SnapshotStore, WINDOWS
except ModuleNotFoundError:
    import legacy_http_admission as admission
    from mobile_projection import SnapshotStore, WINDOWS

HERE = Path(__file__).resolve().parent
MAX_UPSTREAM_BYTES = 4 * 1024 * 1024
UPSTREAM_TIMEOUT = 2
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "font-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


@dataclass(frozen=True)
class Config:
    owner_login: str
    public_origin: str
    listen_port: int = 8781
    upstream_port: int = 8770

    def __post_init__(self):
        if (not self.owner_login or len(self.owner_login)>254 or
                not self.owner_login.isascii() or any(ord(x)<=32 or ord(x)>=127 for x in self.owner_login)):
            raise ValueError('owner login must be the exact ASCII Serve login')
        scheme, host, _ = admission.origin(self.public_origin)
        if scheme != 'https' or not host.endswith('.ts.net'):
            raise ValueError('public origin must be a fixed HTTPS .ts.net origin')
        if (type(self.listen_port) is not int or type(self.upstream_port) is not int or
                not 1 <= self.listen_port <= 65535 or not 1 <= self.upstream_port <= 65535 or
                self.listen_port == self.upstream_port):
            raise ValueError('valid separate relay/upstream ports are required')


class Upstream:
    """Literal loopback plus a finite internal route set; no proxy/redirects."""
    def __init__(self, port):
        self.port = port

    def __call__(self, key):
        if key == 'agents':
            path = '/api/agents'
        elif key == 'graph':
            path = '/api/graph?all=1'
        elif key.startswith('mail:') and key[5:] in WINDOWS:
            path = '/api/mobile-mail?window=' + key[5:]
        else:
            raise ValueError('internal_route')
        con = http.client.HTTPConnection('127.0.0.1', self.port, timeout=UPSTREAM_TIMEOUT)
        timer = None
        deadline = time.monotonic() + UPSTREAM_TIMEOUT
        try:
            con.connect()
            sock = con.sock
            def expire():
                try:sock.shutdown(socket.SHUT_RDWR)
                except OSError:pass
            timer = threading.Timer(max(0.001,deadline-time.monotonic()),expire)
            timer.daemon = True
            timer.start()
            con.request('GET', path, headers={'Host':f'127.0.0.1:{self.port}',
                                            'Accept':'application/json','Connection':'close'})
            response = con.getresponse()
            if response.status != 200 or response.getheader('Content-Type','').split(';')[0].strip().lower() != 'application/json':
                raise ValueError('upstream_unavailable')
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise ValueError('upstream_encoding')
            length = response.getheader('Content-Length')
            if length is not None and (not length.isdigit() or int(length)>MAX_UPSTREAM_BYTES):
                raise ValueError('upstream_size')
            data=response.read(MAX_UPSTREAM_BYTES+1)
            if len(data)>MAX_UPSTREAM_BYTES:raise ValueError('upstream_size')
            return json.loads(data)
        except (http.client.HTTPException, UnicodeError) as exc:
            raise ValueError('upstream_protocol') from exc
        finally:
            if timer is not None:timer.cancel()
            con.close()


def static_assets(root=HERE):
    """Fixed bundled files only; no file path, overlay or provider lookup."""
    fixed = {
        '/': (root/'mobile/index.html', 'text/html; charset=utf-8'),
        '/mobile.css': (root/'mobile/mobile.css', 'text/css; charset=utf-8'),
        '/mobile.js': (root/'mobile/mobile.js', 'text/javascript; charset=utf-8'),
        '/network_view.js': (root/'mobile/network_view.js', 'text/javascript; charset=utf-8'),
        '/network_core.js': (root/'network_core.js', 'text/javascript; charset=utf-8'),
        '/network_node.js': (root/'network_node.js', 'text/javascript; charset=utf-8'),
    }
    for font in ('fraunces-normal','fraunces-italic','ibm-plex-mono-400','ibm-plex-mono-500','ibm-plex-mono-600'):
        fixed['/fonts/'+font+'.woff2']=(root/'mobile/fonts'/(font+'.woff2'),'font/woff2')
    for logo in ('orbit','anthropic','openai','google','qwen'):
        fixed['/assets/'+logo+'.svg']=(root/'assets'/(logo+'.svg'),'image/svg+xml')
    # Byte-identical canonical icon, bundled for the manual mobile reader even
    # when ordinary dashboard DeepSeek presentation is not opted in.
    fixed['/assets/deepseek.svg']=(root/'mobile/assets/deepseek.svg','image/svg+xml')
    # Names come from a fixed product-owned directory, never operator overlays.
    # The allowlist is fixed at server construction; traversal/query is rejected.
    for p in (root/'portraits_64').glob('*.png'):
        if re.fullmatch(r'[A-Za-z]+',p.stem) and not p.is_symlink():
            fixed['/portraits/'+p.name]=(p,'image/png')
    return fixed


class Handler(BaseHTTPRequestHandler):
    server_version = 'ORRERY-mobile'
    sys_version = ''
    protocol_version = 'HTTP/1.0'

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        # Do not persist raw request paths, supplied logins or query strings.
        pass

    def send_error(self, code, message=None, explain=None):
        self.close_connection = True
        self._send(code, {'ok':False,'error':'request_rejected'})

    def _send(self, status, data, mime='application/json; charset=utf-8'):
        if isinstance(data, dict):data=json.dumps(data,separators=(',',':'),allow_nan=False).encode()
        self.send_response(status)
        for key,value in [('Content-Type',mime),('Content-Length',str(len(data))),
                          ('Cache-Control','no-store'),('Content-Security-Policy',CSP),
                          ('X-Content-Type-Options','nosniff'),('Referrer-Policy','no-referrer'),
                          ('X-Frame-Options','DENY'),('Permissions-Policy','camera=(), microphone=(), geolocation=()')]:
            self.send_header(key,value)
        self.end_headers()
        if self.command != 'HEAD':self.wfile.write(data)

    def _admitted(self):
        cfg=self.server.mobile_config
        hosts=self.headers.get_all('Host',[])
        users=self.headers.get_all('Tailscale-User-Login',[])
        origins=self.headers.get_all('Origin',[])
        try:
            expected=admission.origin(cfg.public_origin)
            if len(hosts)!=1 or len(users)!=1 or users[0]!=cfg.owner_login or len(origins)>1:
                return False
            host,port=admission.authority(hosts[0])
            if (host,port if port is not None else 443)!=expected[1:]:return False
            if origins and admission.origin(origins[0])!=expected:return False
        except (ValueError,TypeError):
            return False
        if self.headers.get_all('Authority',[]):return False
        if self.headers.get('Sec-Fetch-Site','').lower() not in {'','none','same-origin'}:return False
        if any(self.headers.get_all(h,[]) for h in ('Upgrade','Transfer-Encoding','X-HTTP-Method-Override','X-Method-Override','X-HTTP-Method')):return False
        lengths=self.headers.get_all('Content-Length',[])
        if len(lengths)>1 or (lengths and lengths[0]!='0'):return False
        words = self.requestline.split()
        target = words[1] if len(words) == 3 else ''
        return target == self.path and target.startswith('/') and not target.startswith('//') and '#' not in target and '%' not in target and '\\' not in target

    def do_GET(self):
        if not self._admitted():
            self._send(403,{'ok':False,'error':'request_denied'});return
        parts=urlsplit(self.path)
        if parts.path=='/view/snapshot':
            query=parse_qs(parts.query,keep_blank_values=True)
            if set(query)-{'window'} or len(query.get('window',[]))>1 or query.get('window',['all'])[0] not in WINDOWS:
                self._send(400,{'ok':False,'error':'invalid_window'});return
            if self.command=='HEAD':self._send(200,b'');return
            payload=self.server.mobile_store.snapshot(query.get('window',['all'])[0])
            unavailable=all(x['status']=='unavailable' for x in payload['sources'].values())
            self._send(503 if unavailable else 200,payload);return
        if parts.query:
            self._send(404,{'ok':False,'error':'not_found'});return
        if parts.path=='/network.css':
            # One maintained PC stylesheet. Only its authored style block,
            # never the full dashboard HTML/script/operation surface, is served.
            try:
                text=(self.server.mobile_root/'index.html').read_text()
                style=re.search(r'<style id="agentstack-dashboard-styles">(.*?)</style>',text,re.S)
                if style is None:raise ValueError('style_missing')
                self._send(200,style[1].encode(),'text/css; charset=utf-8')
            except (OSError,ValueError):self._send(503,{'ok':False,'error':'asset_unavailable'})
            return
        asset=self.server.mobile_assets.get(parts.path)
        if asset is None:
            self._send(404,{'ok':False,'error':'not_found'});return
        file,mime=asset
        try:
            if file.is_symlink():raise OSError('asset_symlink')
            data=file.read_bytes()
            self._send(200,data,mime)
        except OSError:self._send(503,{'ok':False,'error':'asset_unavailable'})

    def do_HEAD(self):self.do_GET()

    def _no_method(self):
        self.close_connection=True
        self._send(405,{'ok':False,'error':'method_not_allowed'})

    do_POST=do_PUT=do_PATCH=do_DELETE=do_OPTIONS=do_TRACE=do_CONNECT=_no_method


def make_server(config:Config, *, store=None, root=HERE, test_port=None):
    # Tests can request an ephemeral loopback port; CLI has no bind-host option.
    server=ThreadingHTTPServer(('127.0.0.1',config.listen_port if test_port is None else test_port),Handler)
    server.daemon_threads=True
    server.mobile_config=config
    server.mobile_store=store if store is not None else SnapshotStore(Upstream(config.upstream_port))
    server.mobile_root=Path(root)
    server.mobile_assets=static_assets(Path(root))
    return server


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enable',action='store_true',help='explicitly start the opt-in loopback reader')
    parser.add_argument('--check',action='store_true',help='validate options without networking or starting a listener')
    parser.add_argument('--owner-login',default='')
    parser.add_argument('--public-origin',default='')
    parser.add_argument('--listen-port',type=int,default=8781)
    parser.add_argument('--upstream-port',type=int,default=8770)
    args=parser.parse_args(argv)
    if not args.enable and not args.check:
        print('Mobile reader is off. No listener started.');return 0
    try:config=Config(args.owner_login,args.public_origin,args.listen_port,args.upstream_port)
    except ValueError as exc:
        parser.error(str(exc))
    if args.check:
        print('Options valid; device policy, Serve and phone access remain unverified. No listener started.');return 0
    server=make_server(config)
    print(f'Mobile reader listening on 127.0.0.1:{config.listen_port}; Serve and device policy are operator-managed.',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
    return 0


if __name__=='__main__':raise SystemExit(main())
