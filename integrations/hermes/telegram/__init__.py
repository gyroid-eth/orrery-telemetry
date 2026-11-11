"""Hermes Telegram DM channel for the TUI.

The TUI runs the agent; this plugin polls Telegram and injects inbound DMs into
the live TUI session.  The model replies with the ``telegram_reply`` tool.
This mirrors Claude Channels' Telegram MCP server, but uses Hermes's plugin
API so the TUI session is the agent session.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from . import managed_guard, settings


from collections import deque

import httpx

LOG = logging.getLogger("hermes.telegram")
API_BASE = "https://api.telegram.org"
_PLUGIN = None


def _is_tui() -> bool:
    return managed_guard.is_tui_backend()


def _configuration(ctx) -> dict:
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.plugins_loader import _plugin_home_scope

        # The TUI process may not have HERMES_HOME in its ambient thread env.
        # Read the plugin manager's profile config explicitly.
        with _plugin_home_scope(ctx._manager.home_path):
            cfg = load_config_readonly() or {}
        return settings.validate(cfg.get("telegram"))
    except Exception:
        return {}


class TelegramChannel:
    def __init__(self, ctx):
        self.ctx = ctx
        managed_guard.install_network_log_redaction()
        self.owner = managed_guard.PollOwner(ctx._manager.home_path)
        configuration = _configuration(ctx)
        self.token = settings.read_token(configuration.get("token_ref"))
        self.allow = set(configuration.get("allow_from", []))
        self.session_key: str | None = None
        self.pending: deque[str] = deque(maxlen=50)
        self.chats: set[int] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None
        self._offset = 0

    # ---- lifecycle -----------------------------------------------------
    def start(self) -> None:
        LOG.info("Hermes Telegram: start tui=%s token=%s allow=%d",
                 _is_tui(), bool(self.token), len(self.allow))
        if not _is_tui():
            LOG.info("Hermes Telegram: not a TUI host; skipping")
            return
        if not self.token:
            LOG.warning("Hermes Telegram: managed token reference is unavailable; Telegram startup failed")
            self.owner.status("credential-unavailable")
            return
        if not self.allow:
            LOG.warning("Hermes Telegram: telegram.allow_from is empty; refusing to poll")
            self.owner.status("allowlist-empty")
            return
        if not self.owner.acquire():
            return
        self._register()
        self._thread = threading.Thread(
            target=self._run_loop, name="hermes-telegram", daemon=True
        )
        self._thread.start()
        LOG.info("Hermes Telegram: polling started (allow=%d)", len(self.allow))

    def _register(self) -> None:
        schema = {
            "name": "telegram_reply",
            "description": (
                "Reply to a Telegram chat. Use the chat_id from the incoming "
                "[Telegram] message. This sends a real Telegram message."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_id": {"type": "integer", "description": "Telegram chat id"},
                    "text": {"type": "string", "description": "Message text"},
                    "reply_to": {
                        "type": ["integer", "null"],
                        "description": "Optional Telegram message id to quote",
                    },
                },
                "required": ["chat_id", "text"],
            },
        }
        self.ctx.register_tool(
            name="telegram_reply",
            toolset="telegram",
            schema=schema,
            handler=self._reply_tool,
        )
        self.ctx.register_hook("on_session_start", self._on_session)
        self.ctx.register_hook("on_session_reset", self._on_session)
        self.ctx.register_hook("agent_loop_stopped", self._on_session)
        self.ctx.on_unload(self.stop)
        LOG.info("Hermes Telegram: telegram_reply registered")

    def stop(self) -> None:
        self._stop.set()
        thread, loop = self._thread, self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._cancel_poll)
            except RuntimeError:
                LOG.debug("Managed Telegram: event loop already closed during stop")
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)
            if thread.is_alive():
                self.owner.status("telegram-stop-timeout")
                raise RuntimeError("Telegram poller did not stop; profile lock retained")

    def _cancel_poll(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    # ---- session / injection ------------------------------------------
    def _on_session(self, session_id=None, session_key=None, **kwargs) -> None:
        key = session_key or session_id
        if not key:
            return
        self.session_key = str(key)
        LOG.info("Hermes Telegram: session key captured")
        self._flush()

    def _enqueue(self, content: str) -> None:
        self.pending.append(content)
        self._flush()

    def _flush(self) -> None:
        if not self.session_key:
            return
        while self.pending:
            content = self.pending[0]
            try:
                ok = self.ctx.inject_message(
                    content, role="user", session_key=self.session_key
                )
            except Exception:
                LOG.warning("Hermes Telegram: inject failed", exc_info=False)
                return
            if not ok:
                LOG.warning("Hermes Telegram: inject rejected; keeping queued")
                return
            self.pending.popleft()

    # ---- Telegram loop ------------------------------------------------
    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        failed = False
        try:
            self._task = loop.create_task(self._poll_loop())
            if self._stop.is_set():
                self._task.cancel()
            loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            failed = True
            self.owner.status("telegram-startup-failed", error_type=type(exc).__name__)
            LOG.warning("Managed Telegram: poll thread crashed (%s)", type(exc).__name__)
        finally:
            # Finish transport/request cancellation before releasing the profile lock.
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            self._task = None
            self._loop = None
            if not failed:
                self.owner.status("telegram-stopped")
            self.owner.release()

    async def _poll_loop(self) -> None:
        LOG.info("Hermes Telegram: poll loop entering")
        from plugins.platforms.telegram.telegram_network import (
            SEED_FALLBACK_IPS,
            TelegramFallbackTransport,
        )

        transport = TelegramFallbackTransport(SEED_FALLBACK_IPS)
        timeout = httpx.Timeout(45.0, connect=10.0, read=45.0, write=10.0, pool=10.0)
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
            self._client = client
            LOG.info("Hermes Telegram: poll client ready")
            identity = await self._api("getMe", {})
            self.owner.ready((identity.get("result") or {}).get("username"))
            while not self._stop.is_set():
                try:
                    data = await self._api("getUpdates", {
                        "offset": self._offset,
                        "timeout": 25,
                        "allowed_updates": ["message"],
                    })
                    self.owner.healthy()
                    for upd in data.get("result") or []:
                        self._offset = max(self._offset, int(upd.get("update_id", 0)) + 1)
                        self._handle_update(upd)
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    if self.owner.state not in {"telegram-conflict", "telegram-error"}:
                        self.owner.status("telegram-network-error", error_type=type(exc).__name__)
                    # Never include the URL/token in the message.
                    LOG.warning("Hermes Telegram: poll failed (%s)", type(exc).__name__)
                    await asyncio.sleep(3)
            self._client = None

    async def _api(self, method: str, payload: dict) -> dict:
        if self._client is None:
            raise RuntimeError("telegram client not ready")
        r = await self._client.post(f"{API_BASE}/bot{self.token}/{method}", json=payload)
        try:
            data = r.json()
        except Exception:
            raise RuntimeError("telegram non-json response")
        if not data.get("ok"):
            code = data.get("error_code")
            self.owner.error(code)
            if code == 409:
                LOG.warning("Hermes Telegram: another poller holds the bot token (409)")
            raise RuntimeError(f"telegram api error {code or '?'}")
        return data

    def _handle_update(self, upd: dict) -> None:
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        uid = sender.get("id")
        if chat.get("type") != "private" or uid not in self.allow:
            return
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if not text:
            text = "(non-text message; reply with a short acknowledgement)"
        chat_id = int(chat.get("id"))
        message_id = int(msg.get("message_id") or 0)
        self.chats.add(chat_id)
        content = (
            f"[Telegram] chat_id={chat_id} message_id={message_id}\n"
            f"Reply with telegram_reply(chat_id={chat_id}, text=..., reply_to={message_id}).\n\n"
            f"{text}"
        )
        self._enqueue(content)

    # ---- reply tool ---------------------------------------------------
    def _reply_tool(self, params, **kwargs):
        try:
            chat_id = int(params["chat_id"])
            text = str(params["text"])
            reply_to = params.get("reply_to")
            if chat_id not in self.chats and chat_id not in self.allow:
                return json.dumps({"ok": False, "error": "chat_id not allowed"})
            ids = self._send_sync(chat_id, text, reply_to)
            return json.dumps({"ok": True, "message_ids": ids})
        except Exception as exc:
            LOG.warning("Hermes Telegram: reply failed (%s)", type(exc).__name__)
            return json.dumps({"ok": False, "error": "telegram_send_failed"})

    def _send_sync(self, chat_id: int, text: str, reply_to=None) -> list[int]:
        loop = self._loop
        if loop is None or not loop.is_running():
            raise RuntimeError("telegram poll loop not running")
        future = asyncio.run_coroutine_threadsafe(
            self._send_async(chat_id, text, reply_to), loop
        )
        return future.result(timeout=35)

    async def _send_async(self, chat_id: int, text: str, reply_to=None) -> list[int]:
        # Telegram's text limit is 4096; leave room for safety.
        chunks = [text[i:i + 3500] for i in range(0, len(text), 3500)] or [""]
        ids: list[int] = []
        for chunk in chunks:
            payload = {"chat_id": chat_id, "text": chunk}
            if reply_to:
                payload["reply_parameters"] = {
                    "message_id": int(reply_to),
                    "allow_sending_without_reply": True,
                }
            data = await self._api("sendMessage", payload)
            result = data.get("result") or {}
            if result.get("message_id"):
                ids.append(int(result["message_id"]))
        return ids


def register(ctx):
    global _PLUGIN
    if not managed_guard.is_tui_backend():
        LOG.info("Managed Telegram: non-backend host; poll disabled")
        return
    _PLUGIN = TelegramChannel(ctx)
    _PLUGIN.start()
