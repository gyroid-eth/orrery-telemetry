"""HTTP admission and projections for computer use."""
from functools import lru_cache
import importlib.util
import json
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, urlsplit

try:
    from dashboard.http_operator import Admission
except ModuleNotFoundError:
    from http_operator import Admission


OPERATOR = "/api/computer-use"
CHILD = OPERATOR + "/child"
CHILD_EVENTS = CHILD + "/events"
EVENTS = OPERATOR + "/events"
PATHS = {OPERATOR, CHILD, CHILD_EVENTS, EVENTS}


@lru_cache(maxsize=4)
def state_module(hooks_dir):
    file = Path(hooks_dir) / "screen_state.py"
    if not file.is_file():
        file = Path(__file__).resolve().parents[1] / "hooks/screen_state.py"
    spec = importlib.util.spec_from_file_location("orrery_screen_state", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def admit(handler, path, home):
    authority = Admission(home, f"http://127.0.0.1:{handler.server.server_port}")
    if (path, handler.command) in {(CHILD, "GET"), (CHILD_EVENTS, "POST")}:
        authority.network(handler.headers, handler.client_address[0])
    else:
        authority.http(handler.headers, handler.client_address[0])


def respond(handler, path, runtime_dir, hooks_dir):
    try:
        state = state_module(hooks_dir)
        store = state.Store(runtime_dir)
        method = handler.command
        query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True)
        if method == "GET" and path in (OPERATOR, EVENTS):
            if path == OPERATOR and not query:
                value = {"launches": store.list()}
            else:
                if set(query) != set(state.TARGET) or any(len(row) != 1 for row in query.values()):
                    raise state.ScreenStateError("SCREEN_TARGET_INVALID")
                target = {key: entry[0] for key, entry in query.items()}
                value = store.get(target) if path == OPERATOR else {"calls": store.recent(target)}
        elif method == "POST" and path in (OPERATOR, CHILD_EVENTS) and not query:
            if handler.headers.get_content_type() != "application/json":
                raise state.ScreenStateError("SCREEN_JSON_REQUIRED", 415)
            content_length = handler.headers.get_all("Content-Length", [])
            if (len(content_length) != 1 or not content_length[0].isascii() or not content_length[0].isdigit()
                    or len(content_length[0]) > 4 or not 1 <= int(content_length[0]) <= 8192
                    or handler.headers.get("Transfer-Encoding")):
                raise state.ScreenStateError("SCREEN_BODY_INVALID")
            body = json.loads(handler.rfile.read(int(content_length[0])))
            if path == OPERATOR:
                if not isinstance(body, dict) or set(body) != {*state.TARGET, "computer_use"}:
                    raise state.ScreenStateError("SCREEN_TARGET_INVALID")
                mode = body.pop("computer_use")
                value = store.change(body, mode)
            else:
                store.log(_bearer(handler), body)
                value = {"ok": True}
        elif method == "GET" and path == CHILD and not query:
            value = store.child(_bearer(handler))
        else:
            raise state.ScreenStateError("SCREEN_REQUEST_INVALID")
        handler._send(200, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8")
    except (OSError, ValueError, sqlite3.Error) as exc:
        handler.close_connection = True
        if isinstance(exc, (OSError, sqlite3.Error)):
            status, reason = 503, "SCREEN_STATE_UNAVAILABLE"
        else:
            status = getattr(exc, "status", 400)
            reason = str(exc) if hasattr(exc, "status") else "SCREEN_REQUEST_INVALID"
        handler._send(status, json.dumps({"ok": False, "reason": reason}).encode(),
                      "application/json; charset=utf-8")


def _bearer(handler):
    header = handler.headers.get("Authorization", "")
    return header[7:] if header.startswith("Bearer ") else ""
