"""Synchronous JSON-RPC client for the experimental Codex app-server.

The adapter intentionally exposes only the methods required by the integration.
It starts ``codex app-server`` as an argv array, exchanges newline-delimited
JSON over stdio, initializes the connection, and retains interleaved server
notifications for callers to inspect.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TextIO


JsonObject = dict[str, Any]
ProcessFactory = Callable[..., subprocess.Popen[str]]
_STREAM_CLOSED = object()


class AppServerError(RuntimeError):
    """Base error raised for app-server transport or JSON-RPC failures."""


class AppServerTimeout(AppServerError):
    """Raised when a JSON-RPC response does not arrive before the deadline."""


class AppServerClosed(AppServerError):
    """Raised when app-server closes stdout before returning a response."""


class AppServerClient:
    """Small, synchronous app-server v2 client over newline-delimited stdio."""

    def __init__(
        self,
        command: Sequence[str] = ("codex", "app-server"),
        *,
        timeout: float = 30.0,
        client_name: str = "agentstack-codex-app",
        client_title: str = "AgentStack Codex App Bridge",
        client_version: str = "0.1.0",
        process_factory: ProcessFactory = subprocess.Popen,
    ) -> None:
        if not command:
            raise ValueError("app-server command cannot be empty")
        self.command = tuple(command)
        self.timeout = timeout
        self.client_info = {
            "name": client_name,
            "title": client_title,
            "version": client_version,
        }
        self._process_factory = process_factory
        self._process: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[JsonObject | object] = queue.Queue()
        self._notifications: list[JsonObject] = []
        self._request_id = 0
        self._write_lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None

    @property
    def notifications(self) -> tuple[JsonObject, ...]:
        """Return notifications and unmatched server messages seen so far."""

        return tuple(self._notifications)

    def drain_notifications(self) -> list[JsonObject]:
        """Return and clear accumulated server notifications."""

        notifications = self._notifications[:]
        self._notifications.clear()
        return notifications

    def start(self) -> "AppServerClient":
        """Start app-server and complete the JSON-RPC initialize handshake."""

        if self._process is not None:
            return self
        process = self._process_factory(
            list(self.command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        if process.stdin is None or process.stdout is None:
            process.terminate()
            raise AppServerError("app-server process did not expose stdio pipes")
        self._process = process
        self._reader_thread = threading.Thread(
            target=self._read_stdout,
            args=(process.stdout,),
            name="agentstack-app-server-reader",
            daemon=True,
        )
        self._reader_thread.start()
        try:
            self._request("initialize", {"clientInfo": self.client_info})
            self._notify("initialized")
        except Exception:
            self.close()
            raise
        return self

    def close(self) -> None:
        """Terminate the child process without invoking a shell."""

        process, self._process = self._process, None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def __enter__(self) -> "AppServerClient":
        return self.start()

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> JsonObject:
        """Issue an initialized JSON-RPC request and return its result object."""

        self.start()
        return self._request(method, params, timeout=timeout)

    def thread_list(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
        sort_key: str = "updated_at",
        sort_direction: str = "desc",
        use_state_db_only: bool = False,
    ) -> JsonObject:
        """Call ``thread/list`` using protocol snake_case enum values."""

        if sort_key not in {"created_at", "updated_at", "recency_at"}:
            raise ValueError(f"invalid thread sort key: {sort_key}")
        if sort_direction not in {"asc", "desc"}:
            raise ValueError(f"invalid sort direction: {sort_direction}")
        params: JsonObject = {
            "limit": limit,
            "sortKey": sort_key,
            "sortDirection": sort_direction,
            "useStateDbOnly": use_state_db_only,
        }
        if cursor is not None:
            params["cursor"] = cursor
        return self.request("thread/list", params)

    def thread_start(
        self,
        *,
        cwd: str | None = None,
        model: str | None = None,
        sandbox: str | None = None,
        approval_policy: str | None = None,
        developer_instructions: str | None = None,
    ) -> JsonObject:
        """Create a new app-server thread."""

        params = _without_none(
            {
                "cwd": cwd,
                "model": model,
                "sandbox": sandbox,
                "approvalPolicy": approval_policy,
                "developerInstructions": developer_instructions,
            }
        )
        return self.request("thread/start", params)

    def turn_start(
        self,
        thread_id: str,
        input_items: str | Sequence[Mapping[str, Any]],
        *,
        cwd: str | None = None,
        model: str | None = None,
        approval_policy: str | None = None,
    ) -> JsonObject:
        """Start a turn using text or already-encoded protocol input items."""

        if isinstance(input_items, str):
            encoded_items: list[Mapping[str, Any]] = [
                {"type": "text", "text": input_items}
            ]
        else:
            encoded_items = list(input_items)
        params = _without_none(
            {
                "threadId": thread_id,
                "input": encoded_items,
                "cwd": cwd,
                "model": model,
                "approvalPolicy": approval_policy,
            }
        )
        return self.request("turn/start", params)

    def thread_inject_items(
        self,
        thread_id: str,
        items: Sequence[Mapping[str, Any]],
    ) -> JsonObject:
        """Append raw Responses API items with ``thread/inject_items``."""

        return self.request(
            "thread/inject_items",
            {"threadId": thread_id, "items": list(items)},
        )

    def _read_stdout(self, stdout: TextIO) -> None:
        try:
            for line in stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    message = {
                        "_protocol_error": "invalid JSON from app-server",
                        "detail": str(exc),
                    }
                if not isinstance(message, dict):
                    message = {"_protocol_error": "non-object JSON from app-server"}
                self._messages.put(message)
        finally:
            self._messages.put(_STREAM_CLOSED)

    def _send(self, message: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise AppServerClosed("app-server is not running")
        payload = json.dumps(message, separators=(",", ":")) + "\n"
        try:
            with self._write_lock:
                process.stdin.write(payload)
                process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AppServerClosed("app-server stdin closed") from exc

    def _notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        message: JsonObject = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = dict(params)
        self._send(message)

    def _request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> JsonObject:
        self._request_id += 1
        request_id = self._request_id
        message: JsonObject = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            message["params"] = dict(params)
        self._send(message)

        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerTimeout(f"timed out waiting for {method}")
            try:
                response = self._messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise AppServerTimeout(f"timed out waiting for {method}") from exc
            if response is _STREAM_CLOSED:
                raise AppServerClosed(f"app-server closed while waiting for {method}")
            assert isinstance(response, dict)
            if response.get("id") != request_id:
                self._notifications.append(response)
                continue
            if "error" in response:
                error = response["error"]
                if isinstance(error, dict):
                    detail = error.get("message") or json.dumps(error, sort_keys=True)
                else:
                    detail = str(error)
                raise AppServerError(f"{method} failed: {detail}")
            result = response.get("result", {})
            if not isinstance(result, dict):
                raise AppServerError(f"{method} returned a non-object result")
            return result


def _without_none(values: Mapping[str, Any]) -> JsonObject:
    return {key: value for key, value in values.items() if value is not None}
