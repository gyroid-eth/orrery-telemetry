"""Private same-host target inspection in the existing fresh dashboard."""
import asyncio
import fcntl
import json
import os
from pathlib import Path
import re
import runpy
import secrets
import stat
import threading

from agentstack_mail.fresh_runtime import FreshRuntime
from agentstack_mail.fresh_targets import SELECTORS
from agentstack_mail.storage_origin_contract import B1Error


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


class Host:
    def __init__(self, backend):
        self.backend = backend
        self.api = runpy.run_path(str(backend.root / 'bin/lib/runtime_tmux.py'))
        self.path = self.api['fresh_host_path'](backend.client.path)
        self.epoch = secrets.token_hex(32)
        self.loop = None
        self.server = None
        self.runtime = None
        self.lock = None
        self.inode = None
        self.staging = None
        self.ready = threading.Event()
        self.failure = None
        self.thread = None

    def start(self):
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        info = self.path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError('FRESH_HOST_UNSAFE')
        if len(os.fsencode(self.path)) >= 100:
            raise RuntimeError('FRESH_HOST_PATH_TOO_LONG')
        fd = os.open(self.path.parent / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.lock = os.fdopen(fd, 'r+')
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            self.lock.close(); self.lock = None
            raise RuntimeError('FRESH_HOST_UNSAFE')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists() or self.path.is_symlink():
                info = self.path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise RuntimeError('FRESH_HOST_UNSAFE')
                self.path.unlink()
            self.thread = threading.Thread(target=self._thread, name='fresh-host', daemon=True)
            self.thread.start()
            if not self.ready.wait(10) or self.failure is not None:
                raise RuntimeError('FRESH_HOST_UNAVAILABLE')
        except BaseException:
            self.close()
            raise

    def _thread(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._start())
            self.ready.set()
            self.loop.run_forever()
        except BaseException as exc:
            self.failure = exc
            self.ready.set()
        finally:
            self.loop.run_until_complete(self._stop())
            self.loop.close()

    async def _start(self):
        self.runtime = await FreshRuntime.open(Path(self.backend.client.context['root']) / 'config.json')
        self.targets = self.backend.host_targets(self.runtime)
        self.limit = asyncio.Semaphore(8)
        staging = self.path.parent / ('.%s.sock' % os.getpid())
        if staging.exists() or staging.is_symlink():
            raise RuntimeError('FRESH_HOST_UNSAFE')
        self.server = await asyncio.start_unix_server(self._handle, path=str(staging), limit=16385)
        info = staging.lstat()
        self.staging = (staging, (info.st_dev, info.st_ino))
        os.chmod(staging, 0o600)
        os.rename(staging, self.path)
        self.staging = None
        info = self.path.lstat()
        self.inode = (info.st_dev, info.st_ino)

    async def _stop(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await self.loop.shutdown_default_executor()
        if self.runtime is not None:
            await self.runtime.close()
            self.runtime = None
        try:
            info = self.path.lstat()
            if self.inode == (info.st_dev, info.st_ino):
                self.path.unlink()
        except FileNotFoundError:
            pass
        if self.staging is not None:
            path, identity = self.staging
            try:
                info = path.lstat()
                if identity == (info.st_dev, info.st_ino):path.unlink()
            except FileNotFoundError:
                pass

    def close(self):
        if self.loop is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread is not None:
            self.thread.join(10)
            if self.thread.is_alive():
                raise RuntimeError('FRESH_HOST_STOP_TIMEOUT')
            self.thread = None
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    async def _dispatch(self, request):
        if not isinstance(request, dict) or set(request) - {'operation', 'session', 'mail_instance_id', 'mail_agent_id', 'purpose', 'expected', 'pin'}:
            raise B1Error('FRESH_TARGET_QUERY_INVALID')
        operation = request.get('operation')
        if operation not in {'hello', 'catalog', 'inspect', 'membership'}:
            raise B1Error('FRESH_TARGET_OPERATION_UNAVAILABLE')
        allowed = {'operation'} if operation == 'hello' else {'operation', 'pin'}
        if operation in {'inspect','membership'}:
            allowed |= SELECTORS | {'purpose', 'expected'}
        if set(request) - allowed:
            raise B1Error('FRESH_TARGET_QUERY_INVALID')
        context, proof = self.backend.client._load()
        pin = dict(contract='orrery-fresh-host-v1', mail_instance_id=context['mail_instance_id'],
                   operator_id=context['mail_agent_id'], generation=context['credential_generation'],
                   context_digest=self.backend.client.context_digest, epoch=self.epoch,
                   wrapper_root=str(self.backend.root))
        if operation != 'hello' and request.get('pin') != pin:
            raise B1Error('FRESH_HOST_CHANGED')
        if operation == 'hello':
            async with self.runtime.core.owner_transaction(context['mail_instance_id'], proof):
                pass
            result = {'ok': True}
        elif operation == 'catalog':
            result = await self.targets.catalog()
        elif operation == 'inspect':
            query = {key: request[key] for key in SELECTORS if key in request}
            result = await self.targets.inspect(query, purpose=request.get('purpose', 'attach'), expected=request.get('expected'))
        else:
            if 'purpose' in request:raise B1Error('FRESH_TARGET_QUERY_INVALID')
            query = {key: request[key] for key in SELECTORS if key in request}
            result = await self.targets.membership(query,request.get('expected'))
        return {'pin': pin, 'result': result}

    async def _handle(self, reader, writer):
        try:
            if self.limit.locked():
                raise B1Error('FRESH_HOST_BUSY')
            async with self.limit:
                raw = await asyncio.wait_for(reader.readline(), 7)
                if not raw.endswith(b'\n') or len(raw) > 16384:
                    raise ValueError('invalid request')
                request = json.loads(raw, object_pairs_hook=_object)
                reply = await asyncio.wait_for(self._dispatch(request), 7)
                raw = (json.dumps(reply, separators=(',', ':'), allow_nan=False) + '\n').encode()
                if len(raw) > 262144:
                    raise ValueError('invalid response')
                writer.write(raw)
                await writer.drain()
        except Exception as exc:
            code = exc.code if isinstance(exc, B1Error) else None
            if not isinstance(code, str) or re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', code) is None:
                code = 'FRESH_HOST_UNAVAILABLE'
            try:
                writer.write((json.dumps({'error': code}) + '\n').encode())
                await writer.drain()
            except (OSError, ConnectionError):
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass
