"""Synchronous HTTPX policy for the pinned Hermes OpenAI/Anthropic routes.

No request headers, bodies, credential values or remote error strings are saved.
This is a Python transport boundary, not an operating-system process sandbox.
"""
from contextvars import ContextVar
import json
from pathlib import Path
import sys
import threading
from urllib.parse import urlsplit

from mail_lifecycle import read
from task_evidence import audit

_permitted = ContextVar('orrery_http_transport', default=False)


class Policy:
    def __init__(self, state, limit=32):
        self.state = Path(state)
        config = read(self.state / 'run-config.json')
        profile = read(Path(config['profile_home']) / 'config.yaml')
        route = profile['providers'][config['provider']]
        base = urlsplit(route['base_url'])
        path = base.path.rstrip('/')
        # Hermes strips trailing /v1 for Anthropic; the SDK appends /v1/messages.
        path = path.removesuffix('/v1') + '/v1/messages' if route['api_mode'] == 'anthropic_messages' else path + '/chat/completions'
        self.endpoint = (base.scheme, base.netloc, path)
        self.limit, self.seq, self.lock = limit, 0, threading.Lock()

    def before(self, request):
        url = urlsplit(str(request.url))
        if (request.method != 'POST' or (url.scheme, url.netloc, url.path) != self.endpoint
                or url.query or url.fragment):
            audit(self.state, 'blocked-network', reason='endpoint-not-allowed')
            raise RuntimeError('Managed provider endpoint denied')
        with self.lock:
            self.seq += 1
            seq = self.seq
        if seq > self.limit:
            audit(self.state, 'limit-reached', limit=self.limit)
            raise RuntimeError('Managed provider request limit reached')
        audit(self.state, 'provider-request-start', seq=seq)
        return seq

    def response(self, seq, status):
        audit(self.state, 'provider-response', seq=seq, status=status)


def install(state):
    import httpx
    policy = Policy(state)
    original_sync = httpx.Client._send_single_request
    original_async = httpx.AsyncClient._send_single_request

    def send(client, request):
        seq = policy.before(request)
        token = _permitted.set(True)
        try:
            result = original_sync(client, request)
            policy.response(seq, result.status_code)
            return result
        finally:
            _permitted.reset(token)

    async def asend(client, request):
        seq = policy.before(request)
        token = _permitted.set(True)
        try:
            result = await original_async(client, request)
            policy.response(seq, result.status_code)
            return result
        finally:
            _permitted.reset(token)

    httpx.Client._send_single_request = send
    httpx.AsyncClient._send_single_request = asend

    def guard(event, args):
        if event in ('socket.connect', 'socket.getaddrinfo') and not _permitted.get():
            audit(state, 'blocked-network', reason='outside-httpx-provider-transport')
            raise RuntimeError('Unmanaged Python network operation denied')
    sys.addaudithook(guard)
    audit(state, 'preload-ready', backend_id=read(Path(state) / 'run-config.json')['provider'])
    return policy
