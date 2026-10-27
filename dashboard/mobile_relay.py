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
from urllib.parse import parse_qs, urlsplit, urlencode

try:
    from dashboard import legacy_http_admission as admission
    from dashboard.mobile_projection import SnapshotStore, MobileMailStore, WINDOWS, NAME, EVENT_KEY, png_image
    from dashboard import mobile_input
except ModuleNotFoundError:
    import legacy_http_admission as admission
    from mobile_projection import SnapshotStore, MobileMailStore, WINDOWS, NAME, EVENT_KEY, png_image
    import mobile_input

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
    input_enabled: bool = False
    phone_policy_reviewed: bool = False
    cockpit_port: int = 8791

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
        if (type(self.input_enabled) is not bool or type(self.phone_policy_reviewed) is not bool
                or self.input_enabled and not self.phone_policy_reviewed):
            raise ValueError('input requires explicit opt-in and --phone-policy-reviewed acknowledgement')
        if (type(self.cockpit_port) is not int or not 1<=self.cockpit_port<=65535
                or self.input_enabled and self.cockpit_port in {self.listen_port,self.upstream_port}):
            raise ValueError('valid separate cockpit adapter port is required')


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
        return json.loads(self._read(path,'application/json',MAX_UPSTREAM_BYTES))

    def portrait(self,name):
        if not isinstance(name,str) or not NAME.fullmatch(name):raise ValueError('invalid_name')
        data=self._read('/api/mobile-portrait?name='+name,'image/png',512*1024)
        if not png_image(data):raise ValueError('portrait_format')
        return data

    def message(self,window,key,mode):
        if window not in WINDOWS or not EVENT_KEY.fullmatch(key) or mode not in {'content','revision'}:raise ValueError('message_query')
        return json.loads(self._read('/api/mobile-message?'+urlencode({'window':window,'event':key,'mode':mode}),'application/json',MAX_UPSTREAM_BYTES,expired=True))

    def mail_page(self,window,cursor,*,pair=None,direction='both'):
        query={'window':window}
        if cursor is not None:query['cursor']=cursor
        if pair is not None:query.update(a=pair[0],b=pair[1],direction=direction)
        return json.loads(self._read('/api/mobile-mail-page?'+urlencode(query),'application/json',MAX_UPSTREAM_BYTES,expired=True))

    def cache_page(self,cursor):
        query='' if cursor is None else '?'+urlencode({'cursor':cursor})
        return json.loads(self._read('/api/mobile-cache-page'+query,'application/json',MAX_UPSTREAM_BYTES))

    def _read(self,path,mime,limit,*,expired=False):
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
                                            'Accept':mime,'Connection':'close'})
            response = con.getresponse()
            if expired and response.status==410:raise LookupError('upstream_event_or_cursor_expired')
            if response.status != 200 or response.getheader('Content-Type','').split(';')[0].strip().lower() != mime:
                raise ValueError('upstream_unavailable')
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise ValueError('upstream_encoding')
            length = response.getheader('Content-Length')
            if length is not None and (not length.isdigit() or int(length)>limit):
                raise ValueError('upstream_size')
            data=response.read(limit+1)
            if len(data)>limit:raise ValueError('upstream_size')
            return data
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

    def _admitted(self,*,write=False):
        cfg=self.server.mobile_config
        hosts=self.headers.get_all('Host',[])
        users=self.headers.get_all('Tailscale-User-Login',[])
        origins=self.headers.get_all('Origin',[])
        try:
            expected=admission.origin(cfg.public_origin)
            if len(hosts)!=1 or len(users)!=1 or users[0]!=cfg.owner_login or len(origins)>1:
                return False
            if write and len(origins)!=1:return False
            host,port=admission.authority(hosts[0])
            if (host,port if port is not None else 443)!=expected[1:]:return False
            if origins and admission.origin(origins[0])!=expected:return False
        except (ValueError,TypeError):
            return False
        if self.headers.get_all('Authority',[]):return False
        words = self.requestline.split()
        target = words[1] if len(words) == 3 else ''
        site=self.headers.get_all('Sec-Fetch-Site',[])
        mode=self.headers.get_all('Sec-Fetch-Mode',[])
        dest=self.headers.get_all('Sec-Fetch-Dest',[])
        if any(len(values)>1 for values in (site,mode,dest)):return False
        if (site[0] if site else '').lower() not in {'','none','same-origin'}:
            # Android home shortcuts can start an authenticated top-level
            # document navigation cross-site. This admits only the root shell,
            # never its data/assets, and still passes every other guard below.
            if not (self.command in {'GET','HEAD'} and target==self.path=='/'
                    and site==['cross-site'] and mode==['navigate'] and dest==['document']):
                return False
        if any(self.headers.get_all(h,[]) for h in ('Upgrade','Transfer-Encoding','X-HTTP-Method-Override','X-Method-Override','X-HTTP-Method')):return False
        lengths=self.headers.get_all('Content-Length',[])
        if write:
            if (len(lengths)!=1 or re.fullmatch(r'[0-9]{1,5}',lengths[0]) is None or not 0<int(lengths[0])<=mobile_input.MAX_JSON_BYTES
                    or self.headers.get_all('Content-Type',[])!=['application/json']
                    or any(self.headers.get_all(h,[]) for h in ('Content-Encoding','Expect'))):return False
        elif len(lengths)>1 or (lengths and lengths[0]!='0'):return False
        return target == self.path and target.startswith('/') and not target.startswith('//') and '#' not in target and '%' not in target and '\\' not in target

    def do_GET(self):
        if not self._admitted():
            self._send(403,{'ok':False,'error':'request_denied'});return
        parts=urlsplit(self.path)
        if self.path=='/view/terminal':
            if self.command=='HEAD':self._send(200,b'');return
            if not self.server.mobile_config.input_enabled:
                self._send(200,{'contract':mobile_input.CONTRACT,'enabled':False});return
            try:self._send(200,self.server.mobile_input.call('capability'))
            except Exception:self._send(503,{'error':'input_unavailable','enabled':False})
            return
        if parts.path=='/view/snapshot':
            query=parse_qs(parts.query,keep_blank_values=True)
            if set(query)-{'window'} or len(query.get('window',[]))>1 or query.get('window',['day'])[0] not in WINDOWS:
                self._send(400,{'ok':False,'error':'invalid_window'});return
            if self.command=='HEAD':self._send(200,b'');return
            payload=self.server.mobile_store.snapshot(query.get('window',['day'])[0])
            self.server.mobile_mail.observe(payload)
            if payload.get('sources',{}).get('mail',{}).get('status')=='available':self.server.mobile_mail.start_warm()
            payload['mail_cache']=self.server.mobile_mail.progress()
            unavailable=all(x['status']=='unavailable' for x in payload['sources'].values())
            self._send(503 if unavailable else 200,payload);return
        if parts.path in {'/view/message','/view/mail'}:
            query=parse_qs(parts.query,keep_blank_values=True)
            window=query.get('window',['day'])[0]
            allowed={'window','event'} if parts.path=='/view/message' else {'window','cursor','a','b','direction'}
            if set(query)-allowed or any(len(v)!=1 for v in query.values()) or window not in WINDOWS:
                self._send(400,{'error':'invalid_mail_query'});return
            key=query.get('event',[''])[0];cursor=query.get('cursor',[None])[0]
            pair=None;direction=query.get('direction',['both'])[0]
            if parts.path=='/view/message':
                valid=bool(EVENT_KEY.fullmatch(key))
            else:
                pair_fields=set(query)&{'a','b','direction'}
                if pair_fields:
                    pair=(query.get('a',[''])[0],query.get('b',[''])[0])
                valid=(cursor is None or bool(EVENT_KEY.fullmatch(cursor))) and (
                    pair is not None and pair_fields=={'a','b','direction'} and all(NAME.fullmatch(n) for n in pair) and
                    direction in {'both','forward','reverse'} and (pair[0]!=pair[1] or direction=='both') or
                    pair is None and cursor is not None)
            if not valid:self._send(400,{'error':'invalid_mail_query'});return
            if pair and not all(self.server.mobile_store.known_agent(window,n) for n in pair):
                self._send(404,{'error':'pair_unavailable'});return
            if self.command=='HEAD':self._send(200,b'');return
            try:
                if parts.path=='/view/message':payload=self.server.mobile_mail.message(window,key)
                elif pair is None:payload=self.server.mobile_mail.page(window,cursor)
                else:payload=self.server.mobile_mail.page(window,cursor,pair=pair,direction=direction)
                self._send(200,payload)
            except LookupError:self._send(410,{'error':'mail_event_or_cursor_expired'})
            except (OSError,ValueError,TypeError):self._send(503,{'error':'mail_unavailable'})
            return
        if parts.path=='/view/portrait':
            query=parse_qs(parts.query,keep_blank_values=True)
            window=query.get('window',['day'])[0];nm=query.get('name',[''])[0]
            if set(query)-{'name','window'} or any(len(v)!=1 for v in query.values()) or window not in WINDOWS or not NAME.fullmatch(nm):
                self._send(400,{'ok':False,'error':'invalid_portrait'});return
            if not self.server.mobile_store.known_agent(window,nm):
                self._send(404,{'ok':False,'error':'portrait_unavailable'});return
            if self.command=='HEAD':self._send(200,b'','image/png');return
            try:self._send(200,Upstream(self.server.mobile_config.upstream_port).portrait(nm),'image/png')
            except (OSError,ValueError):self._send(503,{'ok':False,'error':'portrait_unavailable'})
            return
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

    def do_POST(self):
        operation=self.path.removeprefix('/view/terminal/')
        if (not self.server.mobile_config.input_enabled or not self.path.startswith('/view/terminal/')
                or operation not in mobile_input.OPERATIONS):
            self._no_method();return
        self.close_connection=True
        if not self._admitted(write=True):
            self._send(403,{'error':'input_request_denied'});return
        timer=None
        try:
            def expire():
                try:self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:pass
            timer=threading.Timer(5,expire);timer.daemon=True;timer.start()
            size=int(self.headers['Content-Length']);data=self.rfile.read(size)
            if len(data)!=size:raise ValueError('input_json_invalid')
            payload=mobile_input.decode_json(data)
            if not mobile_input.request_fields(operation,payload):raise ValueError('input_json_invalid')
        except (ValueError,OSError):
            self._send(400,{'error':'input_request_invalid'});return
        finally:
            if timer is not None:timer.cancel()
        try:self._send(200,self.server.mobile_input.call(operation,payload))
        except Exception:
            # A lost response cannot establish whether a commit reached tmux.
            # The UI retains only the operation ID, offers status, and never
            # repeats the send automatically.
            self._send(503,{'error':'input_unavailable','state':'unknown' if operation=='commit' else 'unavailable'})

    def _no_method(self):
        self.close_connection=True
        self._send(405,{'ok':False,'error':'method_not_allowed'})

    do_PUT=do_PATCH=do_DELETE=do_OPTIONS=do_TRACE=do_CONNECT=_no_method


class ReaderServer(ThreadingHTTPServer):
    def server_close(self):
        if hasattr(self,'mobile_mail'):self.mobile_mail.close()
        super().server_close()


def make_server(config:Config, *, store=None, mail_store=None, input_client=None, root=HERE, test_port=None):
    # Tests can request an ephemeral loopback port; CLI has no bind-host option.
    server=ReaderServer(('127.0.0.1',config.listen_port if test_port is None else test_port),Handler)
    server.daemon_threads=True
    server.mobile_config=config
    server.mobile_store=store if store is not None else SnapshotStore(Upstream(config.upstream_port))
    server.mobile_mail=mail_store if mail_store is not None else MobileMailStore(Upstream(config.upstream_port))
    server.mobile_root=Path(root)
    server.mobile_assets=static_assets(Path(root))
    server.mobile_input=(input_client if input_client is not None else
                         mobile_input.InputClient(config.cockpit_port,config.owner_login) if config.input_enabled else None)
    return server


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enable',action='store_true',help='explicitly start the opt-in loopback reader')
    parser.add_argument('--check',action='store_true',help='validate options without networking or starting a listener')
    parser.add_argument('--owner-login',default='')
    parser.add_argument('--public-origin',default='')
    parser.add_argument('--listen-port',type=int,default=8781)
    parser.add_argument('--upstream-port',type=int,default=8770)
    parser.add_argument('--enable-terminal',action='store_true',help='opt in to confirmed input; viewing alone does not enable it')
    parser.add_argument('--phone-policy-reviewed',action='store_true',
                        help='operator acknowledgement of the access policy; does not verify it or enforce a single phone')
    parser.add_argument('--cockpit-port',type=int,default=8791)
    args=parser.parse_args(argv)
    if args.enable_terminal and not (args.enable or args.check):parser.error('--enable-terminal also requires --enable or --check')
    if not args.enable and not args.check:
        print('Mobile reader is off. No listener started.');return 0
    try:config=Config(args.owner_login,args.public_origin,args.listen_port,args.upstream_port,
                      args.enable_terminal,args.phone_policy_reviewed,args.cockpit_port)
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
