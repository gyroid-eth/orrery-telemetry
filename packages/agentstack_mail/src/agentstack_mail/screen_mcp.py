"""Publish the reviewed Cua Driver tools over stdio."""
from __future__ import annotations

import argparse
import asyncio
import base64
from collections import deque
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
import http.client
import io
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import stat
import sys
from threading import Thread
from urllib.parse import urlsplit
from uuid import uuid4

import anyio
from fastmcp import FastMCP
from fastmcp.tools import Tool
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.server.stdio import stdio_server
from PIL import Image

from . import screen_contract as contract

_SCHEMA_DATA = json.loads(Path(__file__).with_name("screen_schemas.json").read_text())
DRIVER_VERSION = _SCHEMA_DATA["version"]
SCHEMAS = _SCHEMA_DATA["platforms"]
for platform_definitions in SCHEMAS.values():
    for tool_name, tool_definition in platform_definitions.items():
        if tool_name in _SCHEMA_DATA["outputs"]:
            tool_definition["outputSchema"] = _SCHEMA_DATA["outputs"][tool_name]
HIDDEN_ARGUMENTS = {
    "check_permissions": {"probe_direct_capture"},
    "get_window_state": {"screenshot_out_file", "full_output", "max_dimension"},
    "click": {"debug_image_out", "from_zoom"},
    "launch_app": {"additional_arguments", "webkit_inspector_port"},
}
WINDOW_LIMITS = {"max_elements": 250, "max_depth": 25, "max_image_dimension": 1280}
MAX_PIXELS = WINDOW_LIMITS["max_image_dimension"] ** 2
MAX_BYTES = 2 * 1024 * 1024
MAX_TEXT_BYTES = 64 * 1024
MAX_ARGUMENT_BYTES = 64 * 1024
UPSTREAM_TIMEOUT = 15
AUTHORITY_TIMEOUT = 2
TRUNCATED = "応答の一部を上限に合わせて省略しました。"
STATUS_FIELDS = {"project", "runtime", "generation", "computer_use", "source"}
TREE_FIELDS = ("elements", "observations", "tree_markdown")


class ScreenError(RuntimeError):
    pass


class DriverDiagnostics:
    """Drain stderr concurrently and retain enough to classify a failure."""

    def __init__(self):
        read_fd, write_fd = os.pipe()
        self.writer = os.fdopen(write_fd, "wb", buffering=0)
        self.chunks = deque(maxlen=4)

        def drain():
            with os.fdopen(read_fd, "rb", buffering=0) as reader:
                while data := reader.read(2048):
                    self.chunks.append(data)

        self.thread = Thread(target=drain, daemon=True)
        self.thread.start()

    def close(self):
        self.writer.close()
        self.thread.join(timeout=0.25)

    def message(self, phase, error):
        if isinstance(error, FileNotFoundError):
            return "Cua Driver の実行ファイルが見つかりません。"
        if isinstance(error, PermissionError):
            return "Cua Driver の実行ファイルに実行権限がありません。"
        data = b"".join(self.chunks.copy()).lower()
        if b"wsl_interop" in data or b"exec format error" in data:
            return "WSL interop を利用できません。対話セッションの起動経路を確認してください。"
        if b"windows session 0" in data or b"interactive windows user session" in data:
            return "Windows の対話セッションに Cua Driver の daemon が必要です。"
        if b"no process is on the other end of the pipe" in data or b"access is denied" in data:
            return "Windows の名前付きパイプを開けません。daemon と所有者・昇格状態を確認してください。"
        if isinstance(error, TimeoutError):
            return "Cua Driver の応答が時間切れになりました。"
        if phase == "startup":
            return "Cua Driver の stdio を初期化できません。"
        return "Cua Driver の stdio が切断されました。"


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def _text(value):
    return types.TextContent(type="text", text=value)


def _error(message):
    return types.CallToolResult(content=[_text(message)], isError=True)


def driver_platform():
    if sys.platform == "linux":
        if "microsoft" in os.uname().release.lower() or os.environ.get("WSL_DISTRO_NAME"):
            return "windows"
    return sys.platform


def driver_parameters(platform):
    name = "cua-driver.exe" if platform == "windows" else "cua-driver"
    configured = os.environ.get("AGENTSTACK_CUA_DRIVER")
    if configured and not Path(configured).is_absolute():
        raise ScreenError("AGENTSTACK_CUA_DRIVER には実行ファイルの絶対パスが必要です。")
    binary = configured or shutil.which(name)
    if not binary and platform == "darwin":
        app_binary = Path("/Applications/CuaDriver.app/Contents/MacOS/cua-driver")
        if app_binary.is_file():
            binary = str(app_binary)
    if not binary:
        raise ScreenError("Cua Driver が見つかりません。実行ファイルのパスを確認してください。")
    args = ["mcp"]
    if platform == "windows":
        args += ["--socket", r"\\.\pipe\cua-driver"]
    # Only the driver's environment goes upstream. In particular the child
    # authority and either Mail or operator credentials stay in this process.
    keys = ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XAUTHORITY",
            "DBUS_SESSION_BUS_ADDRESS", "XDG_CURRENT_DESKTOP", "WSL_INTEROP", "WSL_DISTRO_NAME")
    env = {key: os.environ[key] for key in keys if key in os.environ}
    opt_outs = {
        "CUA_DRIVER_RS_TELEMETRY_ENABLED": "false",
        "CUA_DRIVER_RS_UPDATE_CHECK": "false",
    }
    env.update(opt_outs)
    if platform == "windows":
        # Export only the opt-outs across WSL interop to the MCP executable.
        env["WSLENV"] = ":".join(f"{key}/w" for key in opt_outs)
    return StdioServerParameters(command=binary, args=args, env=env)


def public_schema(definition, name):
    schema = json.loads(json.dumps(definition["inputSchema"]))
    schema["additionalProperties"] = False
    props = schema["properties"]
    for key in HIDDEN_ARGUMENTS.get(name, ()):
        props.pop(key, None)
    if name == "check_permissions" and "prompt" in props:
        props["prompt"].update(const=False, default=False)
    if name == "get_window_state":
        for key, maximum in WINDOW_LIMITS.items():
            if key in props:
                props[key].update(minimum=1, maximum=maximum, default=maximum)
    return schema


def _result_bytes(result):
    return len(result.model_dump_json(by_alias=True, exclude_none=True).encode())


def _trim_json(value, budget):
    """Keep complete rows, keys and lines; never renumber accessibility rows."""
    if len(json_bytes(value)) <= budget:
        return value
    if isinstance(value, dict):
        kept = {}
        keys = [key for key in value if key not in TREE_FIELDS]
        keys.extend(key for key in TREE_FIELDS if key in value)
        for key in keys:
            item = value[key]
            cost = len(json_bytes(key)) + 2
            remaining = budget - len(json_bytes(kept)) - cost
            if remaining < 2:
                break
            piece = _trim_json(item, remaining)
            if piece is not None:
                kept[key] = piece
        return kept
    if isinstance(value, list):
        kept = []
        remaining = budget - 2
        for item in value:
            remaining -= len(json_bytes(item)) + (1 if kept else 0)
            if remaining < 0:
                break
            kept.append(item)
        return kept
    if isinstance(value, str):
        kept = []
        remaining = budget - 2
        for line in value.splitlines(keepends=True):
            remaining -= len(json_bytes(line)) - 2
            if remaining < 0:
                break
            kept.append(line)
        return "".join(kept)
    return None


def _image_pixels(block, remaining):
    if block.mimeType not in ("image/png", "image/jpeg") or len(block.data) > MAX_BYTES:
        return 0
    try:
        with Image.open(io.BytesIO(base64.b64decode(block.data, validate=True))) as image:
            expected = "PNG" if block.mimeType == "image/png" else "JPEG"
            if image.format != expected or max(image.size) > WINDOW_LIMITS["max_image_dimension"]:
                return 0
            pixels = image.width * image.height
            return pixels if pixels <= remaining else 0
    except (ValueError, OSError, Image.DecompressionBombError):
        return 0


def bound_result(upstream, output_schema=None):
    """Return bounded MCP content while keeping the driver's pixel coordinates."""
    structured = upstream.structuredContent
    result = types.CallToolResult(content=[], isError=upstream.isError)
    pixels = MAX_PIXELS
    truncated = getattr(upstream, "meta", None) is not None
    if structured is not None:
        # The raster is only carried in MCP image blocks.
        filtered = {key: value for key, value in structured.items()
                    if key not in ("screenshot", "screenshot_base64", "image", "image_base64")}
        trimmed = _trim_json(filtered, MAX_TEXT_BYTES)
        truncated |= trimmed != structured
        if output_schema and not upstream.isError:
            validator = Draft202012Validator(output_schema)
            if not validator.is_valid(trimmed):
                trimmed = {"status": "truncated"}
                truncated = True
        result.structuredContent = trimmed
    remaining_text = MAX_TEXT_BYTES - len(json_bytes(result.structuredContent))
    for block in upstream.content:
        if isinstance(block, types.TextContent):
            text = block.text
            size = len(json_bytes(text)) - 2
            if size > remaining_text:
                try:
                    value = json.loads(text)
                except ValueError:
                    value = text
                if structured is not None and value == structured:
                    truncated = True
                    continue
                piece = _trim_json(value, remaining_text)
                text = piece if isinstance(value, str) else json_bytes(piece).decode()
                size = len(json_bytes(text)) - 2 if text else 0
                truncated = True
            if text and 0 < size <= remaining_text:
                result.content.append(_text(text))
                remaining_text -= size
        elif isinstance(block, types.ImageContent):
            count = _image_pixels(block, pixels)
            if count and _result_bytes(result) + len(block.data) + 1024 < MAX_BYTES:
                result.content.append(types.ImageContent(
                    type="image", data=block.data, mimeType=block.mimeType,
                ))
                pixels -= count
            else:
                truncated = True
        else:
            truncated = True
    if truncated:
        result.content.append(_text(TRUNCATED))
    if _result_bytes(result) > MAX_BYTES:
        return types.CallToolResult(
            content=[_text(TRUNCATED)], isError=result.isError,
            structuredContent={"status": "truncated"} if structured is not None else None,
        )
    return result


class ChildAuthority:
    def __init__(self, origin, token, identity):
        url = urlsplit(origin)
        if (url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port
                or url.username or url.path or url.query or url.fragment):
            raise ScreenError("computer use 照会先の設定が無効です。")
        self.port = url.port
        self.token = token
        self.identity = identity

    def _request(self, method, path, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=AUTHORITY_TIMEOUT)
        try:
            conn.request(method, path, json_bytes(body) if body is not None else None, {
                "Authorization": "Bearer " + self.token, "Content-Type": "application/json",
            })
            response = conn.getresponse()
            if response.status != 200:
                raise ValueError
            raw = response.read(8193)
            if len(raw) > 8192:
                raise ValueError
            response = json.loads(raw)
            if not isinstance(response, dict):
                raise ValueError
            return response
        finally:
            conn.close()

    async def request(self, method, path, body=None):
        try:
            with anyio.fail_after(AUTHORITY_TIMEOUT):
                return await anyio.to_thread.run_sync(
                    self._request, method, path, body, abandon_on_cancel=True,
                )
        except (OSError, TimeoutError, ValueError, http.client.HTTPException):
            raise ScreenError("computer use の権限を照会できません。") from None

    async def status(self):
        status = await self.request("GET", contract.CHILD_PATH)
        if (set(status) != STATUS_FIELDS or any(status.get(key) != val for key, val in self.identity.items())
                or status["computer_use"] not in contract.MODES):
            raise ScreenError("computer use の権限または起動世代を確認できません。")
        return status

    async def event(self, call_id, tool, phase, result=None):
        await self.request("POST", contract.EVENT_PATH, {
            "call_id": call_id, "tool": tool, "phase": phase, "result": result,
        })


@dataclass
class Call:
    name: str
    arguments: dict
    reply: asyncio.Future


class Relay:
    def __init__(self, definitions, authority, parameters, *, timeout=UPSTREAM_TIMEOUT):
        self.definitions = definitions
        self.authority = authority
        self.parameters = parameters
        self.timeout = timeout
        self.calls = asyncio.Queue()
        self.validators = {name: Draft202012Validator(public_schema(definition, name))
                           for name, definition in definitions.items()}
        self.session = None
        self.failure = None
        self.diagnostics = None

    async def call(self, name, arguments):
        job = Call(name, arguments, asyncio.get_running_loop().create_future())
        await self.calls.put(job)
        return await job.reply

    async def run(self):
        async with AsyncExitStack() as stack:
            while True:
                job = await self.calls.get()
                if job.reply.cancelled():
                    continue
                result = await self._dispatch(job, stack)
                if not job.reply.done():
                    job.reply.set_result(result)

    async def _open(self, stack):
        self.diagnostics = DriverDiagnostics()
        stack.callback(self.diagnostics.close)
        # Keep raw upstream stderr out of both the harness and the audit stream.
        read, write = await stack.enter_async_context(
            stdio_client(self.parameters(), errlog=self.diagnostics.writer),
        )
        self.diagnostics.writer.close()
        session = await stack.enter_async_context(ClientSession(read, write))
        with anyio.fail_after(self.timeout):
            info = (await session.initialize()).serverInfo
            if (info.name, info.version) != ("cua-driver", DRIVER_VERSION):
                raise ScreenError("Cua Driver の版が承認済みの版と一致しません。")
            tools = {}
            cursor = None
            for _ in range(8):
                listed = await session.list_tools(cursor)
                for tool in listed.tools:
                    if tool.name in tools:
                        raise ScreenError("Cua Driver の道具の定義が重複しています。")
                    tools[tool.name] = tool
                cursor = listed.nextCursor
                if cursor is None:
                    break
            else:
                raise ScreenError("Cua Driver の道具の一覧を確認できません。")
        for name, definition in self.definitions.items():
            tool = tools.get(name)
            if (tool is None or tool.inputSchema != definition["inputSchema"]
                    or tool.outputSchema != definition.get("outputSchema")):
                raise ScreenError("Cua Driver の道具の定義が承認済みの定義と一致しません。")
        self.session = session

    async def _stop(self, stack, message):
        self.failure = message
        self.session = None
        try:
            await stack.aclose()
        except Exception:
            pass

    @staticmethod
    def _check_mode(name, status):
        mode = status["computer_use"]
        if mode == "off":
            raise ScreenError("この agent の computer use は Off です。")
        if mode == "view" and name in contract.CONTROL_TOOLS:
            raise ScreenError("この agent の computer use は View です。操作には人が Control を選ぶ必要があります。")

    async def _dispatch(self, job, stack):
        name = job.name
        tool = name if name in self.definitions else "unknown"
        call_id = uuid4().hex
        outcome = "denied"
        try:
            await self.authority.event(call_id, tool, "start")
        except ScreenError as exc:
            return _error(str(exc))
        try:
            if tool == "unknown":
                return _error("この道具は orrery-screen の一覧にありません。")
            try:
                valid = (len(json_bytes(job.arguments)) <= MAX_ARGUMENT_BYTES
                         and self.validators[name].is_valid(job.arguments))
            except (ValueError, OverflowError, RecursionError):
                valid = False
            if not valid:
                return _error("道具の引数が承認済みの定義または量の上限に合いません。")
            self._check_mode(name, await self.authority.status())
            if self.failure:
                outcome = "upstream_error"
                return _error(self.failure)
            if self.session is None:
                try:
                    await self._open(stack)
                except Exception as exc:
                    message = (
                        str(exc) if isinstance(exc, ScreenError) else
                        self.diagnostics.message("startup", exc) if self.diagnostics else
                        "Cua Driver の stdio を初期化できません。"
                    )
                    await self._stop(stack, message)
                    outcome = "upstream_error"
                    return _error(self.failure)
                # Startup can wait on the driver. Read the authority again
                # after it, immediately before a call can reach the driver.
                self._check_mode(name, await self.authority.status())
            args = dict(job.arguments)
            if name == "get_window_state":
                for key, limit in WINDOW_LIMITS.items():
                    if key in self.validators[name].schema["properties"]:
                        args.setdefault(key, limit)
                if "full_output" in self.definitions[name]["inputSchema"]["properties"]:
                    args["full_output"] = False
            try:
                outcome = "upstream_error"
                with anyio.fail_after(self.timeout):
                    response = await self.session.call_tool(name, args)
            except Exception as exc:
                outcome = "upstream_error"
                await self._stop(stack, self.diagnostics.message("call", exc))
                return _error(self.failure)
            effect = (response.structuredContent or {}).get("effect")
            outcome = ("upstream_error" if response.isError
                       or effect in ("refused", "partial", "suspected_noop", "unverifiable") else "ok")
            try:
                return bound_result(response, self.definitions[name].get("outputSchema"))
            except (ValueError, TypeError, OverflowError, RecursionError):
                outcome = "upstream_error"
                return _error("Cua Driver の応答を安全に読み取れません。")
        except ScreenError as exc:
            return _error(str(exc))
        finally:
            try:
                await self.authority.event(call_id, tool, "finish", outcome)
            except ScreenError:
                logging.getLogger(__name__).warning("computer use の終了を記録できません。")


class ScreenServer(FastMCP):
    def __init__(self, relay):
        self.relay = relay

        @asynccontextmanager
        async def lifespan(_server):
            worker = asyncio.create_task(relay.run())
            try:
                yield {}
            finally:
                worker.cancel()
                with anyio.CancelScope(shield=True):
                    try:
                        await worker
                    except asyncio.CancelledError:
                        pass

        super().__init__(contract.SERVER, lifespan=lifespan, strict_input_validation=False)
        for name in contract.TOOLS:
            definition = relay.definitions[name]
            self.add_tool(Tool(
                name=name, description=definition["description"],
                parameters=relay.validators[name].schema,
                output_schema=definition.get("outputSchema"),
                annotations=types.ToolAnnotations.model_validate(definition.get("annotations", {})),
            ))

    async def _call_tool_mcp(self, key, arguments):
        return await self.relay.call(key, arguments)

    async def run_stdio_async(self, show_banner=False, log_level=None):
        # The SDK's default threaded stdin can keep SIGTERM waiting on a
        # harness that has stopped reading but still holds the pipe open.
        reader = asyncio.StreamReader(limit=2 * MAX_ARGUMENT_BYTES)
        protocol = asyncio.StreamReaderProtocol(reader)
        with os.fdopen(os.dup(sys.stdin.fileno()), "rb") as pipe:
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, pipe)
            try:
                async with self._lifespan_manager():
                    async with stdio_server(stdin=reader) as (read, write):
                        await self._mcp_server.run(read, write, self._mcp_server.create_initialization_options())
            finally:
                transport.close()


async def _run(server):
    loop = asyncio.get_running_loop()
    current = asyncio.current_task()
    for signum in (signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(signum, current.cancel)
    try:
        await server.run_async(transport="stdio", show_banner=False)
    except asyncio.CancelledError:
        pass
    finally:
        for signum in (signal.SIGTERM, signal.SIGHUP):
            loop.remove_signal_handler(signum)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="print the bound permission and generation")
    args = parser.parse_args()
    try:
        fd = os.open(os.environ["AGENTSTACK_SCREEN_BINDING_FILE"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ScreenError("orrery-screen の起動設定は非公開の通常ファイルにしてください。")
            binding = json.loads(file.read(8193))
        authority = ChildAuthority(binding["origin"], binding["credential"], {
            key: binding[key] for key in ("project", "runtime", "generation")
        })
        if args.status:
            print(json_bytes(asyncio.run(authority.status())).decode())
            return 0
        platform = driver_platform()
        definitions = SCHEMAS.get(platform)
        if not definitions:
            raise ScreenError("この OS の Cua Driver の定義は承認されていません。")
        # Parser and SDK diagnostic logs can contain untrusted protocol input.
        logging.getLogger("mcp.client.stdio").setLevel(logging.CRITICAL)
        server = ScreenServer(Relay(definitions, authority, lambda: driver_parameters(platform)))
        asyncio.run(_run(server))
        return 0
    except (KeyError, OSError, TypeError, ValueError):
        print("orrery-screen の起動設定が無効です。", file=sys.stderr)
    except ScreenError as exc:
        print(exc, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
