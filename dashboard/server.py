#!/usr/bin/env python3
"""Agent Dashboard — tmux 上で動く Claude エージェント一覧 + ワンクリック飛び先。

データソース (すべて read-only):
  - tmux           : セッション / アクティブペインのタイトル(Claude のライブ作業表示) / アタッチ状況
  - mcp-agent-mail : storage.sqlite3 から task_description(受けた指示の要約) / model /
                     最終アクティブ時刻 / 最後に受信した指示メッセージ

エンドポイント:
  GET  /              -> index.html
  GET  /api/agents    -> JSON (エージェント一覧、UI が数秒ごとにポーリング)
  POST /api/jump      -> {session: NAME} 受信。対応端末で tmux attach/resume
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import secrets
import signal
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse
import urllib.request
from urllib.parse import urlparse, parse_qs

# `server.py` is both an executable script and an importable dashboard module
# in the tests.  Support both import roots without coupling callers to cwd.
try:
    from dashboard.providers.codex_app import CodexAppRuntimeProvider
except ModuleNotFoundError:  # direct `python dashboard/server.py`
    from providers.codex_app import CodexAppRuntimeProvider


def _env_path(name: str, default: str = "") -> str:
    value = os.environ.get(name, default).strip()
    return os.path.expanduser(value) if value else ""


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_text(name: str, default: str = "") -> str:
    return (os.environ.get(name, default) or default).strip()


PORT = _env_int("AGENTSTACK_PORT", 8770)
BIND_HOST = _env_text("AGENTSTACK_BIND_HOST", "127.0.0.1")
HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "index.html")
THEME_ASSETS = {
    "/theme_core.js": (os.path.join(HERE, "theme_core.js"), "text/javascript; charset=utf-8"),
    "/theme_controller.js": (
        os.path.join(HERE, "theme_controller.js"),
        "text/javascript; charset=utf-8",
    ),
    "/theme_light.css": (os.path.join(HERE, "theme_light.css"), "text/css; charset=utf-8"),
}
DB_PATH = _env_path("AGENTSTACK_MAIL_DB", "~/mcp_agent_mail/storage.sqlite3")
MAIL_ENV_PATH = _env_path("AGENTSTACK_MAIL_ENV", "~/mcp_agent_mail/.env")
MAIL_HTTP_BEARER_MODE = _env_text(
    "AGENTSTACK_MAIL_HTTP_BEARER_MODE", "auto"
).lower()
VAULT = _env_path("AGENTSTACK_VAULT", "")
PROJECT_KEY = _env_text("AGENTSTACK_PROJECT_KEY", "")
LABEL_PREFIX = _env_text("AGENTSTACK_LABEL_PREFIX", "org.agentstack")
TERMINAL_SETTING = _env_text("AGENTSTACK_TERMINAL", "auto").lower()
HOOKS_DIR = _env_path("AGENTSTACK_HOOKS_DIR", "~/.agentstack/hooks")
RUNTIME_DIR = _env_path("AGENTSTACK_RUNTIME_DIR", "~/.agentstack/runtime")
MAIL_HOME = _env_path("AGENTSTACK_MAIL_HOME", "~/.mcp_agent_mail")
SIGNALS_DIR = _env_path("AGENTSTACK_SIGNALS_DIR", os.path.join(MAIL_HOME, "signals"))
MAIL_WATCHER_LABEL = f"{LABEL_PREFIX}.mail-watcher"
NOTIFY_DAEMON_LABEL = f"{LABEL_PREFIX}.notify-daemon"
MAIL_WATCHER_PIDFILE = _env_path(
    "AGENTSTACK_MAIL_WATCHER_PIDFILE",
    "/tmp/mcp-agent-mail-watcher.lock/watcher.pid",
)
MAIL_WATCHER_HEARTBEAT = _env_path(
    "AGENTSTACK_MAIL_WATCHER_HEARTBEAT",
    os.path.join(os.path.dirname(MAIL_WATCHER_PIDFILE), "heartbeat"),
)
PORT_64 = os.path.join(HERE, "portraits_64")
# High-resolution portraits are optional distribution assets.  Keep the legacy
# 64px set as a graceful fallback when a downstream install does not ship them.
PORT_HI = os.path.join(HERE, "portraits_hi")
PORTRAIT_OVERLAY_DIR = _env_path("AGENTSTACK_PORTRAITS_DIR", "")
CUSTOM_PORTRAITS_PATH = _env_path("AGENTSTACK_CUSTOM_PORTRAITS", "")
DASHBOARD_LANG = _env_text("AGENTSTACK_LANG", "").lower()
DASHBOARD_MURMUR = _env_text("AGENTSTACK_MURMUR", "").lower()

_DASHBOARD_CONFIG_MARKER = b"const AGENTSTACK_SERVER_DEFAULTS={language:null,murmur:null};"


def _render_dashboard_index(
    source: bytes,
    language: str = DASHBOARD_LANG,
    murmur: str = DASHBOARD_MURMUR,
) -> bytes:
    """Inject allow-listed display defaults without exposing the server env."""
    config = {
        "language": language if language in {"ja", "en"} else None,
        "murmur": "off" if murmur == "off" else None,
    }
    replacement = (
        "const AGENTSTACK_SERVER_DEFAULTS="
        + json.dumps(config, ensure_ascii=True, separators=(",", ":"))
        + ";"
    ).encode()
    return source.replace(_DASHBOARD_CONFIG_MARKER, replacement, 1)


def _resolve_version() -> str:
    """Resolve a shipped version artifact before falling back to git metadata."""
    for version_file in (os.path.join(os.path.dirname(HERE), "VERSION"), os.path.join(HERE, "VERSION")):
        try:
            with open(version_file, encoding="utf-8") as f:
                version = f.read().strip()
            if version:
                return version
        except OSError:
            pass
    try:
        return subprocess.run(
            ["git", "-C", os.path.dirname(HERE), "describe", "--tags", "--always", "--dirty"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _project_key() -> str:
    return PROJECT_KEY or VAULT

# --- Model-string normalization (read-time, non-destructive) ---------------
# Each session registers a free-form `model` string, so the same model shows
# up under many spellings (claude-opus-4-7 / opus-4.7 / claude-opus-4-7[1m]).
# Fold them to one display label. Structural parser (<family>-<version>) so
# future versions (opus-4.8 ...) work with no code change. Source of truth:
# ~/mcp_agent_mail/src/mcp_agent_mail/model_normalize.py — keep in sync.
_MN_FAMILIES = ("fable", "mythos", "opus", "sonnet", "haiku", "gpt", "gemini", "grok",
                "llama", "qwen", "mistral", "deepseek")
_MN_FAMILY_RE = re.compile(
    r"(?P<family>" + "|".join(_MN_FAMILIES) + r")[-_. ]*"
    r"(?P<version>\d+(?:[.\-]\d+)?)"
    r"(?P<variant>[-_]?(?:codex|thinking|mini|nano|turbo|flash|pro|preview|exp))?"
)
_MN_VENDOR_RE = re.compile(r"^(?:claude|anthropic|openai|google)[-_. ]*")
_MN_CTX_RE = re.compile(r"[\[\(\-_ ]*\d+\s*m(?:[-_ ]?context)?[\]\)]*\s*$")
_MN_DISPLAY = {"fable": "Fable", "mythos": "Mythos", "opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku",
               "gpt": "GPT", "gemini": "Gemini", "grok": "Grok",
               "llama": "Llama", "qwen": "Qwen", "mistral": "Mistral",
               "deepseek": "DeepSeek"}
_MN_ALIASES: dict[str, str] = {}  # add codename->canonical when one appears


def _normalize_model(raw: str | None) -> str:
    if not raw or not raw.strip():
        return ""
    original = raw.strip()
    s = _MN_ALIASES.get(original.lower(), original.lower())
    s = _MN_VENDOR_RE.sub("", s)
    s = _MN_CTX_RE.sub("", s).strip()
    m = _MN_FAMILY_RE.search(s)
    if not m:
        return original
    variant = (m.group("variant") or "").lstrip("-_")
    canon = f"{m.group('family')}-{m.group('version').replace('-', '.')}"
    return f"{canon}-{variant}" if variant else canon


def _display_model(raw: str | None) -> str:
    canon = _normalize_model(raw)
    if not canon:
        return ""
    m = _MN_FAMILY_RE.search(canon)
    if not m:
        return canon
    fam = _MN_DISPLAY.get(m.group("family"), m.group("family").title())
    variant = (m.group("variant") or "").lstrip("-_")
    label = f"{fam} {m.group('version')}"
    return f"{label} {variant}" if variant else label


# family → 提供元 (network view の provider logo 判定に使用)。
# 不明 family は空文字を返し、フロントで logo 非描画。
_MN_PROVIDER = {
    "fable": "anthropic", "mythos": "anthropic",
    "opus": "anthropic", "sonnet": "anthropic", "haiku": "anthropic",
    "gpt": "openai",
    "gemini": "google", "grok": "xai",
    "llama": "meta", "qwen": "alibaba",
    "mistral": "mistral", "deepseek": "deepseek",
}


def _provider_of(raw: str | None) -> str:
    """モデル文字列から提供元キーを返す（"anthropic" / "openai" / ...）。
    判定不能なら空文字。display 用ではなく分類用なので canon に揃えて family を抽出。"""
    canon = _normalize_model(raw)
    if not canon:
        return ""
    m = _MN_FAMILY_RE.search(canon)
    if not m:
        return ""
    return _MN_PROVIDER.get(m.group("family"), "")


# 1M ベータ枠の表記ゆれ: claude-opus-4-7[1m] / opus-4.7 1m / "(1M context)"
_MN_1M_RE = re.compile(r"(?:^|[\[\(\-_ ])1\s*m(?:[-_ ]?context)?(?:[\]\) ]|$)",
                        re.IGNORECASE)


def _ctx_window(raw: str | None) -> str:
    """モデル文字列に *明示された* コンテキスト窓だけ返す（権威はペイン側
    の statusline。ここは登録名に [1m] 等が付く場合のみの補完）。
    family からの推測 200K はしない＝1M セッションを誤表示しないため。"""
    if not raw or not raw.strip():
        return ""
    if _MN_1M_RE.search(raw):
        return "1M"
    m = re.search(r"([\d.]+\s*[mk])\s*[-_ ]?context", raw, re.IGNORECASE)
    return re.sub(r"\s+", "", m.group(1)).upper() if m else ""


def _safe_portrait_name(name: str) -> bool:
    return bool(name) and "/" not in name and "\\" not in name and ".." not in name


def _png_names(directory: str) -> set[str]:
    if not directory:
        return set()
    try:
        return {
            f[:-4]
            for f in os.listdir(directory)
            if f.endswith(".png") and _safe_portrait_name(f[:-4])
        }
    except OSError:
        return set()


def _portrait_set() -> set[str]:
    return _png_names(PORT_64) | _png_names(PORT_HI) | _png_names(PORTRAIT_OVERLAY_DIR)


def _portrait_file(name: str, hi: bool) -> str:
    if not _safe_portrait_name(name) or name not in PORTRAITS:
        return ""
    fname = name + ".png"
    if PORTRAIT_OVERLAY_DIR:
        fp = os.path.join(PORTRAIT_OVERLAY_DIR, fname)
        if os.path.isfile(fp):
            return fp
    if hi:
        fp = os.path.join(PORT_HI, fname)
        if os.path.isfile(fp):
            return fp
    fp = os.path.join(PORT_64, fname)
    return fp if os.path.isfile(fp) else ""


def _portrait_fallback(name: str, hi: bool) -> bytes:
    """Return a dependency-free initials portrait for valid unknown names."""
    size = 256 if hi else 64
    initials = (name[:2] or "??").upper()
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" '
        f'height="{size}" viewBox="0 0 64 64">'
        '<rect width="64" height="64" fill="#0e1012"/>'
        '<text x="32" y="32" fill="#c4c0b2" font-family="Menlo,monospace" '
        f'font-size="26" text-anchor="middle" dominant-baseline="central">{initials}</text></svg>'
    ).encode()


def _custom_portrait_map() -> dict:
    if not CUSTOM_PORTRAITS_PATH:
        return {}
    try:
        with open(CUSTOM_PORTRAITS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


PORTRAITS = _portrait_set()

# tmux フォーマットのフィールド区切り。制御文字 US(0x1f) はペインタイトルに出ない。
SEP = "\x1f"

def _is_activity_glyph(ch: str) -> bool:
    """Claude Code のスピナー/アクティビティ先頭グリフか判定。

    Braille スピナー(U+2800–U+28FF)・ダ​ーバ​ッツの星(U+2700–U+27BF)・
    中黒(·)・ブレット(•)・アスタリスク演算子(∗) を広めにカバーする。
    """
    if not ch:
        return False
    o = ord(ch)
    return (
        0x2800 <= o <= 0x28FF  # Braille spinner frames
        or 0x2700 <= o <= 0x27BF  # ✳ ✶ ✻ ✽ など dingbats
        or ch in "·•∗*✢◐◓◑◒"
    )


def _stable_live(live: str) -> str:
    """先頭の回転グリフを除き、状態変化の署名に使える live 文言を返す。"""
    text = live.lstrip()
    while text and _is_activity_glyph(text[0]):
        text = text[1:].lstrip()
    return text


_OBSERVED_LIMIT = 4096
_observed: dict[str, tuple[tuple[object, ...], float | None]] = {}
_observed_lock = threading.Lock()


def _observe_activity(
    name: str, signature: tuple[object, ...], now: float,
) -> float | None:
    """署名が前回から変わった時刻を返す。初回観測は活動として数えない。"""
    with _observed_lock:
        previous = _observed.get(name)
        if previous is None:
            _observed[name] = (signature, None)
            return None
        previous_signature, changed_at = previous
        if previous_signature != signature:
            changed_at = now
            _observed[name] = (signature, changed_at)
        return changed_at


def _prune_observed(current_names: set[str]) -> None:
    """消滅した行を落とし、観測キャッシュに明示的な上限を設ける。"""
    with _observed_lock:
        for name in set(_observed) - current_names:
            _observed.pop(name, None)
        overflow = len(_observed) - _OBSERVED_LIMIT
        if overflow > 0:
            oldest = sorted(
                _observed,
                key=lambda name: _observed[name][1] or 0,
            )[:overflow]
            for name in oldest:
                _observed.pop(name, None)

# 実エージェントではないインフラ/プールセッション
INFRA_NAMES = {"mail-watcher"}
WARMUP_NAMES = {"warm-opus", "warm-sonnet"}


# --------------------------------------------------------------------------- #
# tmux
# --------------------------------------------------------------------------- #
def _tmux(args: list[str]) -> str:
    try:
        out = subprocess.run(
            ["tmux", *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout if out.returncode == 0 else ""
    except Exception:
        return ""


def tmux_state() -> dict:
    """セッション名 -> {created, activity, attached, cmd, title} を返す。"""
    sessions: dict[str, dict] = {}

    fmt = SEP.join([
        "#{session_name}",
        "#{session_created}",
        "#{session_activity}",
        "#{session_id}",
    ])
    for line in _tmux(["list-sessions", "-F", fmt]).splitlines():
        parts = line.split(SEP)
        if len(parts) < 3:
            continue
        name, created, activity = parts[0], parts[1], parts[2]
        sessions[name] = {
            "name": name,
            "created": _to_int(created),
            "session_id": parts[3] if len(parts) >= 4 else "",
            "activity": _to_int(activity),
            "attached": False,
            "client_tty": None,
            "cmd": "",
            "title": "",
        }

    # アクティブウィンドウのアクティブペインだけを採用 (title last + maxsplit)
    fmt = SEP.join(
        [
            "#{session_name}",
            "#{window_active}#{pane_active}",
            "#{pane_current_command}",
            "#{pane_title}",
        ]
    )
    for line in _tmux(["list-panes", "-a", "-F", fmt]).splitlines():
        parts = line.split(SEP, 3)
        if len(parts) < 4:
            continue
        name, flags, cmd, title = parts
        if flags != "11":  # active window + active pane
            continue
        if name in sessions:
            sessions[name]["cmd"] = cmd
            sessions[name]["title"] = title

    fmt = SEP.join(["#{client_session}", "#{client_tty}"])
    for line in _tmux(["list-clients", "-F", fmt]).splitlines():
        parts = line.split(SEP)
        if len(parts) < 2:
            continue
        sname, tty = parts[0], parts[1]
        if sname in sessions:
            sessions[sname]["attached"] = True
            sessions[sname]["client_tty"] = tty

    _prune_runtime_cache(sessions)
    return sessions


def _to_int(s: str) -> int:
    try:
        return int(s)
    except (ValueError, TypeError):
        return 0


# --------------------------------------------------------------------------- #
# agent-mail SQLite (read-only)
# --------------------------------------------------------------------------- #
class _ClosingConnection(sqlite3.Connection):
    """sqlite connection whose context manager also releases the file handle.

    ``sqlite3.Connection.__exit__`` only commits or rolls back; it does not
    close the connection.  Dashboard request handlers intentionally use
    ``with _db()`` throughout, so make that spelling safe by construction.
    Explicit ``con = _db(); con.close()`` callers remain supported as well.
    """

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _db():
    return sqlite3.connect(
        f"file:{DB_PATH}?mode=ro",
        uri=True,
        timeout=2,
        factory=_ClosingConnection,
    )


_RETIRED_AT_CACHE: dict[str, bool] = {}


def _has_retired_at() -> bool:
    """Does this agent-mail's `agents` table have a `retired_at` column?

    The dashboard reads a database it does not own, at whatever version the
    operator installed. A tester running a forty-day-old agent-mail has no such
    column, and every query naming it raised `OperationalError: no such column:
    a.retired_at` — which took out the whole card, and (before the descriptor
    fix) leaked the connection on the way out.

    Probe the schema rather than infer it from a version string: the column is
    what we actually depend on, and asking is cheap.
    """
    try:
        stamp = str(os.path.getmtime(DB_PATH))
    except OSError:
        return False
    cached = _RETIRED_AT_CACHE.get(stamp)
    if cached is not None:
        return cached
    con = None
    present = False
    try:
        con = _db()
        present = any(
            row[1] == "retired_at" for row in con.execute("PRAGMA table_info(agents)")
        )
    except Exception:
        present = False
    finally:
        if con is not None:
            con.close()
    _RETIRED_AT_CACHE.clear()
    _RETIRED_AT_CACHE[stamp] = present
    return present


def _retired_at_select(alias: str = "a") -> str:
    """`retired_at` where the column exists, a constant NULL where it does not.

    A schema without the column has no notion of retirement, so "nothing is
    retired" is the truthful answer, and every caller already handles NULL.
    """
    return f"{alias}.retired_at" if _has_retired_at() else "NULL AS retired_at"


def _retired_names(project_key: str) -> set[str]:
    """agent-mail が retired と見なしている名前。列が無い版では空集合。

    agent-mail は 24 時間無活動で agent を retire する。終了した session を
    片付けるぶんには妥当だが、**生きたまま idle だった常駐 agent** も巻き込む。
    そして retired agent は送信も自分の inbox 読取も素通りし、受信だけが黙って
    拒否されるので、当人も人間も気づけない。他 agent のメールが bounce して
    初めて分かる。
    """
    if not project_key or not _has_retired_at():
        return set()
    con = None
    try:
        con = _db()
        return {
            r[0] for r in con.execute(
                "SELECT a.name FROM agents a JOIN projects p ON p.id = a.project_id "
                "WHERE p.human_key = ? AND a.retired_at IS NOT NULL",
                (project_key,),
            )
        }
    except Exception:
        return set()
    finally:
        if con is not None:
            con.close()


def agentmail_state() -> tuple[dict, dict]:
    """(agents_by_name, last_instruction_by_name)。DB が無くても空で返す。"""
    agents: dict[str, dict] = {}
    instr: dict[str, dict] = {}
    if not os.path.exists(DB_PATH):
        return agents, instr
    con = None
    try:
        con = _db()
        con.row_factory = sqlite3.Row
        cur = con.cursor()

        retired_filter = "retired_at IS NULL" if _has_retired_at() else "1=1"
        cur.execute(
            f"""
            SELECT a.name, a.model, a.program, a.task_description, a.last_active_ts
            FROM agents a
            JOIN (
                SELECT name, MAX(last_active_ts) m
                FROM agents WHERE {retired_filter} GROUP BY name
            ) x ON a.name = x.name AND a.last_active_ts = x.m
            """
        )
        for r in cur.fetchall():
            agents[r["name"]] = {
                "model": _display_model(r["model"]),
                "model_raw": r["model"],
                "program": r["program"],
                "task": r["task_description"] or "",
                "last_active": _iso_to_epoch(r["last_active_ts"]),
            }

        cur.execute(
            """
            SELECT a.name AS aname, m.subject, m.created_ts, m.importance,
                   sn.name AS sender
            FROM message_recipients mr
            JOIN agents a   ON a.id  = mr.agent_id
            JOIN messages m ON m.id  = mr.message_id
            JOIN agents sn  ON sn.id = m.sender_id
            JOIN (
                SELECT mr2.agent_id, MAX(m2.created_ts) mc
                FROM message_recipients mr2
                JOIN messages m2 ON m2.id = mr2.message_id
                GROUP BY mr2.agent_id
            ) last ON last.agent_id = mr.agent_id AND last.mc = m.created_ts
            """
        )
        for r in cur.fetchall():
            instr[r["aname"]] = {
                "subject": r["subject"],
                "sender": r["sender"],
                "importance": r["importance"],
                "ts": _iso_to_epoch(r["created_ts"]),
            }
    except Exception:
        pass
    finally:
        if con is not None:
            con.close()
    return agents, instr


def _iso_to_epoch(s: str | None) -> int:
    if not s:
        return 0
    try:
        s = s.replace("T", " ").split("+")[0].strip()
        if "." in s:
            s = s.split(".")[0]
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return int(dt.replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return 0


# --------------------------------------------------------------------------- #
# 合成
# --------------------------------------------------------------------------- #
PENDING_RE = re.compile(r"^pending-\d+$")


def classify(name: str, cmd: str, title: str, in_mail: bool,
             program: str | None = None) -> str:
    if name in INFRA_NAMES:
        return "infra"
    if name in WARMUP_NAMES:
        return "warmup"
    glyph = bool(title) and _is_activity_glyph(title[0])
    claude = cmd in ("node", "claude") or glyph
    # Codex は pane_current_command が zsh で報告されることが多く (REPL の node
    # が zsh の子プロセスのため)、glyph が消える待機中に "finished" 誤判定して
    # しまう。agent-mail に program=codex-cli で登録され、かつ tmux session が
    # 生きているなら「Codex 起動中」とみなす。終了時は tmux session が消えて
    # build_agents の 2nd pass で gone/retired として扱われる。
    if not claude and program and program.startswith("codex") and in_mail:
        claude = True
    if PENDING_RE.match(name):
        return "unnamed" if claude else "idle"
    if claude:
        return "agent"
    if in_mail:
        # agent-mail に登録が残るが claude プロセスが生きていない
        # = exit 済みでセッションだけ残骸として残っている
        return "finished"
    return "idle"


def build_agents() -> list[dict]:
    sessions = tmux_state()
    mail_agents, mail_instr = agentmail_state()
    codex_apps = _codex_app_runtimes()
    now = int(time.time())
    didx = _deliverables_index()  # {agent: [...]}（60秒キャッシュ）
    retired_names = _retired_names(_project_key())
    substitutions = _name_substitutions()
    rows = []
    for name, s in sessions.items():
        m = mail_agents.get(name)
        cat = classify(name, s["cmd"], s["title"], m is not None,
                       program=(m or {}).get("program"))
        title = s["title"].strip()
        # ペインタイトルがコマンド名そのものや空ならライブ表示としては無意味
        live = ""
        if title and title not in (s["cmd"], name) and not title.startswith("/"):
            live = title
        running = s["cmd"] in ("node", "claude") or (
            bool(title) and _is_activity_glyph(title[:1])
        )
        if name == "mail-watcher":
            watcher_health = mail_watcher_health()
            running = bool(watcher_health.get("watcher_running"))
            if not live and watcher_health.get("watcher_mode"):
                live = f"watcher: {watcher_health['watcher_mode']}"
        # Codex は zsh が pane_current_command として報告されるので、上の
        # cmd チェック + glyph チェックだけでは待機中に running=False になる。
        # category と整合させるため、cat=="agent" なら running も True に。
        if not running and cat == "agent":
            running = True
        last_active = max(
            s["activity"],
            m["last_active"] if m else 0,
        )
        # 稼働中はペイン直読みの runtime（HP・状態・経過時間・窓）を一括取得。
        # 4.5s TTL キャッシュ済みなので graph_payload との重複呼び出しは無料。
        rt = (
            _agent_runtime(name, s.get("created"), s.get("session_id"))
            if running
            else {}
        )
        rows.append(
            {
                "name": name,
                "category": cat,
                "running": running,
                "attached": s["attached"],
                # Exact same-name presence in agent-mail is the identity link.
                # tmux client attachment is a separate UI/safety signal and
                # must never imply that registration succeeded.
                "mail_linked": m is not None,
                # Alive here, retired over there. The sweep cannot see tmux;
                # this side can, so this side is where the contradiction shows
                # up. Reported, never repaired on its own — the agent is still
                # having a conversation, and silently changing its state is
                # how "it looked fine" happens.
                "retired_but_alive": name in retired_names,
                # The name we asked agent-mail for, when it granted a different
                # one. Empty for everybody else.
                "requested_name": substitutions.get(name, ""),
                "cmd": s["cmd"],
                "live": live,
                # pane のステータスバー由来を優先（warm pool claim で DB
                # の model が実 model と乖離するケースを救う）。
                "model": _display_model(rt.get("pane_model")) or
                         (m or {}).get("model", ""),
                "model_raw": rt.get("pane_model") or (m or {}).get("model_raw", ""),
                "provider": _provider_of(
                    rt.get("pane_model")
                    or (m or {}).get("model_raw")
                    or (m or {}).get("model")),
                "ctx_window": rt.get("ctx_window") or _ctx_window(
                    (m or {}).get("model_raw") or (m or {}).get("model")),
                "ctx_used": rt.get("ctx_used"),
                "act_state": rt.get("act_state"),
                "work_disp": rt.get("work_disp"),
                "last_disp": rt.get("last_disp"),
                "task": (m or {}).get("task", ""),
                "instruction": mail_instr.get(name),
                "deliv": len(didx.get(name, [])),
                "mail_active": m["last_active"] if m else None,
                "last_active": last_active,
                "last_active_rel": _rel(last_active, now),
                "created": s["created"],
                "surface": "tmux",
            }
        )
    # 2nd pass: tmux 不在の agent-mail 登録 (retired / gone) も rows に含める。
    # これが無いと kill 直後の retired agent が deck の showAll でも見えず、
    # 検索・resume の起点が失われる (2026-05-20 ユーザー報告)。
    seen = {r["name"] for r in rows}
    project_key = _project_key()
    if project_key:
        con = None
        try:
            con = _db()
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            # 過去30日に絞って historical noise を除外（587 件全部出すと deck が
            # 飽和する）。直近 kill した相手を showAll で探す用途には十分。
            retired_flag = (
                "a.retired_at IS NOT NULL" if _has_retired_at() else "0"
            )
            cur.execute(
                f"""
                SELECT a.name, a.model, a.task_description, a.last_active_ts,
                       {retired_flag} AS retired
                FROM agents a
                JOIN projects p ON a.project_id = p.id
                WHERE p.human_key = ?
                  AND a.last_active_ts > datetime('now', '-30 days')
                ORDER BY a.last_active_ts DESC
                """,
                (project_key,),
            )
            for r in cur.fetchall():
                if r["name"] in seen:
                    continue
                la = _iso_to_epoch(r["last_active_ts"])
                row = {
                    "name": r["name"],
                    "category": "retired" if r["retired"] else "gone",
                    "running": False, "attached": False, "mail_linked": False,
                    "cmd": "", "live": "",
                    "model": _display_model(r["model"]), "model_raw": r["model"],
                    "provider": _provider_of(r["model"]),
                    "ctx_window": _ctx_window(r["model"]),
                    "ctx_used": None, "act_state": None,
                    "work_disp": None, "last_disp": None,
                    "task": r["task_description"] or "",
                    "instruction": None,
                    "deliv": len(didx.get(r["name"], [])),
                    "mail_active": la,
                    "last_active": la, "last_active_rel": _rel(la, now),
                    "created": 0, "retired": bool(r["retired"]),
                    "surface": "tmux",
                }
                # A Codex App runtime has no tmux pane.  It may still be a
                # live dashboard agent, but only provider-validated snapshots
                # are allowed to promote it from gone/retired.
                runtime = None if r["retired"] else codex_apps.get(r["name"])
                if runtime:
                    app_live = _codex_app_live(runtime)
                    row.update(cmd="codex-app", live=app_live["live"],
                               surface="codex-app", model=runtime["model"],
                               model_raw=runtime["model"],
                               provider=_provider_of(runtime["model"]))
                    if app_live["running"]:
                        row.update(category="agent", running=True,
                                   act_state=app_live["act_state"])
                    else:
                        row.update(category="finished")
                rows.append(row)
        except Exception:
            pass  # 失敗しても tmux ベースの rows は返す
        finally:
            if con is not None:
                con.close()

    observed_now = time.time()
    for row in rows:
        signature = (
            row.get("act_state"),
            row.get("ctx_used"),
            _stable_live(row.get("live") or ""),
        )
        row["observed_active"] = _observe_activity(
            row["name"], signature, observed_now,
        )
    _prune_observed({row["name"] for row in rows})

    order = {
        "agent": 0,
        "finished": 1,
        "unnamed": 2,
        "warmup": 3,
        "infra": 4,
        "idle": 5,
        "gone": 6,
        "retired": 7,
    }
    rows.sort(
        key=lambda r: (
            order.get(r["category"], 9),
            0 if r["running"] else 1,
            -r["last_active"],
        )
    )
    return rows


def _rel(epoch: int, now: int) -> str:
    if not epoch:
        return "—"
    d = max(0, now - epoch)
    if d < 60:
        return f"{d}s 前"
    if d < 3600:
        return f"{d // 60}m 前"
    if d < 86400:
        return f"{d // 3600}h 前"
    return f"{d // 86400}d 前"


# --------------------------------------------------------------------------- #
# Graph (親子 + agent-mail 通信網)
# --------------------------------------------------------------------------- #
_CODEX_APP_PROVIDER = CodexAppRuntimeProvider()
_CAPP_CACHE: dict = {"ts": 0.0, "map": {}}
_CAPP_RUN_STATES = frozenset(("registering", "working", "waiting", "blocked"))


def _codex_app_runtimes() -> dict[str, dict]:
    """Return provider-validated Codex App records keyed by agent name."""
    now = time.time()
    if now - _CAPP_CACHE["ts"] < 4.5:
        return _CAPP_CACHE["map"]
    records: dict[str, dict] = {}
    for snapshot in _CODEX_APP_PROVIDER.list_runtimes():
        rec = dict(snapshot.metadata)
        name = rec.get("agent_name")
        if not isinstance(name, str) or not name:
            continue
        previous = records.get(name)
        if previous and (previous.get("last_seen_at") or "") > (rec.get("last_seen_at") or ""):
            continue
        records[name] = rec
    _CAPP_CACHE.update(ts=now, map=records)
    return records


def _codex_app_live(rec: dict) -> dict:
    """Convert a provider record into the dashboard's live-state vocabulary."""
    state = str(rec.get("state") or "")
    seen = _iso_to_epoch(str(rec.get("last_seen_at") or "").replace("Z", ""))
    delta = max(0, int(time.time()) - seen) if seen else 99999
    # Bridge snapshots are write-on-change.  A once-working record that has not
    # been refreshed for ten minutes must not remain visibly active forever.
    if state in _CAPP_RUN_STATES and delta > 600:
        state = "dormant"
    running = state in _CAPP_RUN_STATES
    delivery = rec.get("delivery") or {}
    wake = str(delivery.get("wake_status") or "")
    live = f"Codex App · {state}" if wake in ("", "idle") else f"Codex App · wake:{wake}"
    return {
        "present": True, "running": running, "attached": False,
        "live": live, "state": "run" if running else "finished",
        "sig": round(max(0.0, min(1.0, 1.0 - delta / 480.0)), 3) if running else 0.0,
        "act_state": {"working": "work", "registering": "work", "waiting": "wait", "blocked": "ask"}.get(state),
        "surface": "codex-app",
    }


def _open_codex_app(name: str) -> dict:
    for snapshot in _CODEX_APP_PROVIDER.list_runtimes():
        if snapshot.metadata.get("agent_name") == name:
            result = _CODEX_APP_PROVIDER.perform(snapshot.external_id, "open")
            if result.ok:
                return {"ok": True, "action": "opened", "detail": "Codex App を前面化", **dict(result.details)}
            return {"ok": False, "error": result.error or "Codex App activation failed"}
    return {"ok": False, "error": "unknown Codex App runtime"}


_GRAPH_CACHE: dict = {"ts": 0, "data": None}


def _raw_graph() -> dict:
    """graph_data.build_graph() を遅延 import + 8 秒キャッシュ。

    build_graph の集計をポーリングのたびに繰り返さないようにする。"""
    now = time.time()
    if _GRAPH_CACHE["data"] is not None and now - _GRAPH_CACHE["ts"] < 8:
        return _GRAPH_CACHE["data"]
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import graph_data  # noqa: PLC0415  (lazy: 壊れても全体は落とさない)

    data = graph_data.build_graph()
    _GRAPH_CACHE.update(ts=now, data=data)
    return data


# エージェント名 -> 成果物 LOG の索引（60秒キャッシュ）。
# LOG_*.md の frontmatter `agent:` がノード名と一致するものを成果物とみなす。
_DELIV_CACHE: dict = {"ts": 0.0, "key": None, "map": None}
_DELIV_BASE_CACHE: dict = {"cwd": None, "base": None}
_AGENT_FM_RE = re.compile(r"^agent:\s*(.+?)\s*$", re.M)


def _deliverable_roots() -> list[str]:
    """Return generic, project-scoped roots that may contain ``LOG_*.md``.

    An explicit colon-separated ``AGENTSTACK_DELIVERABLE_ROOTS`` list is the
    project's source of truth.  Otherwise the log skill's fallback contract is
    used: ``logs/`` below the configured project.  With no project path, the
    server's cwd is resolved to its git root once and then used as the fallback.
    ``AGENTSTACK_VAULT`` remains a link-integration hint; it is not a private
    directory-layout convention.
    """
    configured = os.environ.get("AGENTSTACK_DELIVERABLE_ROOTS", "").strip()
    if configured:
        candidates = configured.split(os.pathsep)
    else:
        base = PROJECT_KEY if os.path.isabs(PROJECT_KEY) else ""
        if not base:
            base = VAULT if os.path.isabs(VAULT) else ""
        if base:
            base = os.path.realpath(os.path.expanduser(base))
        else:
            cwd = os.path.realpath(os.getcwd())
            base = (
                _DELIV_BASE_CACHE.get("base")
                if _DELIV_BASE_CACHE.get("cwd") == cwd else None
            )
            if not base:
                base = cwd
                try:
                    result = subprocess.run(
                        ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                        capture_output=True,
                        text=True,
                        timeout=2,
                        check=False,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        base = result.stdout.strip()
                except (OSError, subprocess.SubprocessError):
                    pass
                _DELIV_BASE_CACHE.update(cwd=cwd, base=base)
        candidates = [os.path.join(base, "logs")]

    roots: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        root = os.path.realpath(os.path.expanduser(candidate))
        if os.path.isdir(root) and root not in seen:
            seen.add(root)
            roots.append(root)
    return roots


def _deliverable_location(path: str, root: str) -> tuple[str, str]:
    """Return ``(relative_path, obsidian_vault_name)`` for one result."""
    if VAULT:
        vault_root = os.path.realpath(VAULT)
        real_path = os.path.realpath(path)
        try:
            if os.path.commonpath([real_path, vault_root]) == vault_root:
                return (
                    os.path.relpath(real_path, vault_root),
                    os.path.basename(os.path.normpath(vault_root)),
                )
        except ValueError:
            pass
    return os.path.relpath(path, root), ""


def _deliverables_index() -> dict:
    """{agent_name: [{title, rel, mtime}, ...]} を返す（mtime 降順）。

    走査対象は ``AGENTSTACK_DELIVERABLE_ROOTS``、未設定なら project の
    ``logs/``。frontmatter 先頭付近のみ読むので軽量。"""
    now = time.time()
    roots = _deliverable_roots()
    cache_key = (tuple(roots), os.path.realpath(VAULT) if VAULT else "")
    cached = _DELIV_CACHE["map"]
    if (cached is not None and _DELIV_CACHE.get("key") == cache_key
            and now - _DELIV_CACHE["ts"] < 60):
        return cached

    idx: dict[str, list] = {}
    seen_files: set[str] = set()
    for root in roots:
        for dp, _dn, fns in os.walk(root):
            for fn in fns:
                if not (fn.startswith("LOG_") and fn.endswith(".md")):
                    continue
                fp = os.path.join(dp, fn)
                real_fp = os.path.realpath(fp)
                if real_fp in seen_files:
                    continue
                seen_files.add(real_fp)
                try:
                    with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
                        head = fh.read(800)  # frontmatter のみで十分
                except OSError:
                    continue
                m = _AGENT_FM_RE.search(head)
                if not m:
                    continue
                ag = m.group(1).strip().strip('"').strip("'")
                if not ag:
                    continue
                try:
                    mt = int(os.path.getmtime(fp))
                except OSError:
                    mt = 0
                rel, vault_name = _deliverable_location(fp, root)
                idx.setdefault(ag, []).append(
                    {"title": fn[:-3],
                     "rel": rel,
                     "vault": vault_name,
                     "mtime": mt}
                )
    for v in idx.values():
        v.sort(key=lambda x: -x["mtime"])
    _DELIV_CACHE.update(ts=now, key=cache_key, map=idx)
    return idx


# --------------------------------------------------------------------------- #
# Role annotations（ノードの「タスク内役割」ラベル: 人狼デモ / delegate 構図）
#   $AGENTSTACK_RUNTIME_DIR/annotations.json =
#       {agent_name: {"role": str, "emoji": str, "group": str}}
#   tmux/agent-mail には無い情報を read-time に重ねる。色は spawn 系統に予約済
#   なので役割は色で塗らず、ノード下のチップ文字で表現（UI 規約に従う）。
# --------------------------------------------------------------------------- #
ANNOT_PATH = os.path.join(RUNTIME_DIR, "annotations.json")
LEGACY_ANNOT_PATH = os.path.join(HERE, "annotations.json")
_ANNOT_CACHE: dict = {"path": "", "mtime": -1.0, "data": {}}
_ANNOT_LOCK = threading.Lock()


def _annotation_read_path() -> str:
    """Return the canonical store, falling back to the pre-runtime location.

    Once the runtime file exists it always wins.  This keeps a legacy install
    readable until the next annotation write migrates the complete store.
    """
    if os.path.isfile(ANNOT_PATH):
        return ANNOT_PATH
    if os.path.isfile(LEGACY_ANNOT_PATH):
        return LEGACY_ANNOT_PATH
    return ANNOT_PATH


def _annotations() -> dict:
    """{agent_name: {role, emoji, group}} を返す。mtime ベースで再読込。

    `{name: {...}}` の素形式と `{"agents": {name: {...}}}` ラッパの両対応。
    壊れた JSON / 不在は空 dict（チップを出さないだけで全体は落とさない）。"""
    path = _annotation_read_path()
    try:
        mt = os.path.getmtime(path)
    except OSError:
        _ANNOT_CACHE.update(path="", mtime=-1.0, data={})
        return {}
    if path == _ANNOT_CACHE.get("path") and mt == _ANNOT_CACHE["mtime"]:
        return _ANNOT_CACHE["data"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        if path == _ANNOT_CACHE.get("path"):
            return _ANNOT_CACHE.get("data") or {}
        return {}
    data: dict[str, dict] = {}
    if isinstance(raw, dict):
        src = raw.get("agents") if isinstance(raw.get("agents"), dict) else raw
        for name, v in src.items():
            if not isinstance(name, str) or not isinstance(v, dict):
                continue
            role = str(v.get("role", "")).strip()[:40]
            emoji = str(v.get("emoji", "")).strip()[:8]
            group = str(v.get("group", "")).strip()[:24]
            if role or emoji or group:
                data[name] = {"role": role, "emoji": emoji, "group": group}
    _ANNOT_CACHE.update(path=path, mtime=mt, data=data)
    return data


# --------------------------------------------------------------------------- #
# Name substitutions
#   $AGENTSTACK_RUNTIME_DIR/name-substitutions.json =
#       {registered_name: {"requested": str, "ts": str}}
#
#   agent-mail does not always register the name it was asked for; which names
#   it honours depends on its version. The agent then runs fine under a name
#   nobody else can address it by, and the only trace is a missing portrait —
#   a face is easy to read as a style choice, not as a fault. So the fact is
#   recorded where it is known (at spawn) and stated in the UI, rather than
#   left to be inferred from an absence.
# --------------------------------------------------------------------------- #
SUBST_PATH = os.path.join(RUNTIME_DIR, "name-substitutions.json")
_SUBST_CACHE: dict = {"mtime": -1.0, "data": {}}
_SUBST_LOCK = threading.Lock()


def _name_substitutions() -> dict:
    """{registered: requested}. Missing or corrupt file means no claims made."""
    try:
        mt = os.path.getmtime(SUBST_PATH)
    except OSError:
        _SUBST_CACHE.update(mtime=-1.0, data={})
        return {}
    if mt == _SUBST_CACHE["mtime"]:
        return _SUBST_CACHE["data"]
    try:
        with open(SUBST_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return _SUBST_CACHE.get("data") or {}
    data: dict[str, str] = {}
    if isinstance(raw, dict):
        for name, entry in raw.items():
            if not isinstance(name, str):
                continue
            requested = ""
            if isinstance(entry, dict):
                requested = str(entry.get("requested", "")).strip()[:128]
            elif isinstance(entry, str):
                requested = entry.strip()[:128]
            if requested and requested != name:
                data[name] = requested
    _SUBST_CACHE.update(mtime=mt, data=data)
    return data


def _record_name_substitution(registered: str, requested: str) -> None:
    """Persist that a requested identity was not the one granted.

    Best effort by design: failing to write this must not fail a spawn that
    otherwise worked. It is logged either way.
    """
    if not registered or not requested or registered == requested:
        return
    try:
        with _SUBST_LOCK:
            try:
                with open(SUBST_PATH, "r", encoding="utf-8") as f:
                    store = json.load(f)
            except (OSError, ValueError):
                store = {}
            if not isinstance(store, dict):
                store = {}
            store[registered] = {
                "requested": requested,
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            os.makedirs(RUNTIME_DIR, exist_ok=True)
            tmp = f"{SUBST_PATH}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(store, f, indent=2, ensure_ascii=False)
            os.replace(tmp, SUBST_PATH)
    except OSError as e:
        logging.warning("could not record name substitution %r->%r: %s",
                        requested, registered, e)


def _write_annotation(name: str, role: str, emoji: str,
                      group: str = "") -> dict:
    """1 エージェント分の annotation を upsert / 削除。

    role / emoji / group がすべて空な場合だけ削除する。

    runtime の annotations.json をロックして read-modify-write（atomic
    replace）。旧 dashboard path しかない場合は内容を引き継いで新 path
    に書く。"""
    if not name or _NAME_RE.fullmatch(name) is None:
        return {"ok": False, "error": "invalid name"}
    role = (role or "").strip()[:40]
    emoji = (emoji or "").strip()[:8]
    group = (group or "").strip()[:24]
    with _ANNOT_LOCK:
        source_path = _annotation_read_path()
        try:
            with open(source_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raw = {}
        except (OSError, ValueError):
            raw = {}
        wrapped = isinstance(raw.get("agents"), dict)
        store = raw["agents"] if wrapped else raw
        removed = False
        if role or emoji or group:
            store[name] = {"role": role, "emoji": emoji, "group": group}
        else:
            store.pop(name, None)
            removed = True
        out = {"agents": store} if wrapped else store
        os.makedirs(os.path.dirname(ANNOT_PATH), exist_ok=True)
        tmp = ANNOT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ANNOT_PATH)
    _ANNOT_CACHE.update(path="", mtime=-1.0)  # 次回 _annotations で強制再読込
    if removed:
        return {"ok": True, "removed": name}
    return {"ok": True, "annot": {"name": name, "role": role,
                                  "emoji": emoji, "group": group}}


def graph_payload(days: float, show_all: bool) -> dict:
    """recency で間引いた {nodes, edges, spawn} + tmux ライブ状態。

    graph_data normalizes both Rust INTEGER microseconds and legacy ISO TEXT
    to UTC epoch seconds before this recency filter runs."""
    g = _raw_graph()
    graph_health = {
        "timestamp_diagnostics": g.get(
            "timestamp_diagnostics", {"invalid_count": 0, "fields": {}}
        ),
        "degraded": bool(g.get("degraded")),
    }
    nodes = g.get("nodes", [])
    if not nodes:
        return {
            "nodes": [], "edges": [], "spawn": [], "total": 0,
            **graph_health,
        }

    mx = max((n["last_active"] for n in nodes if n["last_active"]), default=0)
    win = days * 86400
    sessions = tmux_state()  # name -> {attached, cmd, title, activity, ...}
    codex_apps = _codex_app_runtimes()
    if show_all:
        keep = {n["name"] for n in nodes}
    else:
        # running_set: claude/node プロセスが alive な tmux session のみ。
        # zsh husk (session 残存だが claude 非稼働) は除外する。
        running_set: set[str] = set()
        for nm, s in sessions.items():
            t = (s.get("title") or "").strip()
            if s["cmd"] in ("node", "claude") or (t and _is_activity_glyph(t[:1])):
                running_set.add(nm)
        retired_names = {n["name"] for n in nodes if n.get("retired")}
        for nm, rec in codex_apps.items():
            if nm not in retired_names and _codex_app_live(rec)["running"]:
                running_set.add(nm)
        # running な agent は window bypass、それ以外は last_active でフィルタ。
        keep = {
            n["name"]
            for n in nodes
            if n["name"] in running_set or
               (n["last_active"] and (mx - n["last_active"]) <= win)
        }
    now_real = int(time.time())

    def live(name: str, program: str | None = None) -> dict:
        s = sessions.get(name)
        if not s:
            if program == "codex-app" and (rec := codex_apps.get(name)):
                return _codex_app_live(rec)
            # tmux セッション無し = プロセス終了済み（DB登録のみ残る）
            return {"present": False, "running": False, "attached": False,
                    "live": "", "state": "gone", "sig": 0.0}
        t = (s.get("title") or "").strip()
        running = s["cmd"] in ("node", "claude") or (
            bool(t) and _is_activity_glyph(t[:1])
        )
        # Codex は cmd=zsh で報告されるため、program=codex-cli 登録で tmux
        # session が live なら running 扱い (build_agents と同じ判定)
        if not running and program and program.startswith("codex"):
            running = True
        live_txt = ""
        if t and t not in (s.get("cmd", ""), name) and not t.startswith("/"):
            live_txt = t
        # 生存シグナル sig: tmux session_activity の新しさ＝実際に作業して
        # いるかの近似。agent-mail のメッセージ数(act)は作業量と無関係なので
        # 脈拍駆動には使わない（CalmKepler レビュー P1 指摘）
        delta = max(0, now_real - int(s.get("activity") or 0))
        sig = max(0.0, min(1.0, 1.0 - delta / 480.0))
        # graph ノードは全て agent-mail 登録済。present だが claude 非稼働
        # = exit 済でセッションだけ残った husk → idle ではなく finished
        # HP(ctx 残量) + 動作状態は running のみ取得（capture-pane 抑制）
        rt = (
            _agent_runtime(name, s.get("created"), s.get("session_id"))
            if running
            else {}
        )
        return {
            "present": True,
            "running": running,
            "attached": s.get("attached", False),
            "live": live_txt,
            "state": "run" if running else "finished",
            "sig": round(sig, 3) if running else 0.0,
            "ctx_used": rt.get("ctx_used"),
            "act_state": rt.get("act_state"),
            "ctx_window": rt.get("ctx_window"),  # ペイン直読み（権威）
            "work_disp": rt.get("work_disp"),    # work 中の経過（live）
            "work_secs": rt.get("work_secs"),
            "last_disp": rt.get("last_disp"),    # wait 中の直前ターン尺
            "pane_model": rt.get("pane_model"),  # ステータスバー由来モデル
        }

    didx = _deliverables_index()  # {agent: [...]}（60秒キャッシュ）
    annots = _annotations()       # {agent: {role, emoji, group}}（mtime キャッシュ）
    substitutions = _name_substitutions()  # {registered: requested}
    fn = [
        {**n, "rel": _rel(n["last_active"], mx) if n["last_active"] else "—",
         "deliv": len(didx.get(n["name"], [])),
         "annot": annots.get(n["name"]),
         # 要求した名前が通らず別名で登録された場合のみ非空。肖像が出ない
         # 理由をここで名指しする（顔の不在から察させない）。
         "requested_name": substitutions.get(n["name"], ""),
         **(lv := live(n["name"], n.get("program"))),
         # 窓: running はペイン直読み(権威)、不在はモデル文字列で補完
         "ctx_window": lv.get("ctx_window") or _ctx_window(n.get("model")),
         # モデル: pane 由来を優先 (warm pool claim で DB が乖離するケース)
         #   display 形式に揃える (build_agents と同じ正規化)
         "model": _display_model(lv.get("pane_model")) or n.get("model"),
         # provider: family ベースで anthropic / openai 等を判定（logo 用）
         "provider": _provider_of(lv.get("pane_model") or n.get("model"))}
        for n in nodes
        if n["name"] in keep
    ]
    fe = [
        e
        for e in g.get("edges", [])
        if e["source"] in keep and e["target"] in keep
    ]
    fs = [
        s
        for s in g.get("spawn", [])
        if s["source"] in keep and s["target"] in keep
    ]
    return {
        "nodes": fn,
        "edges": fe,
        "spawn": fs,
        "total": len(nodes),
        "shown": len(fn),
        **graph_health,
    }


# --------------------------------------------------------------------------- #
# Jump
# --------------------------------------------------------------------------- #
# Ghostty 自身の Scripting Suite を使う(System Events 非依存 = launchd でも可)。
# OSC2 でセットした端末タイトル(= セッション名)を持つ terminal を探して focus。
FOCUS_OSA = """
on run argv
  set tgt to item 1 of argv
  tell application "Ghostty"
    activate
    repeat with w in windows
      repeat with t in tabs of w
        repeat with trm in terminals of t
          try
            if (name of trm) contains tgt then
              focus trm
              return "raised"
            end if
          end try
        end repeat
      end repeat
    end repeat
  end tell
  return "activated"
end run
"""


_NAME_RE = re.compile(r"[A-Za-z0-9_.\-]+")


def _valid(session: str) -> bool:
    return bool(session) and _NAME_RE.fullmatch(session) is not None


def _has_session(session: str) -> bool:
    return subprocess.run(
        ["tmux", "has-session", "-t", f"={session}"], capture_output=True
    ).returncode == 0


ITERM_OSA = """
on run argv
  set cmd to item 1 of argv
  tell application "iTerm2"
    activate
    create window with default profile command cmd
  end tell
end run
"""


TERMINAL_OSA = """
on run argv
  set cmd to item 1 of argv
  tell application "Terminal"
    activate
    do script cmd
  end tell
end run
"""


def _mac_app_exists(app_name: str) -> bool:
    return (
        os.path.isdir(os.path.join("/Applications", app_name))
        or os.path.isdir(os.path.expanduser(f"~/Applications/{app_name}"))
    )


def _auto_terminal() -> str:
    if sys.platform != "darwin":
        return "none"
    if _mac_app_exists("Ghostty.app") or shutil.which("ghostty"):
        return "ghostty"
    if _mac_app_exists("iTerm.app") or _mac_app_exists("iTerm2.app"):
        return "iterm"
    if _mac_app_exists("Terminal.app") or os.path.isdir(
        "/System/Applications/Utilities/Terminal.app"
    ):
        return "terminal"
    return "none"


def _terminal_adapter() -> str:
    if TERMINAL_SETTING in ("", "auto"):
        return _auto_terminal()
    if TERMINAL_SETTING in ("ghostty", "iterm", "terminal", "none"):
        return TERMINAL_SETTING
    return "none"


def _terminal_unsupported() -> dict:
    return {
        "ok": False,
        "error": "terminal jump unsupported; set AGENTSTACK_TERMINAL=ghostty, iterm, terminal, or none",
    }


def _zsh_safe_quote(a: str) -> str:
    """shlex.quote + zsh の先頭展開対策。

    shlex.quote は `=` と `~` を「安全」扱いして裸で返すが、zsh は
    先頭 `=` を EQUALS 展開(`=cmd` → コマンドのパス)、先頭 `~` を
    チルダ展開する。tmux の完全一致ターゲット `=SwiftBohr` をそのまま
    `do script`(Terminal/iTerm は zsh で実行)へ渡すと
    `zsh: SwiftBohr not found` になる。先頭がこれらの文字なら強制クオート。
    """
    s = shlex.quote(a)
    if s == a and a[:1] in ("=", "~"):
        s = "'" + a.replace("'", "'\\''") + "'"
    return s


def _shell_join(argv: list[str]) -> str:
    return " ".join(_zsh_safe_quote(a) for a in argv)


def _open_terminal_tmux(tmux_args: list[str], title: str) -> dict:
    adapter = _terminal_adapter()
    if adapter == "none":
        return _terminal_unsupported()
    try:
        if adapter == "ghostty":
            cmd = [
                "env", "-u", "TMUX", "-u", "TMUX_PANE",
                "open", "-na", "Ghostty.app", "--args",
                f"--title={title}", "-e",
            ] + tmux_args
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        else:
            shell_cmd = _shell_join(
                ["env", "-u", "TMUX", "-u", "TMUX_PANE"] + tmux_args
            )
            osa = ITERM_OSA if adapter == "iterm" else TERMINAL_OSA
            r = subprocess.run(
                ["osascript", "-e", osa, shell_cmd],
                capture_output=True, text=True, timeout=8,
            )
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "").strip()
            return {"ok": False, "error": msg or f"{adapter} launch failed"}
        return {"ok": True, "adapter": adapter}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{adapter} launch failed: {e}"}


def _focus_existing_terminal(session: str) -> bool:
    if _terminal_adapter() != "ghostty":
        return False
    try:
        r = subprocess.run(
            ["osascript", "-e", FOCUS_OSA, session],
            capture_output=True,
            text=True,
            timeout=8,
        )
        return (r.stdout or "").strip() == "raised"
    except Exception:
        return False


CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")


_TPATH_CACHE: dict[str, tuple[float, str | None]] = {}


def _ownership_score(text: str, name: str) -> int:
    """text が name の「自分の transcript」である度合い。

    単純な `"name"` 出現数だと、他エージェントを多く語る巨大な
    オーケストレータ transcript(例: 親/事故対応セッション)が本人の
    小さな transcript を出現数で上回り誤判定する。そこで「その
    エージェントが“自分として”行動した痕跡」を強く重み付けする:
      - `"sender_name": "N"` … N が send_message した(=N の transcript)
      - `"agent_name": "N"`  … N が fetch_inbox/予約を自分で実行
    これらは他人の transcript にはほぼ出ない(他人は N の sender_name で
    送らない)。最後に素の `"N"` 出現を弱い加点として残す。
    """
    s = 0
    for key, w in (("sender_name", 6), ("agent_name", 4)):
        s += w * text.count(f'"{key}": "{name}"')
        s += w * text.count(f'"{key}":"{name}"')
    s += text.count(f'"{name}"')          # 弱い加点(mail 未使用 agent 用)
    return s


def _scan_selfref(files: list[str], name: str, cap: int) -> tuple[str | None, int]:
    """files(新しい順)を最大 cap 件、各 3MB まで読み、所有度スコア最大の
    (ファイル, スコア)を返す。スコア 0 なら (None, 0)。

    スコアも返すのは、別エージェントが同じファイルを主張したときに
    どちらが本人かを比べるため(下の _claim_transcript)。"""
    best, best_s = None, 0
    for f in files[:cap]:
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                sc = _ownership_score(fh.read(3_000_000), name)
        except OSError:
            continue
        if sc > best_s:
            best, best_s = f, sc
    return (best, best_s) if best_s > 0 else (None, 0)


# transcript path -> (agent name, score, exact)。
# 一本の transcript は一人のものである。この登録簿が無いと、子について
# 多く語る親の transcript が子の小さな transcript を出現数で上回り、
# 親子のカードに同じ履歴が出る(テスター報告 ⑯: 親子とも 66/66 で
# 同一 jsonl、内容は親のもの)。名前ごとに独立に解いていたので、
# 二人が同じ答えに到達しても誰も気づかなかった。
_TPATH_OWNER: dict[str, tuple[str, int, bool]] = {}


def _claim_transcript(path: str, name: str, score: int, *, exact: bool) -> bool:
    """path を name のものとして主張する。通れば True。

    exact(登録時に焼いた session index)は常に勝ち、決して奪われない。
    ヒューリスティック同士は所有度スコアで比べ、弱いほうは諦める
    ——他人の履歴を出すくらいなら、何も出さないほうがよい。
    """
    held = _TPATH_OWNER.get(path)
    if held is not None and held[0] != name:
        held_name, held_score, held_exact = held
        if held_exact and not exact:
            return False
        if not exact and score <= held_score:
            return False
        # こちらの主張のほうが強い: 相手のキャッシュを捨てて奪う。
        _TPATH_CACHE.pop(held_name, None)
    _TPATH_OWNER[path] = (name, score, exact)
    return True


def _agent_window(name: str) -> tuple[int, int]:
    """agent-mail DB から (inception_epoch, last_active_epoch)。不明は 0。"""
    if not os.path.exists(DB_PATH):
        return (0, 0)
    con = None
    try:
        con = _db()
        r = con.execute(
            "SELECT inception_ts, last_active_ts FROM agents "
            "WHERE name=? ORDER BY last_active_ts DESC LIMIT 1",
            (name,),
        ).fetchone()
        if r:
            return (_iso_to_epoch(r[0]), _iso_to_epoch(r[1]))
    except Exception:
        pass
    finally:
        if con is not None:
            con.close()
    return (0, 0)


def _all_transcripts() -> list[str]:
    out = []
    try:
        for d in os.scandir(CLAUDE_PROJECTS):
            if not d.is_dir():
                continue
            for f in os.scandir(d.path):
                if f.name.endswith(".jsonl"):
                    out.append(f.path)
    except OSError:
        pass
    return out


SESSION_INDEX_DIR = os.path.join(RUNTIME_DIR, "session_index")


def _agent_id_for_name(name: str) -> int | None:
    """agent-mail DB から name の最新 agent id を返す(無ければ None)。

    UNIQUE(project_id, name) なので 1 プロジェクト内では name→id は一意。
    プロジェクトをまたぐ同名は last_active 最新を採る。"""
    if not os.path.exists(DB_PATH):
        return None
    con = None
    try:
        con = _db()
        r = con.execute(
            "SELECT id FROM agents WHERE name=? "
            "ORDER BY last_active_ts DESC LIMIT 1",
            (name,),
        ).fetchone()
        return int(r[0]) if r else None
    except Exception:
        return None
    finally:
        if con is not None:
            con.close()


def _indexed_transcript(name: str) -> str | None:
    """精密マップ(record-session-index.py が登録時に書く id→sessionId/
    transcript)から該当 transcript を引く。

    name→agent-mail id→`~/.agentstack/runtime/session_index/<id>.json` の
    transcript_path を返す。これは selfref スコア+活動期間窓のヒューリス
    ティックと違い、登録時に焼いた exact な対応なので同名使い回し・
    last_active 固着のどちらにも左右されない。マップが無い(本フック導入前
    に登録された古いエージェント)・ファイルが消えている場合は None を返し、
    呼び出し側のヒューリスティックにフォールバックさせる。"""
    aid = _agent_id_for_name(name)
    if aid is None:
        return None
    f = os.path.join(SESSION_INDEX_DIR, f"{aid}.json")
    try:
        with open(f, encoding="utf-8") as fh:
            o = json.load(fh)
    except (OSError, ValueError):
        return None
    # Only a record that says what it is may be exact authority. Records
    # written before the schema existed, and records made when one session
    # registered a different agent, both name a transcript belonging to
    # somebody else -- a parent's transcript once appeared on a child's card
    # this way. Falling back to the heuristic is better than showing the wrong
    # session with certainty.
    if o.get("schema_version") != 2 or o.get("binding_kind") != "self":
        return None
    if o.get("agent_name") != name:
        return None
    caller = o.get("registered_by")
    if not isinstance(caller, str) or (caller and caller != name):
        return None
    tp = o.get("transcript_path")
    if isinstance(tp, str) and tp.endswith(".jsonl") and os.path.isfile(tp):
        return tp
    return None


def _transcript_path(session: str) -> str | None:
    """tmux セッション/エージェント名 → Claude transcript JSONL を特定。

    各 transcript には自分の名前が JSON 値 `"<name>"`(register/inbox/
    reservation/sender 等)として突出して多く現れる。これを使い:

    1) 稼働中: tmux ペインの cwd → projects ディレクトリ配下で自己参照
       最多の jsonl を選ぶ。
    2) 終了済み(tmux ペイン無し): agent-mail の活動期間(inception〜
       last_active)で全 projects の jsonl を mtime 絞り込みし、自己参照
       最多の jsonl を選ぶ。データは DB/ディスクに残るので閲覧可能。

    結果は 120 秒キャッシュ。
    """
    now = time.time()
    hit = _TPATH_CACHE.get(session)
    if hit and now - hit[0] < 120:
        return hit[1]

    # 0) 精密マップ優先(登録時に焼いた id↔sessionId↔transcript)。
    #    あればヒューリスティックを完全に飛ばす。NobleHubble 型の
    #    "last_active 固着で活動期間窓から実ファイルが外れる" バグや
    #    同名使い回しの誤マッチをここで根治する。
    indexed = _indexed_transcript(session)
    if indexed:
        _claim_transcript(indexed, session, 1 << 30, exact=True)
        _TPATH_CACHE[session] = (now, indexed)
        return indexed

    chosen: str | None = None
    chosen_score = 0

    # 1) 稼働中: tmux ペイン cwd 由来
    out = subprocess.run(
        ["tmux", "display-message", "-p", "-t", session,
         "#{pane_current_path}"],
        capture_output=True, text=True, timeout=4,
    )
    cwd = (out.stdout or "").strip()
    if out.returncode == 0 and cwd:
        d = os.path.join(CLAUDE_PROJECTS, re.sub(r"[^A-Za-z0-9]", "-", cwd))
        if os.path.isdir(d):
            js = sorted(
                (os.path.join(d, f) for f in os.listdir(d)
                 if f.endswith(".jsonl")),
                key=os.path.getmtime, reverse=True,
            )
            if js:
                # 自己参照ヒット時のみ採用。0 件なら下の横断スキャンへ。
                # (cwd dir の最新へフォールバックしない: `claude --resume`
                #  を別 cwd から起動した等で cwd が transcript と不一致の
                #  場合、無関係エージェントの履歴を誤表示するため)
                chosen, chosen_score = _scan_selfref(js, session, 40)

    # 2) cwd で特定できない(終了済み / resume で cwd 不一致 等):
    #    全 projects 横断 + 活動期間 mtime 絞り込みで自己参照最多。
    #    窓内で 0 件なら全件にフォールバックする。last_active_ts が登録時
    #    から進まない(=登録だけして以後 agent-mail を叩かず resume だけ
    #    された)エージェントは活動期間窓が狭すぎ実ファイル(mtime が窓の
    #    上限より新しい)を取りこぼすため(NobleHubble 事例)。窓優先で同名
    #    使い回しは正しく区別しつつ、窓ミス時だけ全件で救済する。
    if chosen is None:
        inc, last = _agent_window(session)
        files = _all_transcripts()
        files.sort(key=_safe_mtime, reverse=True)
        if inc or last:
            lo = (inc or 0) - 21600          # 6h 前
            hi = (last or 9_999_999_999) + 172800  # 2d 後
            win = [f for f in files
                   if lo <= _safe_mtime(f) <= hi]
            chosen, chosen_score = _scan_selfref(win, session, 80)
        if chosen is None:
            chosen, chosen_score = _scan_selfref(files, session, 120)

    if chosen is not None and not _claim_transcript(
        chosen, session, chosen_score, exact=False
    ):
        # 同じファイルを、より強く主張する別エージェントがいる。
        # 他人の履歴を見せるより空のほうがましなので諦める。
        chosen = None

    _TPATH_CACHE[session] = (now, chosen)
    return chosen


ABS_CLAUDE = os.path.expanduser("~/.local/bin/claude")


def _transcript_cwd(path: str) -> str | None:
    """transcript JSONL から元の cwd を抽出（各イベントに `cwd` フィールド）。"""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for _ in range(60):  # 先頭付近に必ず出る
                line = f.readline()
                if not line:
                    break
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                c = o.get("cwd")
                if isinstance(c, str) and os.path.isdir(c):
                    return c
    except OSError:
        pass
    return None


def _agent_program(session: str) -> str:
    """agent-mail から program 文字列を引く（codex 判定用）。

    register_agent の program は警告系で書き換わらず安定。空なら "" を返す。
    codex は "codex" / "codex-cli" の双方があるため startswith("codex") で判定。"""
    project_key = _project_key()
    if not project_key:
        return ""
    try:
        with _db() as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT a.program FROM agents a "
                "JOIN projects p ON a.project_id=p.id "
                "WHERE a.name=? AND p.human_key=? "
                "ORDER BY a.last_active_ts DESC LIMIT 1",
                (session, project_key),
            ).fetchone()
            return (row["program"] or "") if row else ""
    except sqlite3.Error:
        return ""


def do_resume(session: str) -> dict:
    """retire 済み / 過去セッションを tmux 再開する。

    Claude は `claude --resume <sid>`、Codex は `codex resume <sid>`。
    transcript ファイル名/メタ = sessionId、`cwd` = 元の作業ディレクトリ。
    対応端末で `tmux new-session -A` し、その中で resume。セッション名=
    エージェント名に揃える(identity 整合)。

    ※ codex agent は ~/.claude/projects/ に自分の transcript を持たないため
      Claude 用 _transcript_path の selfref 探索だと「その名前を最も多く参照
      する別 agent(=子)の transcript」を誤マッチする(親 agent が子 agent の
      会話で復元される事故の実績あり)。program で先に分岐して回避する。"""
    program = _agent_program(session)
    if program == "codex-app" and session in _codex_app_runtimes():
        return _open_codex_app(session)
    if program.startswith("codex"):
        return _do_resume_codex(session)
    path = _transcript_path(session)
    if not path:
        return {"ok": False,
                "error": f"'{session}' の会話ログが見つからず再開できません"}
    sid = os.path.basename(path)[:-6]  # 末尾 .jsonl を除去
    if not re.fullmatch(r"[0-9A-Fa-f-]{8,}", sid):
        return {"ok": False, "error": f"sessionId 不正: {sid}"}
    cwd = _transcript_cwd(path)
    if not cwd:
        return {"ok": False,
                "error": "元の作業ディレクトリ(cwd)を特定できず再開できません"}
    if not os.path.exists(ABS_CLAUDE):
        return {"ok": False, "error": "claude CLI が見つかりません"}
    # 端末adapter経由で tmux new-session(-A=あれば attach)。
    #
    # 重要: claude を「単一文字列」で tmux に渡すと tmux は `/bin/sh -c`
    # で実行し ~/.zshrc を読まない → ~/.local/bin が PATH に入らず
    # ("Native installation ... not in your PATH") claude/フックが
    # 不安定化しクラッシュループした(2026-05-18 事故)。
    # 対策: `zsh -lic <inner>` を独立 argv で渡す。tmux は複数引数なら
    # execvp で直接起動するので sh 層が消え、ログイン対話 zsh が .zshrc
    # (24行目で ~/.local/bin を PATH へ追加) を source する。多重防御
    # として inner 先頭でも明示 export し、exec で claude にプロセス
    # 置換(余分なシェルを残さず tmux セッション名=エージェント名を維持)。
    # zshexit 事故(2026-05-18)は .zshrc 側で根治済み(真因は巨大 context
    # ではなく zshexit が Bash サブシェル exit 毎に tmux セッションを kill
    # していた事)。サイズに関わらず常に --resume で会話を完全復元する。
    # AGENT_NAME を export してから exec claude する。これがないと claude
    # 内部の register_agent で AGENT_NAME 環境変数が空となり、サーバーが
    # ランダム名を発番してしまう (2026-05-22 GreenOstwald 事例、
    # 2026-05-26 PinkGuericke 事例)。
    inner = (
        'export PATH="$HOME/.local/bin:$PATH"; '
        f'export AGENT_NAME={session}; '
        f'exec {ABS_CLAUDE} --resume {sid} -n {session}'
    )
    # env -u TMUX -u TMUX_PANE: 端末プロセスに TMUX が継承されると
    # 以後の全ウィンドウへ幽霊 TMUX が伝播し、cx 等の `[[ -n "$TMUX" ]]` 判定が
    # 誤爆する(2026-06-02 調査)。dashboard が tmux 内から再起動された場合に備え剥がす。
    launch = _open_terminal_tmux(
        ["tmux", "new-session", "-A", "-s", session, "-c", cwd,
         "zsh", "-lic", inner],
        title=session,
    )
    if launch.get("ok"):
        return {
            "ok": True,
            "action": "resumed",
            "detail": f"会話を tmux で再開 (sid {sid[:8]}… / {cwd})",
            "terminal": launch.get("adapter"),
        }
    return {"ok": False, "error": f"resume 起動失敗: {launch.get('error')}"}


def _codex_meta(path: str) -> tuple[str | None, str | None]:
    """Codex rollout 1 行目 session_meta から (session_id, cwd) を返す。"""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            first = f.readline().strip()
        o = json.loads(first)
        if o.get("type") != "session_meta":
            return None, None
        p = o.get("payload") or {}
        sid = p.get("id")
        cwd = p.get("cwd")
        if not isinstance(cwd, str) or not os.path.isdir(cwd):
            cwd = None
        return (sid if isinstance(sid, str) else None), cwd
    except (OSError, ValueError):
        return None, None


def _do_resume_codex(session: str) -> dict:
    """Codex agent を `codex resume <sid>` で tmux 再開する。

    rollout は ~/.codex/sessions/.../rollout-*.jsonl。session_meta.payload の
    id=session_id / cwd=作業ディレクトリ。cx と同じ起動条件を再現する:
      - codex_agent_bootstrap.sh を source（AGENT_NAME export + agent-mail
        再登録 + mail-watcher 起動 + tmux リネーム）
      - launch_codex_workspace.sh と同じ writable scope / sandbox / approval
    selfref 探索ではなく inception_ts 一致で rollout を引くので子の会話を
    誤マッチしない（_codex_transcript_path）。"""
    path = _codex_transcript_path(session)
    if not path:
        return {"ok": False,
                "error": f"'{session}' の Codex rollout が見つからず再開できません"}
    sid, cwd = _codex_meta(path)
    if not sid or not re.fullmatch(r"[0-9A-Fa-f-]{8,}", sid):
        return {"ok": False, "error": f"Codex session id 不正: {sid}"}
    if not cwd:
        return {"ok": False,
                "error": "元の作業ディレクトリ(cwd)を特定できず再開できません"}
    bootstrap = os.path.expanduser("~/.codex/bin/codex_agent_bootstrap.sh")
    # zsh -lic で .zshrc を読ませ codex を PATH 解決。bootstrap が無ければ
    # source をスキップ（AGENT_NAME export と resume は維持）。
    src = f'source {shlex.quote(bootstrap)}; ' if os.path.exists(bootstrap) else ''
    add_dirs = ''
    if VAULT:
        add_dirs = f' --add-dir {shlex.quote(VAULT)}'
    inner = (
        'export PATH="$HOME/.local/bin:$PATH"; '
        f'export AGENT_NAME={session}; '
        f'{src}'
        f'exec env -u OPENAI_API_KEY codex resume {sid} '
        f'-C {shlex.quote(cwd)} '
        '--sandbox workspace-write --ask-for-approval on-request '
        f'{add_dirs} '
        '--add-dir "$HOME/.claude" --add-dir "$HOME/.codex"'
    )
    launch = _open_terminal_tmux(
        ["tmux", "new-session", "-A", "-s", session, "-c", cwd,
         "zsh", "-lic", inner],
        title=session,
    )
    if launch.get("ok"):
        return {
            "ok": True,
            "action": "resumed",
            "detail": f"Codex 会話を tmux で再開 (sid {sid[:8]}… / {cwd})",
            "terminal": launch.get("adapter"),
        }
    return {"ok": False, "error": f"codex resume 起動失敗: {launch.get('error')}"}


def _safe_mtime(p: str) -> float:
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0.0


def _block_text(content) -> list[tuple[str, str]]:
    """message.content (str or block list) → [(kind, text)] に正規化。"""
    if isinstance(content, str):
        return [("text", content)] if content.strip() else []
    rows: list[tuple[str, str]] = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text" and b.get("text", "").strip():
            rows.append(("text", b["text"]))
        elif t == "thinking" and b.get("thinking", "").strip():
            rows.append(("thinking", b["thinking"]))
        elif t == "tool_use":
            inp = b.get("input", {})
            s = json.dumps(inp, ensure_ascii=False)
            if len(s) > 240:
                s = s[:240] + "…"
            rows.append(("tool_use", f"{b.get('name', '?')}  {s}"))
        elif t == "tool_result":
            c = b.get("content", "")
            if isinstance(c, list):
                c = " ".join(
                    x.get("text", "") for x in c if isinstance(x, dict)
                )
            c = str(c).strip()
            if len(c) > 400:
                c = c[:400] + "…"
            if c:
                rows.append(("tool_result", c))
    return rows


_CODEX_SESSIONS_DIR = os.path.expanduser("~/.codex/sessions")


def _codex_transcript_path(session: str) -> str | None:
    """Codex (codex-cli) 用 transcript ファイルを探索する。

    Codex は `~/.codex/sessions/YYYY/MM/DD/rollout-DATE-UUID.jsonl` に保存し、
    ファイル名にエージェント名が入らない。1 行目の session_meta.payload.timestamp
    を読み、agent-mail の inception_ts と最も近い (90 秒以内) ものを返す。

    結果は 120 秒キャッシュ。
    """
    now = time.time()
    hit = _TPATH_CACHE.get(("codex", session))
    if hit and now - hit[0] < 120:
        return hit[1]

    if not os.path.isdir(_CODEX_SESSIONS_DIR):
        _TPATH_CACHE[("codex", session)] = (now, None)
        return None

    # agent-mail から inception_ts を引く
    project_key = _project_key()
    if not project_key:
        _TPATH_CACHE[("codex", session)] = (now, None)
        return None
    inception = 0
    try:
        with _db() as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT a.inception_ts FROM agents a "
                "JOIN projects p ON a.project_id=p.id "
                "WHERE a.name=? AND p.human_key=? "
                "ORDER BY a.last_active_ts DESC LIMIT 1",
                (session, project_key),
            ).fetchone()
            if row and row["inception_ts"]:
                inception = _iso_to_epoch(row["inception_ts"])
    except sqlite3.Error:
        pass
    if not inception:
        _TPATH_CACHE[("codex", session)] = (now, None)
        return None

    best_path: str | None = None
    best_diff = 90  # 90 秒以内のみ採用
    for root, _dirs, files in os.walk(_CODEX_SESSIONS_DIR):
        for fn in files:
            if not fn.startswith("rollout-") or not fn.endswith(".jsonl"):
                continue
            fp = os.path.join(root, fn)
            try:
                with open(fp, encoding="utf-8") as fh:
                    first = fh.readline().strip()
                    if not first:
                        continue
                    o = json.loads(first)
                    if o.get("type") != "session_meta":
                        continue
                    ts_str = (o.get("payload") or {}).get("timestamp") or ""
                    ts = _iso_to_epoch(ts_str)
                    diff = abs(ts - inception) if ts else best_diff + 1
                    if diff < best_diff:
                        best_diff = diff
                        best_path = fp
            except (OSError, ValueError, KeyError):
                continue
    _TPATH_CACHE[("codex", session)] = (now, best_path)
    return best_path


def _events_from_codex_jsonl(path: str) -> list[dict]:
    """Codex rollout JSONL を Claude 互換の events list に正規化する。

    Codex の構造:
      {"timestamp": ISO, "type": "response_item",
       "payload": {"type":"message", "role":"user|assistant|developer",
                   "content":[{"type":"text", "text":"..."}]}}

    developer ロール（システムプロンプト埋め込み等）はノイズになるので
    除外し、user / assistant のみを events に展開する。
    """
    events: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") != "response_item":
                    continue
                payload = o.get("payload") or {}
                if payload.get("type") != "message":
                    continue
                role = payload.get("role")
                if role not in ("user", "assistant"):
                    continue
                ts = o.get("timestamp", "")
                content = payload.get("content")
                if isinstance(content, str):
                    if content.strip():
                        events.append({"role": role, "kind": "text",
                                       "text": content, "ts": ts})
                    continue
                if not isinstance(content, list):
                    continue
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    btype = blk.get("type", "text")
                    txt = blk.get("text", "") or ""
                    # Codex は input_text / output_text を使う (Claude の text と等価)
                    if btype in ("text", "input_text", "output_text") and txt.strip():
                        events.append({"role": role, "kind": "text",
                                       "text": txt, "ts": ts})
                    elif btype in ("image", "image_url", "input_image"):
                        events.append({"role": role, "kind": "image",
                                       "text": "", "ts": ts})
    except OSError:
        pass
    return events


def history_payload(session: str, limit: int) -> dict:
    if not _valid(session):
        return {"ok": False, "error": "invalid session name"}
    # codex agent は Claude 用 selfref 探索だと子の transcript を誤マッチする
    # ため、program で先に分岐する（do_resume と同じ理由）。
    if _agent_program(session).startswith("codex"):
        path = _codex_transcript_path(session)
        is_codex = bool(path)
        if not path:                      # 念のため Claude 側もフォールバック
            path = _transcript_path(session)
    else:
        path = _transcript_path(session)
        is_codex = False
        if not path:
            path = _codex_transcript_path(session)
            is_codex = bool(path)
    if not path:
        return {
            "ok": False,
            "error": "transcript が見つかりません"
            "（このエージェント名の会話ログがディスク上に存在しない）",
        }
    if is_codex:
        events = _events_from_codex_jsonl(path)
    else:
        events = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    typ = o.get("type")
                    if typ not in ("user", "assistant"):
                        continue
                    msg = o.get("message") or {}
                    role = msg.get("role", typ)
                    ts = o.get("timestamp", "")
                    for kind, txt in _block_text(msg.get("content")):
                        events.append(
                            {"role": role, "kind": kind, "text": txt, "ts": ts}
                        )
        except OSError as e:
            return {"ok": False, "error": f"read 失敗: {e}"}
    total = len(events)
    if limit and total > limit:
        events = events[-limit:]
    return {
        "ok": True,
        "session": session,
        "file": os.path.basename(path),
        "source": "codex" if is_codex else "claude",
        "total": total,
        "shown": len(events),
        "events": events,
    }


# --------------------------------------------------------------------------- #
# Edge messages — network view で edge クリック時の thread side-drawer 用。
#   2 agents (a, b) 間で project_id 一致の messages を双方向に列挙。
#   recipient 側は message_recipients 経由なので 1 message に複数 recipient
#   ある場合は DISTINCT で重複排除しつつ「a と b が含まれる」 message を返す。
# --------------------------------------------------------------------------- #
def edge_messages_payload(a: str, b: str, limit: int = 60) -> dict:
    if not a or not b:
        return {"ok": False, "error": "a and b required"}
    if not (_valid(a) and _valid(b)):
        return {"ok": False, "error": "invalid agent name"}
    project_key = _project_key()
    if not project_key:
        return {"ok": False, "error": "AGENTSTACK_PROJECT_KEY or AGENTSTACK_VAULT is not configured"}
    try:
        with _db() as con:
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            cur.execute(
                """
                SELECT m.id, m.created_ts, m.subject, m.body_md, m.importance,
                       m.thread_id, m.topic, m.ack_required,
                       sa.name AS sender, ra.name AS recipient,
                       mr.kind, mr.read_ts, mr.ack_ts
                FROM messages m
                JOIN projects   p  ON p.id  = m.project_id
                JOIN agents     sa ON sa.id = m.sender_id
                JOIN message_recipients mr ON mr.message_id = m.id
                JOIN agents     ra ON ra.id = mr.agent_id
                WHERE p.human_key = ?
                  AND (
                        (sa.name = ? AND ra.name = ?)
                     OR (sa.name = ? AND ra.name = ?)
                  )
                ORDER BY m.created_ts DESC
                LIMIT ?
                """,
                (project_key, a, b, b, a, max(1, min(200, limit))),
            )
            rows = cur.fetchall()
    except sqlite3.Error as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # 1 message が cc/bcc 等で複数 recipient を持つ場合、上の JOIN で
    # ra.name = ?(a or b) で既に絞られているため、(message_id, recipient)
    # の組は最大2行（双方向の重複は無いはず）。ID で dedup しつつ
    # kind / read_ts / ack_ts は最初に見つかった行のものを採用。
    seen: set = set()
    messages: list = []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        messages.append({
            "id": r["id"],
            "ts": r["created_ts"],
            "ts_unix": _iso_to_epoch(r["created_ts"]),
            "sender": r["sender"],
            "recipient": r["recipient"],
            "subject": r["subject"] or "",
            "body": r["body_md"] or "",
            "importance": (r["importance"] or "").lower(),
            "thread_id": r["thread_id"],
            "topic": r["topic"],
            "ack_required": bool(r["ack_required"]),
            "kind": r["kind"],
            "read_ts": r["read_ts"],
            "ack_ts": r["ack_ts"],
        })
    return {"ok": True, "a": a, "b": b, "count": len(messages),
            "messages": messages}


# --------------------------------------------------------------------------- #
# messages-since — network view の comet アニメ用に since 以降の新規 message
#   を取得。複数 recipient は 1 行ずつ展開して返す（comet を分けて飛ばす）。
#   リプレイ防止のため since は now-300s 未満には遡らない。
# --------------------------------------------------------------------------- #
_BODY_HEAD_STRIP_RE = re.compile(r"^[#>*\-\s`]+")

def messages_since_payload(since_ts: int, limit: int = 80) -> dict:
    now = int(time.time())
    since_ts = max(int(since_ts or 0), now - 300)
    project_key = _project_key()
    if not project_key:
        return {"ok": True, "now": now, "since": since_ts, "messages": []}
    try:
        with _db() as con:
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            # created_ts は ISO 文字列 (例 "2026-05-21 01:35:26.258733")
            # で保存されているため、unix int の since と直接比較できない。
            # strftime('%s', ...) で int 化して比較する。
            # DESC + reverse で「最新 N 件」を確実に拾う（バースト時の取りこぼし防止）
            cur.execute(
                """
                SELECT m.id,
                       CAST(strftime('%s', m.created_ts) AS INTEGER) AS ts_unix,
                       m.subject, m.body_md, m.importance,
                       m.thread_id, sa.name AS sender, ra.name AS recipient,
                       mr.kind
                FROM messages m
                JOIN projects p ON p.id = m.project_id
                JOIN agents sa ON sa.id = m.sender_id
                JOIN message_recipients mr ON mr.message_id = m.id
                JOIN agents ra ON ra.id = mr.agent_id
                WHERE p.human_key = ?
                  AND CAST(strftime('%s', m.created_ts) AS INTEGER) > ?
                ORDER BY m.created_ts DESC, m.id DESC
                LIMIT ?
                """,
                (project_key, since_ts, max(1, min(200, limit))),
            )
            rows = list(reversed(cur.fetchall()))
    except sqlite3.Error as e:
        return {"ok": False, "now": now, "error": f"{type(e).__name__}: {e}",
                "messages": []}
    out: list = []
    for r in rows:
        body = (r["body_md"] or "").strip()
        # 本文先頭の見出し/箇条書きマーカーを軽く除去して読める形に
        first = body.split("\n", 1)[0] if body else ""
        excerpt = _BODY_HEAD_STRIP_RE.sub("", first)[:120]
        out.append({
            "id": r["id"],
            "ts": r["ts_unix"],
            "sender": r["sender"],
            "recipient": r["recipient"],
            "subject": (r["subject"] or "").strip()[:90],
            "excerpt": excerpt,
            "importance": (r["importance"] or "normal").lower(),
            "kind": r["kind"],
            "thread_id": r["thread_id"],
        })
    return {"ok": True, "now": now, "since": since_ts, "messages": out}


# --------------------------------------------------------------------------- #
# Agent history (Task E) — detail panel の 24h sparkline 用。
#   agent-mail SQLite から 1 エージェントの「送信 / 受信 / spawn / retire」を
#   時系列順に返す。live state ではなく past trace を可視化するための専用源。
#
# 区分判定:
#   - mail_sent / mail_recv : message_recipients の sender/recipient 関係
#   - spawn      : subject が 「タスク依頼」or 「Task X:」 で始まる送信 → 子起動
#   - retire     : agents.retired_at がウィンドウ内に入っている場合
#   - context_warn は本セッションでは未実装（live で別系統に出すべきため）
#
# 60s in-memory cache（同じ name+hours へのバースト fetch を吸収）。
# 出力イベント数は 200 件上限（超えたら時間で均等間引き＝spike を保つ）。
# --------------------------------------------------------------------------- #
_HIST_CACHE: dict[tuple[str, int], tuple[float, dict]] = {}
_HIST_TTL = 60.0
_HIST_LOCK = threading.Lock()
_SPAWN_SUBJ_RE = re.compile(
    r"^(?:\s*re:\s*)?(?:タスク依頼|Task\s+[A-Z0-9][\w\-]*\s*[:：])",
    re.IGNORECASE,
)


def _classify_sent_subject(subj: str) -> str:
    if subj and _SPAWN_SUBJ_RE.match(subj):
        return "spawn"
    return "mail_sent"


# Task H: transcript JSONL から `?` (permission prompt 待ち) を抽出。
#   ヒューリスティクス: tool_use → 対応 tool_result の gap が ASK_GAP_MIN_S 以上
#   ある場合、その間を ask 状態とみなす（ユーザー承認待ち）。完璧ではないが
#   実用上「Edit/Write/Bash で人間が物理キーを叩く間」を概ね捉える。
#   ・toolUse 検出対象: 権限ダイアログが頻出する Tool 一式
#   ・per-agent cap: 16 イベント (start/end ペア×8) で UI を埋め尽くさない
_ASK_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"}
_ASK_GAP_MIN_S = 25       # gap [s] 以上で ask 状態とみなす
_ASK_CAP_PER_AGENT = 16   # events 数の上限（pair なので÷2）
_ASK_CACHE: dict[tuple[str, int, int], tuple[float, list[dict]]] = {}
_ASK_CACHE_TTL = 120.0    # 2 分: transcript 走査は重いので長めキャッシュ
_ASK_CACHE_LOCK = threading.Lock()


def _extract_ask_events(path: str | None, name: str,
                        since: int, until: int) -> list[dict]:
    """transcript JSONL → ask_start / ask_end events list。

    cache key = (transcript path, since, until) で 2 分 TTL。
    path=None / 読めない / 該当 0 件 → 空 list を返す（replay は壊れない）。
    """
    if not path or since <= 0 or until <= since:
        return []
    key = (path, since, until)
    now = time.time()
    with _ASK_CACHE_LOCK:
        hit = _ASK_CACHE.get(key)
        if hit and (now - hit[0]) < _ASK_CACHE_TTL:
            return hit[1]
    events: list[dict] = []
    pending: dict[str, int] = {}   # tool_use_id -> ts
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                ts = _iso_to_epoch(o.get("timestamp", ""))
                if not ts:
                    continue
                # 範囲外は読み飛ばし (transcript は時系列なので break しても良いが
                #  並びが乱れているケースを許容して continue にしている)
                if ts < since:
                    continue
                if ts > until + 300:
                    # 末尾の十分先まで来たら打ち切り (transcript は append-only)
                    break
                msg = o.get("message") or {}
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    bt = blk.get("type")
                    if bt == "tool_use":
                        tname = blk.get("name", "")
                        if tname in _ASK_TOOLS:
                            tid = blk.get("id")
                            if isinstance(tid, str):
                                pending[tid] = ts
                    elif bt == "tool_result":
                        tid = blk.get("tool_use_id")
                        if not isinstance(tid, str):
                            continue
                        start_ts = pending.pop(tid, 0)
                        if not start_ts:
                            continue
                        gap = ts - start_ts
                        if gap < _ASK_GAP_MIN_S:
                            continue
                        if start_ts < since or start_ts > until:
                            continue
                        events.append({
                            "ts": start_ts, "kind": "ask_start",
                            "agent": name, "ref": "", "subject": "",
                            "importance": "normal",
                            "sender": name, "recipient": "",
                            "id": None,
                        })
                        events.append({
                            "ts": min(ts, until), "kind": "ask_end",
                            "agent": name, "ref": "", "subject": "",
                            "importance": "normal",
                            "sender": name, "recipient": "",
                            "id": None,
                        })
                        if len(events) >= _ASK_CAP_PER_AGENT:
                            break
                if len(events) >= _ASK_CAP_PER_AGENT:
                    break
    except OSError:
        events = []
    with _ASK_CACHE_LOCK:
        _ASK_CACHE[key] = (now, events)
        if len(_ASK_CACHE) > 64:
            for k, _ in sorted(_ASK_CACHE.items(), key=lambda kv: kv[1][0])[:16]:
                _ASK_CACHE.pop(k, None)
    return events


def agent_history_payload(name: str = "", hours: int | None = 24,
                          names: list[str] | None = None,
                          include_pane_states: bool = False) -> dict:
    """Single (`name`) or multi-agent (`names`) history payload.

    Task G: `names=` is the union/replay path. Events get an `agent` field
    (whose timeline this row belongs to). Mail events seen by both endpoints
    in the selected set are de-duplicated to the sender-side row so the
    mail-comet engine doesn't fire two comets for one message.

    Task G+ (auto-range): `hours=None` means "fit window to actual event
    range" — we fetch a generous 7d window, then crop to [oldest, newest]
    with ±5% pad. Empty range falls back to 1h. Caller can still pin the
    window explicitly with `hours=N` (existing behavior).
    """
    if names:
        unique = [n for n in dict.fromkeys(names) if _valid(n)]
        if not unique:
            return {"ok": False, "error": "invalid agent names"}
        names_list = unique
        is_multi = len(names_list) > 1
    else:
        if not _valid(name):
            return {"ok": False, "error": "invalid agent name"}
        names_list = [name]
        is_multi = False
    # hours_in: 明示 hours は keep、None は auto-range path へ
    auto_range = hours is None
    if not auto_range:
        hours = max(1, min(168, int(hours)))  # 1h .. 7d
    fetch_hours = 168 if auto_range else hours        # auto は 7d ぶん引いて crop
    cache_key: tuple = (tuple(sorted(names_list)), hours, bool(include_pane_states))
    now = int(time.time())
    # キャッシュヒット（now はキャッシュ時刻のまま）
    with _HIST_LOCK:
        ent = _HIST_CACHE.get(cache_key)
        if ent and (time.time() - ent[0]) < _HIST_TTL:
            return ent[1]

    project_key = _project_key()
    if not project_key:
        return {"ok": False, "error": "AGENTSTACK_PROJECT_KEY or AGENTSTACK_VAULT is not configured"}
    since = now - fetch_hours * 3600
    agent_infos: dict[str, dict] = {}   # name -> {id, inception_ts, retired_at}
    raw_events: list[dict] = []
    selected_names: set[str] = set(names_list)
    try:
        with _db() as con:
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            for nm in names_list:
                cur.execute(
                    f"""
                    SELECT a.id, a.inception_ts, {_retired_at_select()}
                    FROM agents a
                    JOIN projects p ON p.id = a.project_id
                    WHERE p.human_key = ? AND a.name = ?
                    ORDER BY a.last_active_ts DESC
                    LIMIT 1
                    """,
                    (project_key, nm),
                )
                row = cur.fetchone()
                if not row:
                    continue
                agent_infos[nm] = {
                    "id": row["id"],
                    "inception_ts": row["inception_ts"],
                    "retired_at": row["retired_at"],
                }
            if not agent_infos:
                return {"ok": False, "error": "agent not found"}

            for nm, info in agent_infos.items():
                agent_id = info["id"]
                # 送信
                cur.execute(
                    """
                    SELECT m.id,
                           CAST(strftime('%s', m.created_ts) AS INTEGER) AS ts,
                           m.subject, m.importance, m.thread_id,
                           (SELECT ra.name
                              FROM message_recipients mr
                              JOIN agents ra ON ra.id = mr.agent_id
                              WHERE mr.message_id = m.id
                              ORDER BY mr.agent_id LIMIT 1) AS first_recipient,
                           (SELECT COUNT(*) FROM message_recipients
                              WHERE message_id = m.id) AS rcpt_n
                    FROM messages m
                    JOIN projects p ON p.id = m.project_id
                    WHERE p.human_key = ?
                      AND m.sender_id = ?
                      AND CAST(strftime('%s', m.created_ts) AS INTEGER) >= ?
                    ORDER BY m.created_ts ASC
                    """,
                    (project_key, agent_id, since),
                )
                for r in cur.fetchall():
                    subj = (r["subject"] or "").strip()
                    kind = _classify_sent_subject(subj)
                    ref = r["first_recipient"] or ""
                    if (r["rcpt_n"] or 0) > 1:
                        ref = f"{ref} +{r['rcpt_n'] - 1}"
                    raw_events.append({
                        "id": r["id"],
                        "ts": r["ts"],
                        "kind": kind,
                        "ref": ref,
                        "subject": subj[:140],
                        "importance": (r["importance"] or "normal").lower(),
                        "agent": nm,
                        "sender": nm,
                        "recipient": r["first_recipient"] or "",
                        "thread_id": r["thread_id"],
                    })
                # 受信
                cur.execute(
                    """
                    SELECT m.id,
                           CAST(strftime('%s', m.created_ts) AS INTEGER) AS ts,
                           m.subject, m.importance, m.thread_id,
                           sa.name AS sender
                    FROM messages m
                    JOIN projects p ON p.id = m.project_id
                    JOIN message_recipients mr ON mr.message_id = m.id
                    JOIN agents sa ON sa.id = m.sender_id
                    WHERE p.human_key = ?
                      AND mr.agent_id = ?
                      AND CAST(strftime('%s', m.created_ts) AS INTEGER) >= ?
                    ORDER BY m.created_ts ASC
                    """,
                    (project_key, agent_id, since),
                )
                for r in cur.fetchall():
                    raw_events.append({
                        "id": r["id"],
                        "ts": r["ts"],
                        "kind": "mail_recv",
                        "ref": r["sender"] or "",
                        "subject": (r["subject"] or "").strip()[:140],
                        "importance": (r["importance"] or "normal").lower(),
                        "agent": nm,
                        "sender": r["sender"] or "",
                        "recipient": nm,
                        "thread_id": r["thread_id"],
                    })
                # retire
                retire_ts = _iso_to_epoch(info["retired_at"]) if info["retired_at"] else 0
                if retire_ts and retire_ts >= since:
                    raw_events.append({
                        "id": None,
                        "ts": retire_ts,
                        "kind": "retire",
                        "ref": "",
                        "subject": "agent retired",
                        "importance": "normal",
                        "agent": nm,
                        "sender": nm,
                        "recipient": "",
                    })
    except sqlite3.Error as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # Dedupe — when sender も recipient も選択集合に居る mail は sender 側のみ採用
    sent_msg_ids = {
        e["id"] for e in raw_events
        if e["kind"] in ("mail_sent", "spawn") and e["id"] is not None
    }
    deduped: list[dict] = []
    for e in raw_events:
        if (
            e["kind"] == "mail_recv"
            and e["id"] in sent_msg_ids
            and e.get("sender") in selected_names
        ):
            continue
        deduped.append(e)
    events = deduped

    # Task H: transcript から ask_start/ask_end を mining (include_pane_states 時)
    # transcript が無い / Codex / Edit 系を呼ばない agent は何も追加されない。
    if include_pane_states:
        ask_window_until = max((e["ts"] for e in events), default=now) if events else now
        for nm in agent_infos:
            tpath = _transcript_path(nm)
            if not tpath:
                continue
            ask_evs = _extract_ask_events(tpath, nm, since, ask_window_until)
            events.extend(ask_evs)

    events.sort(key=lambda ev: ev["ts"])

    # ── auto-range: hours 省略時は events の min/max を ±5% pad して window 化 ──
    # 明示 hours の場合は range = [since, now] をそのまま採用（互換性のため now_ts も維持）。
    if auto_range:
        if events:
            ev_min = events[0]["ts"]
            ev_max = events[-1]["ts"]
            span = max(1, ev_max - ev_min)
            pad = int(span * 0.05)
            range_start = max(since, ev_min - pad)
            # newest event より先には伸ばさない（"now" は range の右端＝最終イベント時刻）
            range_end = min(now, ev_max + pad)
            # ただし span が極端に短い (5min 未満) と scrubber が tap しづらいので 5min まで膨らます
            if (range_end - range_start) < 300:
                cx = (range_end + range_start) // 2
                range_start = max(since, cx - 150)
                range_end = min(now, cx + 150)
        else:
            # 0 件: fallback 1h を「直近 1h」として返す（events も range も虚無）
            range_end = now
            range_start = now - 3600
    else:
        range_start = since
        range_end = now

    # 上限間引き（spawn/retire/exit/ask は温存 — topology & 状態変化は希少だから）
    #   さらに multi 選択時は「選択エージェント同士の mail」も温存する。これが
    #   replay の主役（=ユーザーが見たい会話）であり、stride 間引きに巻き込むと
    #   "6 件やりとりしたのに 1 件しか出ない" になる（third-party との cross-
    #   traffic で total_raw が膨らみ、pair の大半が stride から外れて消える）。
    #   GROUP-ONLY フィルタはこの events をさらに絞るだけなので、ここで pair を
    #   落とすとフロント側ではもう回復できない。pair は全件残し、間引くのは
    #   third-party を含む周辺トラフィックのみ。
    def _is_in_group_mail(e: dict) -> bool:
        if e.get("kind") not in ("mail_sent", "mail_recv"):
            return False
        s = e.get("sender")
        # recipient は multi 宛で "Name +N" 形になりうるので先頭名で判定
        r = (e.get("recipient") or "").split(" ", 1)[0]
        return s in selected_names and r in selected_names

    max_events = 400 if is_multi else 200
    total_raw = len(events)
    if total_raw > max_events:
        keep_kinds = {"spawn", "retire", "exit", "ask_start", "ask_end"}
        keep_idx = {
            i for i, e in enumerate(events)
            if e["kind"] in keep_kinds or (is_multi and _is_in_group_mail(e))
        }
        keep_idx.add(0)
        keep_idx.add(total_raw - 1)
        budget = max_events - len(keep_idx)
        if budget > 0:
            stride = max(1, total_raw // budget)
            for i in range(0, total_raw, stride):
                keep_idx.add(i)
                if len(keep_idx) >= max_events:
                    break
        events = [events[i] for i in sorted(keep_idx)]

    # Task H: range_start 時点での graph state snapshot
    #   alive_agents = inception_ts ≤ range_start AND (retired_at は range_start より後 / 未 retire)
    #   時刻が分からない agent は alive 扱い（保守的 fallback）。
    alive_agents: list[str] = []
    for nm, info in agent_infos.items():
        inc = _iso_to_epoch(info["inception_ts"])
        ret = _iso_to_epoch(info["retired_at"]) if info["retired_at"] else 0
        if (not inc) or inc <= range_start:
            if (not ret) or ret > range_start:
                alive_agents.append(nm)
    # auto モードでは since_ts / now_ts を「窓の見える側」に合わせる（UI が
    # この値で sparkline / scrubber を組むため）。total_raw は dedupe 後の本数。
    payload: dict = {
        "ok": True,
        "hours": hours,            # 明示時は秒数、auto 時は None
        "auto_range": auto_range,
        "since_ts": range_start,
        "now_ts": range_end,
        "range": {"start_ts": range_start, "end_ts": range_end},
        "total_raw": total_raw,
        "events": events,
        "initial_state": {
            "ts": range_start,
            "alive_agents": alive_agents,
        },
        "include_pane_states": bool(include_pane_states),
    }
    if is_multi:
        payload["names"] = list(agent_infos.keys())
        payload["agents"] = {
            nm: {
                "inception_ts": _iso_to_epoch(info["inception_ts"]),
                "retired_ts": _iso_to_epoch(info["retired_at"]) or None,
            }
            for nm, info in agent_infos.items()
        }
    else:
        only = next(iter(agent_infos))
        info = agent_infos[only]
        payload["name"] = only
        payload["inception_ts"] = _iso_to_epoch(info["inception_ts"])
        payload["retired_ts"] = (
            _iso_to_epoch(info["retired_at"]) if info["retired_at"] else None
        )
    with _HIST_LOCK:
        _HIST_CACHE[cache_key] = (time.time(), payload)
        # 簡易 LRU 風: 50 entries 超えたら古い順に間引き
        if len(_HIST_CACHE) > 50:
            for k, _ in sorted(_HIST_CACHE.items(), key=lambda kv: kv[1][0])[:10]:
                _HIST_CACHE.pop(k, None)
    return payload


# --------------------------------------------------------------------------- #
# Agent runtime probe — コンテキスト残量(HP) + 動作状態を tmux ペインから読む
#   Claude Code / Codex の TUI 表示を read-only パース。新データ源は持たない。
#   - ctx_used : Claude "ctx: N% used" / Codex "Context N% left|used" → HP
#   - act_state: work(生成中) / wait(返答待ち) / ask(承認待ち)
#   capture-pane は重いので running セッションのみ・4.5s TTL でキャッシュ
#   （フロントの /api/graph ポーリング=5s と同位相にして 1 周期 1 capture）。
#   ※ Claude と Codex は書式が排他（"ctx: N% used" は Claude のみ、"Context
#     N% left" は Codex のみ）なので両方 try しても誤マッチしない。各 Codex
#     書式は実ペインで確証済み（cxprobe 観測 2026-06-02）。
# --------------------------------------------------------------------------- #
_CTX_RE = re.compile(r"ctx:\s*(\d+)%\s*used")
# Codex ステータスライン: "gpt-5.5 medium · Context 70% left · ~/path"。
#   "left"=残量(→used=100-N) / "used"=使用率。Claude の _CTX_RE と排他。
_CTX_CODEX_LEFT_RE = re.compile(r"Context\s+(\d+)%\s*left", re.IGNORECASE)
_CTX_CODEX_USED_RE = re.compile(r"Context\s+(\d+)%\s*used", re.IGNORECASE)
# 生成中: 入力ロック中ヒント or 経過時間+トークンカウンタの作業ステータス行
#   実測例: "✽ Stewing… (2m 46s · ↓ 8.5k tokens · thought for 1s)"
_WORK_RE = re.compile(
    r"esc to interrupt"
    r"|·\s*[↑↓]?\s*[\d.]+k?\s*tokens"
    r"|\(\s*\d+m\s+\d+s\s*·"
)
# Claude Code 新TUI (2026-08 実測): タスクwidget 稼働時のスピナー行は
#   "✽ <進行中タスク名>…" だけになり、esc to interrupt・経過時間・token 数が
#   一切出ない → _WORK_RE 全滅で work が wait に化ける。グリフ行＋折返し2行以内の
#   "…" を作業中の証拠にする。タスク名折返しでグリフが単独行になる形も許容。
#   idle ペインにグリフ開始行が残らないことを Claude 6 ペインで負例確認済み。
#   "·" は単独行だと汎用的すぎるので "…" 必須側のみに含める。
#   ⚠ 2026-08-03 修正: 初版は待機中を work と誤判定した。同じグリフが **完了
#   マーカー** ("✻ Cooked for 8s") にも使われ、しかも継続行が空行を跨いで別
#   ブロックまで届いたため、無関係な過去ログの "…" と結合していた。2点で塞ぐ:
#     (1) 継続行は非空行のみ = 空行は折返しでなく別ブロックの開始
#     (2) グリフ行が "… for 8s" 形の完了マーカーなら除外（末尾が "…" の正当な
#         スピナーは末尾が duration でないので巻き添えにならない）
#   全 tmux 37 セッションで現行版と比較し、落ちるのは誤検知1体のみ・正しい
#   work 判定は不変であることを実測済み。
_WORK_CLAUDE_TASK_RE = re.compile(
    r"(?m)"
    r"^[ \t\xa0]*[✢✳✶✻✽✺·][ \t\xa0]+"
    r"(?![^\n]*\bfor\s+(?:\d+h\s*)?(?:\d+m\s*)?\d+s\s*$)"
    r"\S(?:[^\n]*\S[^\n]*\n){0,2}[^\n]*…"
    r"|^[ \t\xa0]*[✢✳✶✻✽✺][ \t\xa0]*$")
# Codex 生成中スピナー行: "• Working (1s • esc to interrupt)" / "• Thinking (…"。
#   "esc to interrupt" は _WORK_RE でも拾えるが、文言変更に備え spinner+経過尺
#   (… (Ns) という形)も正本にする。prose 誤マッチを避けるため経過尺を必須化。
_WORK_CODEX_RE = re.compile(
    r"(?m)^\s*[•▌●]\s*(?:Working|Thinking)\b[^\n]*\(\s*\d+\s*s\b")
# 承認待ち: 権限プロンプト "Do you want to …?" + 番号付き選択(❯ 1. …)。
#   旧 y/n 系もフォールバック。※実プロンプトで書式確定させること。
_ASK_Q_RE = re.compile(r"Do you want to .+\?|Would you like to proceed")
_ASK_OPT_RE = re.compile(r"(?m)^\s*[❯>›]?\s*1\.\s")
_ASK_YN_RE = re.compile(r"\?\s*[\[(]y/n[\])]", re.IGNORECASE)
# Codex 承認待ちモーダルの確定フッター。番号付き選択のセレクタが › (U+203A)
#   で Claude と別字形なので、常時出るこのフッター行を正本にする。
#   実ペイン: "Press enter to confirm or esc to cancel"。
_ASK_CODEX_RE = re.compile(
    r"Press\s+enter\s+to\s+confirm\s+or\s+esc\s+to\s+cancel", re.IGNORECASE)
# AskUserQuestion ウィジェット: フッターに "Enter to select" + "Esc to cancel" が
# 同じ 1 行に並ぶ（中黒 · や Tab/Arrow keys を挟む）。DOTALL にすると過去スク
# ロールに残った報告本文等にも誤マッチするため敢えて単一行にする。
# 2026-05-20 FrostyEinstein が完了報告本文に regex 文字列を書いた瞬間、自分自身が
# question 判定される自己マッチ事故が発生。
_QUESTION_RE = re.compile(r"Enter to select.{0,80}Esc to cancel")
# コンテキスト窓: ステータスラインの "Opus 4.7 (1M context)" を直読み。
#   登録モデル文字列より確実（実セッションが報告する値そのもの）。
_WIN_RE = re.compile(r"\(\s*([\d.]+\s*[MK])\s*context\s*\)", re.IGNORECASE)
_WIN_MODEL_RE = re.compile(
    r"(?im)^\s*Model:\s*[^\n]*\bwith\s+([\d.]+\s*[MK])\s+context\b"
)
# 狭いペインでは Claude の Model 行が ``with 1M con…`` の途中で省略
# される。この緩和は Model: 行の末尾だけに限定し、通常の会話やコードに
# 出てくる ``1M con`` を context window と誤読しない。
_WIN_TRUNC_RE = re.compile(
    r"(?im)^\s*Model:\s*[^\n]*\bwith\s+([\d.]+\s*[MK])\s+"
    r"con(?:t(?:e(?:x(?:t)?)?)?)?(?:[.…]{1,3})?\)?\s*$"
)
# 実モデル: pane のステータスバーに出る文字列を直読み。
#   Claude Code: "| Opus 4.6 | ctx: 59% used"
#   Codex:       "gpt-5.4 xhigh · Context 46% left"
# 登録時 model 文字列は warm pool claim 等で書き換わるため信用しない。
_MODEL_PANE_RE = re.compile(
    r"\b(Opus|Sonnet|Haiku)\s+(\d+(?:\.\d+)?)\b"
    r"|\b(gpt-\d+(?:\.\d+)?)(?:-(codex|mini|nano|turbo|thinking))?\b",
    re.IGNORECASE,
)

# 稼働経過時間。work 中: スピナー行の先頭尺。
#   Claude: "(2m 24s · ↓ … tokens …)"（区切り · = U+00B7）
#   Codex : "(1s • esc to interrupt)"（区切り • = U+2022）→ 両方許容。
#   wait 中: 直前ターン要約 "✻ Crunched for 1m 36s" / "Churned for 47s"。
_ACTIVE_T_RE = re.compile(
    r"\(\s*(?:(\d+)\s*h\s*)?(?:(\d+)\s*m\s*)?(\d+)\s*s\s*[·•]")
_LAST_T_RE = re.compile(
    r"\b(?:for|in)\s+(?:(\d+)\s*h\s*)?(?:(\d+)\s*m\s*)?(\d+)\s*s\b",
    re.IGNORECASE)


def _dur(h: int, m: int, s: int) -> tuple[int, str]:
    """(h,m,s) → (総秒, 表示)。表示は最大2桁単位でコンパクトに。"""
    secs = h * 3600 + m * 60 + s
    if h:
        disp = f"{h}h{m:02d}m"
    elif m:
        disp = f"{m}m{s:02d}s"
    else:
        disp = f"{s}s"
    return secs, disp


_RT_TTL = 4.5
_RT_STICKY_KEYS = ("pane_model", "ctx_window")
_rt_cache: dict[str, tuple[float, str, dict]] = {}
_rt_lock = threading.Lock()


def _parse_runtime(text: str) -> dict:
    """ペイン文字列 → runtime 値。動作状態の優先度は work>question>ask>wait。"""
    m = _CTX_RE.search(text)
    if m:                                   # Claude: "ctx: N% used"
        ctx_used = int(m.group(1))
    else:                                   # Codex: "Context N% left|used"
        cl = _CTX_CODEX_LEFT_RE.search(text)
        cu = _CTX_CODEX_USED_RE.search(text)
        if cl:
            ctx_used = 100 - int(cl.group(1))
        elif cu:
            ctx_used = int(cu.group(1))
        else:
            ctx_used = None
    # 既存の完全な ``(1M context)`` は従来どおり全 capture から採り、
    # Model 行の新書式・幅切れだけは誤読を避けるため末尾10行に限定する。
    tail_for_model = "\n".join(text.splitlines()[-10:])
    w = (
        _WIN_RE.search(text)
        or _WIN_MODEL_RE.search(tail_for_model)
        or _WIN_TRUNC_RE.search(tail_for_model)
    )
    ctx_window = re.sub(r"\s+", "", w.group(1)).upper() if w else None
    # ペイン由来の実モデル (steruslineから抽出。末尾10行に限定して
    # スクロールバッファ内のコード片やテキスト中の誤マッチを避ける)
    pane_model = None
    mm = _MODEL_PANE_RE.search(tail_for_model)
    if mm:
        if mm.group(1):  # Claude family (Opus/Sonnet/Haiku)
            pane_model = f"{mm.group(1).title()} {mm.group(2)}"
        else:  # gpt-x[.y][-variant]
            base = mm.group(3)
            variant = mm.group(4)
            pane_model = f"{base}-{variant}" if variant else base
    # AskUserQuestion ウィジェットは常にペイン最下部に表示される。スクロール
    # バッファ上部の自己マッチ（過去の出力にコードや報告文として regex 自体が
    # 書かれているケース等）を避けるため、末尾 12 行に絞って検出する。
    tail = "\n".join(text.splitlines()[-12:])
    # スピナー行はタスクwidget(可変長)の直上に出るため 12 行窓では不足。
    # スクロールバッファの過去出力への誤マッチは避けたいので 25 行に限定。
    tail25 = "\n".join(text.splitlines()[-25:])
    if _WORK_RE.search(text) or _WORK_CODEX_RE.search(text) \
            or _WORK_CLAUDE_TASK_RE.search(tail25):
        act = "work"
    elif _QUESTION_RE.search(tail):
        act = "question"
    elif _ASK_CODEX_RE.search(text) \
            or (_ASK_Q_RE.search(text) and _ASK_OPT_RE.search(text)) \
            or _ASK_YN_RE.search(text):
        act = "ask"
    else:
        act = "wait"   # running だが work/question/ask でない = ユーザー入力待ち
    work_secs = work_disp = last_disp = None
    if act == "work":
        t = _ACTIVE_T_RE.search(text)
        if t:
            work_secs, work_disp = _dur(
                int(t.group(1) or 0), int(t.group(2) or 0), int(t.group(3)))
    else:
        # 直前ターンの所要（最後にマッチしたもの＝最新）
        ms = _LAST_T_RE.findall(text)
        if ms:
            h, m, s = ms[-1]
            _, last_disp = _dur(int(h or 0), int(m or 0), int(s))
    return {"ctx_used": ctx_used, "act_state": act,
            "ctx_window": ctx_window, "work_secs": work_secs,
            "work_disp": work_disp, "last_disp": last_disp,
            "pane_model": pane_model}


def _runtime_generation(created: int | None, session_id: str | None) -> str:
    """tmux server 内で session を一意にする generation token。"""
    return f"{int(created or 0)}:{session_id or ''}"


def _prune_runtime_cache(sessions: dict[str, dict]) -> None:
    """終了または同名で再作成された tmux session の runtime を破棄する。"""
    generations = {
        name: _runtime_generation(
            record.get("created"), record.get("session_id")
        )
        for name, record in sessions.items()
    }
    with _rt_lock:
        for name, (_, generation, _) in list(_rt_cache.items()):
            if generations.get(name) != generation:
                _rt_cache.pop(name, None)


def _agent_runtime(
    session: str,
    session_created: int | None,
    session_id: str | None = None,
) -> dict:
    """running session の runtime。属性だけを generation 内で保持する。"""
    now = time.monotonic()
    generation = _runtime_generation(session_created, session_id)
    with _rt_lock:
        hit = _rt_cache.get(session)
        if hit and hit[1] != generation:
            _rt_cache.pop(session, None)
            hit = None
        if hit and (now - hit[0]) < _RT_TTL:
            return hit[2]
    out = {"ctx_used": None, "act_state": None, "ctx_window": None,
           "work_secs": None, "work_disp": None, "last_disp": None,
           "pane_model": None}
    if _valid(session):
        try:
            r = subprocess.run(
                ["tmux", "capture-pane", "-p", "-J",
                 "-t", session, "-S", "-45"],
                capture_output=True, text=True, timeout=3,
            )
            if r.returncode == 0:
                out = _parse_runtime(r.stdout)
        except Exception:
            pass
    with _rt_lock:
        previous = _rt_cache.get(session)
        if previous and previous[1] == generation:
            for key in _RT_STICKY_KEYS:
                if not out.get(key):
                    out[key] = previous[2].get(key)
        _rt_cache[session] = (now, generation, out)
    return out


def term_capture(session: str, lines: int) -> dict:
    """tmux ペインを ANSI 付き(-e)でキャプチャ。読み取り専用。

    capture-pane -e は SGR(色)のみ出力しカーソル制御は含まないため、
    フロントは SGR パーサだけで安全に描画できる。"""
    if not _valid(session):
        return {"ok": False, "error": "invalid session name"}
    if not _has_session(session):
        return {"ok": False, "error": f"session '{session}' は存在しません"}
    lines = max(40, min(4000, lines))
    # capture-pane の -t はペイン指定。= 接頭辞は不可なのでセッション名を
    # そのまま渡す(存在は上で has-session -t =session で厳密確認済み)。
    out = subprocess.run(
        ["tmux", "capture-pane", "-e", "-p", "-J",
         "-t", session, "-S", f"-{lines}"],
        capture_output=True, text=True, timeout=6,
    )
    if out.returncode != 0:
        return {"ok": False, "error": "capture-pane 失敗"}
    return {"ok": True, "session": session, "content": out.stdout}


# --------------------------------------------------------------------------- #
# ttyd: ブラウザ埋め込みのフル対話端末 (Phase 3)
#   セッションごとに on-demand で ttyd を 127.0.0.1 の空きポートに起動。
#   既存があれば再利用。一定時間アクセスが無いものは reaper が掃除。
# --------------------------------------------------------------------------- #
TTYD_BIN = shutil.which("ttyd") or "/opt/homebrew/bin/ttyd"
_TTYD: dict[str, dict] = {}
_TTYD_LOCK = threading.Lock()
_TTYD_IDLE = 900  # 15分アクセスが無ければ停止


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _ttyd_alive(rec: dict) -> bool:
    return rec.get("proc") and rec["proc"].poll() is None


def ttyd_ensure(session: str) -> dict:
    if not _valid(session):
        return {"ok": False, "error": "invalid session name"}
    if not _has_session(session):
        return {"ok": False, "error": f"session '{session}' は存在しません"}
    if not os.path.exists(TTYD_BIN):
        return {"ok": False, "error": "ttyd が見つかりません"}
    with _TTYD_LOCK:
        rec = _TTYD.get(session)
        if rec and _ttyd_alive(rec):
            rec["last"] = time.time()
            return {"ok": True, "url": f"http://127.0.0.1:{rec['port']}/",
                    "port": rec["port"], "reused": True}
        port = _free_port()
        # -W 書込可(対話) / -i 127.0.0.1 ローカル限定 / -m 同時3 /
        # -t 端末オプション / once しない(再利用) / tmux にミラー attach
        proc = subprocess.Popen(
            [TTYD_BIN, "-p", str(port), "-i", "127.0.0.1", "-W",
             "-m", "3", "-t", "fontSize=13",
             "-t", "titleFixed=" + session,
             "-t", "disableLeaveAlert=true",
             "tmux", "attach", "-t", f"={session}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _TTYD[session] = {"proc": proc, "port": port, "last": time.time()}
    time.sleep(0.5)  # ttyd の listen 開始を待つ
    if proc.poll() is not None:
        return {"ok": False, "error": "ttyd 起動失敗"}
    return {"ok": True, "url": f"http://127.0.0.1:{port}/",
            "port": port, "reused": False}


def _ttyd_kill(rec: dict) -> None:
    try:
        rec["proc"].terminate()
    except Exception:
        pass


def _ttyd_reaper() -> None:
    while True:
        time.sleep(120)
        now = time.time()
        with _TTYD_LOCK:
            for s in list(_TTYD):
                rec = _TTYD[s]
                if not _ttyd_alive(rec) or now - rec["last"] > _TTYD_IDLE:
                    _ttyd_kill(rec)
                    _TTYD.pop(s, None)


@atexit.register
def _ttyd_cleanup() -> None:
    for rec in list(_TTYD.values()):
        _ttyd_kill(rec)


def do_jump(session: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", session or ""):
        return {"ok": False, "error": "invalid session name"}
    # Codex App lives outside tmux and its provider owns the safe activation
    # action.  Check it before the terminal-adapter gate.
    if _agent_program(session) == "codex-app" and session in _codex_app_runtimes():
        return _open_codex_app(session)
    if _terminal_adapter() == "none":
        return _terminal_unsupported()
    if subprocess.run(
        ["tmux", "has-session", "-t", f"={session}"],
        capture_output=True,
    ).returncode != 0:
        # tmux セッションが無い = retire済み/過去セッション(gone)。
        # transcript から claude --resume で再開する。
        return do_resume(session)

    # tmux セッションは在るが、claude が死んで「素の zsh 残骸(husk)」だけが
    # 残っている場合がある(= category 'finished')。この husk に attach しても
    # 死んだシェルに繋がるだけで会話は復元しない。さらに do_resume へ流しても
    # `tmux new-session -A` が husk を掴んで resume コマンドを実行しないため、
    # 先に husk を kill してから do_resume で新規セッションを作る必要がある。
    # (2026-06-04「dashboard から resume が起こらない」報告の真因)
    # 信頼源は build_agents() の category（do_kill と同じ流儀。do_jump 内に
    # 独自 running 判定を書かない＝2026-05-20 自己kill 事故の教訓）。
    cat = None
    try:
        for r in build_agents():
            if r["name"] == session:
                cat = r["category"]
                break
    except Exception:  # noqa: BLE001
        cat = None
    if cat == "finished":
        subprocess.run(
            ["tmux", "kill-session", "-t", f"={session}"],
            capture_output=True,
        )
        return do_resume(session)

    # 1) Ghostty adapter では既存ウィンドウの前面化を試みる。
    if _focus_existing_terminal(session):
        return {
            "ok": True,
            "action": "raised",
            "detail": "既存ウィンドウを前面化",
            "terminal": "ghostty",
        }

    # 2) 対応端末で新規ウィンドウを開き、tmux attach -d で古いクライアントを外す。
    launch = _open_terminal_tmux(
        ["tmux", "attach", "-d", "-t", f"={session}"],
        title=session,
    )
    if launch.get("ok"):
        return {
            "ok": True,
            "action": "opened",
            "detail": "固定タイトル付き新規ウィンドウで attach (-d)",
            "terminal": launch.get("adapter"),
        }
    return {"ok": False, "error": f"open 失敗: {launch.get('error')}"}


def do_kill(session: str, mode: str = "both") -> dict:
    """finished/gone エージェントを kill する。

    mode: 'tmux' (husk shell のみ kill) / 'retire' (agent-mail soft retire のみ) /
          'both' (デフォ＝両方)。

    安全弁:
      - session 名 _valid() 必須、warm-*/pending-* は拒否
      - **build_agents() の category を信頼源**に finished/gone のみ許可。
        独自 running 判定を do_kill 内に書かない (2026-05-20 自己kill 事故)。
      - retire は soft (`agent.retired_at` を立てるだけ)。transcript JSONL は
        一切触れず、`claude --resume` も do_resume も後から動く
      - hard_delete は使わない (agent-mail timestamp 情報を保持し
        _transcript_path() の finished-branch 探索を温存)
    """
    if mode not in ("both", "tmux", "retire"):
        return {"ok": False, "error": "invalid mode"}
    if not _valid(session):
        return {"ok": False, "error": "invalid session name"}
    if session.startswith("warm-") or session.startswith("pending-"):
        return {"ok": False, "error": "warmup/pending sessions are protected"}

    # category check: build_agents() を信頼源にして finished/gone のみ許可。
    # 過去の事故 (2026-05-20): do_kill 内の独自 running 判定が cmd チェック
    # のみで title 活動グリフ条件を抜かしており、SwiftFaraday(自分自身)を
    # kill する事故が発生。build_agents の compound 判定 (cmd OR activity
    # glyph) を信頼源として再利用する。
    target = None
    try:
        for r in build_agents():
            if r["name"] == session:
                target = r
                break
    except Exception as e:
        return {"ok": False, "error": f"failed to enumerate agents: {e}"}
    if target is None:
        return {"ok": False, "error": f"agent '{session}' not found"}
    if target.get("running"):
        return {"ok": False,
                "error": f"agent '{session}' is running - refusing to kill",
                "category": target.get("category")}
    # attached=True は「人間が現に画面で見ている tmux client がいる」状態。
    # running 判定が /compact 等で false negative になっても、attached は
    # tmux 直接シグナルで信頼できる。2026-05-20 SilverBoltzmann kill 起点の
    # SwiftFaraday 連鎖事故の真因はここの穴 (running=False かつ attached=True
    # で kill ボタンが出てしまった)。defense in depth として独立ガード化。
    if target.get("attached"):
        return {"ok": False,
                "error": f"agent '{session}' has an attached tmux client "
                         "- refusing to kill (detach first)",
                "category": target.get("category")}
    if target["category"] not in ("finished", "gone"):
        return {"ok": False,
                "error": f"agent '{session}' category={target['category']} "
                         "- only finished/gone are killable"}

    actions = []

    # 1) retire 先 (agent-mail の retired_at を立てる)
    if mode in ("both", "retire"):
        project_key = _project_key()
        if not project_key:
            actions.append("retire-no-project-key")
            row = None
        else:
            try:
                with _db() as conn:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute(
                        f"SELECT a.id, {_retired_at_select()} FROM agents a "
                        "JOIN projects p ON a.project_id=p.id "
                        "WHERE a.name=? AND p.human_key=?",
                        (session, project_key),
                    ).fetchone()
            except Exception as e:
                row = None
                actions.append(f"retire-db-err:{e}")
        if row is None:
            actions.append("retire-no-record")
        elif row["retired_at"]:
            actions.append("retire-already")
        else:
            req = urllib.request.Request(
                _mail_web_url("/mail/api/retire-agent"),
                data=json.dumps({"agent_id": row["id"]}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=4) as r:
                    body = json.loads(r.read())
                    actions.append("retired" if body.get("success")
                                   else f"retire-fail:{body}")
            except Exception as e:
                actions.append(f"retire-err:{e}")

    # 2) tmux kill (husk shell があれば)
    if mode in ("both", "tmux"):
        if _has_session(session):
            r = subprocess.run(
                ["tmux", "kill-session", "-t", f"={session}"],
                capture_output=True, text=True,
            )
            actions.append("tmux-killed" if r.returncode == 0
                           else f"tmux-fail:{r.stderr.strip()}")
        else:
            actions.append("tmux-absent")

    return {"ok": True, "session": session, "mode": mode, "actions": actions}


# --------------------------------------------------------------------------- #
# do_spawn — dashboard から子エージェントを spawn する control panel 入口。
#   1) mcp-agent-mail に HTTP/JSON-RPC で register_agent → child name 取得
#   2) /api/annotate ロジックで role/emoji/group を反映 (失敗しても続行)
#   3) HTTP/JSON-RPC で send_message → child inbox にタスクメッセージ投函
#   4) spawn_child.sh --pre-registered を background で起動 (tmux + terminal)
#   観測専用だった dashboard を control plane として完結させる。
# --------------------------------------------------------------------------- #
MCP_HTTP_URL = _env_text(
    "AGENTSTACK_MCP_URL", "http://127.0.0.1:8765/mcp"
)
SPAWN_SCRIPT = _env_path(
    "AGENTSTACK_SPAWN_SCRIPT",
    os.path.join(HOOKS_DIR, "spawn_child.sh"),
)
SOURCE_REPO = HERE  # vault 外、自前 git の親 repo
# UI radio と必ず一致させる。program はモデル文字列から決定。
_SPAWN_MODELS = {
    "claude-sonnet-5": ("claude-code", "claude-sonnet-5"),
    "claude-opus-5": ("claude-code", "claude-opus-5"),
    "claude-haiku-4-5-20251001": ("claude-code", "claude-haiku-4-5-20251001"),
}
_CODEX_DEFAULT_MODEL = "gpt-5.6-sol"
_CODEX_DEFAULT_MODELS = (
    _CODEX_DEFAULT_MODEL,
    "gpt-5.6-terra",
    "gpt-5.6-luna",
)
_CODEX_EFFORTS = ("low", "medium", "high", "xhigh")
SPAWN_SCIENTISTS_SCRIPT = os.path.join(os.path.dirname(HERE), "bin", "lib", "agentstack-scientists.sh")


def _codex_models() -> list[str]:
    """Return the installer's Codex model allow-list (comma-separated override)."""
    models = [value.strip() for value in os.environ.get("AGENTSTACK_CODEX_MODELS", "").split(",") if value.strip()]
    return models or list(_CODEX_DEFAULT_MODELS)


def _agent_name_comparison_key(name: str) -> str:
    """Normalize only for identity comparisons across stock/local servers.

    Stock mcp-agent-mail preserves ``Adjective-Scientist`` while some local
    deployments deterministically remove the hyphen.  API calls, tmux names,
    and credential paths must keep the register_agent read-back verbatim; this
    helper is deliberately limited to occupancy/duplicate comparisons.
    """
    return (name or "").replace("-", "").casefold()


def _spawn_name_status(name: str) -> str:
    """Return available/occupied/unknown; database failures fail closed."""
    if not name or not os.path.exists(DB_PATH):
        return "unknown"
    comparison_key = _agent_name_comparison_key(name)
    try:
        with _db() as con:
            registered_names = (
                row[0] for row in con.execute("SELECT name FROM agents")
                if isinstance(row[0], str)
            )
            occupied = any(
                _agent_name_comparison_key(registered) == comparison_key
                for registered in registered_names
            )
        return "occupied" if occupied else "available"
    except sqlite3.Error:
        return "unknown"


def _spawn_name_vocabulary() -> tuple[list[str], list[str]]:
    """Load the launcher-owned scientist/adjective vocabulary once."""
    try:
        output = subprocess.run(
            [
                "bash", "-c",
                'source "$1" && ags_scientist_list && printf "\\036" && '
                'ags_adjective_list',
                "suggest-name", SPAWN_SCIENTISTS_SCRIPT,
            ],
            capture_output=True, text=True, timeout=3, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return [], []
    scientists_raw, separator, adjectives_raw = output.partition("\036")
    if not separator:
        return [], []
    scientists = [
        line.strip() for line in scientists_raw.splitlines() if line.strip()
    ]
    adjectives = [
        line.strip() for line in adjectives_raw.splitlines() if line.strip()
    ]
    return scientists, adjectives


def suggest_spawn_name(scientist: str, attempts: int = 20) -> str | None:
    """Pick an available Adjective-Scientist name; unknown fails closed."""
    if not re.fullmatch(r"[A-Za-z]{2,63}", scientist or ""):
        return None
    scientists, adjectives = _spawn_name_vocabulary()
    if scientist not in scientists:
        return None
    for adjective in secrets.SystemRandom().sample(adjectives, min(attempts, len(adjectives))):
        candidate = f"{adjective}-{scientist}"
        if _spawn_name_status(candidate) == "available":
            return candidate
    return None


def _suggest_any_spawn_name(attempts: int = 75) -> str | None:
    """Generate a safe explicit name for AUTO spawn instead of MCP auto-name.

    A stock server can return a separator-less auto-name that is later coerced
    on token-bearing re-registration.  Supplying an available hyphenated name
    keeps registration idempotent, while read-back still follows local servers
    that remove the separator.
    """
    scientists, adjectives = _spawn_name_vocabulary()
    candidates = [
        f"{adjective}-{scientist}"
        for adjective in adjectives
        for scientist in scientists
    ]
    for candidate in secrets.SystemRandom().sample(
            candidates, min(attempts, len(candidates))):
        if _spawn_name_status(candidate) == "available":
            return candidate
    return None


def _spawn_roots() -> list[str]:
    raw_roots = os.environ.get("AGENTSTACK_SPAWN_ROOTS", "").split(":")
    roots = raw_roots if any(raw_roots) else [os.path.expanduser("~")]
    return [os.path.realpath(os.path.expanduser(root)) for root in roots if os.path.isdir(os.path.expanduser(root))]


def _is_within_root(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def spawn_directory_suggestions(raw_path: str) -> dict:
    """List visible child directories without allowing traversal outside configured roots."""
    roots = _spawn_roots()
    raw_path = (raw_path or "").strip()
    if not roots or any(part == ".." for part in raw_path.split(os.sep)):
        return {"path": None, "dirs": []}
    target = os.path.realpath(os.path.expanduser(raw_path or roots[0]))
    allowed_roots = [root for root in roots if _is_within_root(target, root)]
    if not allowed_roots or not os.path.isdir(target):
        return {"path": None, "dirs": []}
    root = max(allowed_roots, key=len)
    dirs: list[dict[str, str]] = []
    try:
        with os.scandir(target) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                try:
                    real_entry = os.path.realpath(entry.path)
                    if entry.is_dir(follow_symlinks=True) and _is_within_root(real_entry, root):
                        dirs.append({"name": entry.name, "path": real_entry})
                except OSError:
                    continue
    except OSError:
        return {"path": target, "dirs": []}
    dirs.sort(key=lambda item: item["name"].lower())
    return {"path": target, "dirs": dirs[:20], "truncated": len(dirs) > 20}


_SPAWN_STATUS_CACHE: dict = {"ts": 0.0, "key": None, "data": {}}
_SPAWN_STATUS_TTL = 4.5
_SPAWN_STATUS_LOCK = threading.Lock()


def _spawn_scientist_statuses(
        adjectives: list[str], scientists: list[str]) -> dict[str, str]:
    """Report whether each scientist has at least one free adjective pairing.

    Read the agent roster once, then evaluate every dynamically-loaded
    adjective/scientist combination in Python.  This remains one SQL query even
    when the vocabulary grows beyond SQLite's traditional parameter limit.
    """
    if not adjectives or not scientists or not os.path.exists(DB_PATH):
        return {scientist: "unknown" for scientist in scientists}
    try:
        db_mtime = os.stat(DB_PATH).st_mtime_ns
    except OSError:
        return {scientist: "unknown" for scientist in scientists}
    key = (DB_PATH, db_mtime, tuple(adjectives), tuple(scientists))
    now = time.monotonic()
    with _SPAWN_STATUS_LOCK:
        if (_SPAWN_STATUS_CACHE["key"] == key
                and now - _SPAWN_STATUS_CACHE["ts"] < _SPAWN_STATUS_TTL):
            return dict(_SPAWN_STATUS_CACHE["data"])
    try:
        with _db() as con:
            occupied = {
                _agent_name_comparison_key(row[0])
                for row in con.execute("SELECT name FROM agents")
                if isinstance(row[0], str)
            }
    except sqlite3.Error:
        statuses = {scientist: "unknown" for scientist in scientists}
    else:
        statuses = {
            scientist: (
                "available"
                if any(
                    _agent_name_comparison_key(
                        f"{adjective}-{scientist}") not in occupied
                    for adjective in adjectives
                )
                else "occupied"
            )
            for scientist in scientists
        }
    with _SPAWN_STATUS_LOCK:
        _SPAWN_STATUS_CACHE.update(ts=now, key=key, data=statuses)
    return dict(statuses)


def spawn_names_payload() -> dict:
    """Picker data; scientist vocabulary is emitted by the launcher source."""
    try:
        output = subprocess.run(
            ["bash", "-c", 'source "$1" && ags_adjective_list && printf "\\036" && ags_scientist_list', "spawn-names", SPAWN_SCIENTISTS_SCRIPT],
            capture_output=True, text=True, timeout=3, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as e:
        raise ValueError(f"scientist vocabulary unavailable: {e}") from e
    adjective_text, _, scientist_text = output.partition("\x1e")
    adjectives = [line.strip() for line in adjective_text.splitlines() if line.strip()]
    scientists = [line.strip() for line in scientist_text.splitlines() if line.strip()]
    statuses = _spawn_scientist_statuses(adjectives, scientists)
    raw_dirs = os.environ.get("AGENTSTACK_SPAWN_DIRS", "").split(":")
    # Keep `~` symbolic in the API; do_spawn expands it only at launch time.
    dirs = [value for value in raw_dirs if value] or ["~"]
    return {
        "names": [{"name": name, "portrait": bool(_portrait_file(name, False)),
                   "status": statuses.get(name, "unknown")} for name in scientists],
        "adjectives": adjectives,
        "naming": "adjective-scientist",
        "dirs": dirs,
        "models": list(_SPAWN_MODELS),
        "default_model": "claude-sonnet-5",
        "providers": [
            {"id": "claude", "label": "Claude", "program": "claude-code", "models": list(_SPAWN_MODELS), "default_model": "claude-sonnet-5", "efforts": None},
            {"id": "codex", "label": "Codex", "program": "codex-cli", "models": _codex_models(), "default_model": _codex_models()[0], "efforts": list(_CODEX_EFFORTS), "effort_default": "xhigh"},
        ],
    }


def _mcp_bearer() -> str:
    """agent-mail .env から HTTP_BEARER_TOKEN を読む。空なら ''。"""
    if not os.path.exists(MAIL_ENV_PATH):
        return ""
    try:
        with open(MAIL_ENV_PATH, encoding="utf-8") as f:
            for line in f:
                ln = line.strip()
                if not ln or ln.startswith("#"):
                    continue
                if ln.startswith("HTTP_BEARER_TOKEN"):
                    _, _, v = ln.partition("=")
                    return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _legacy_mail_bearer_enabled() -> bool:
    """Select the legacy transport credential without changing its default."""
    if MAIL_HTTP_BEARER_MODE == "enabled":
        return True
    if MAIL_HTTP_BEARER_MODE == "disabled":
        return False
    if MAIL_HTTP_BEARER_MODE == "auto":
        return True
    raise ValueError(
        "AGENTSTACK_MAIL_HTTP_BEARER_MODE must be auto, enabled, or disabled"
    )


def _mcp_jsonrpc(method: str, params: dict, timeout: int = 15) -> dict:
    """Send one selected-authority MCP request and normalize its envelope."""
    try:
        bearer_enabled = _legacy_mail_bearer_enabled()
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    token = _mcp_bearer() if bearer_enabled else ""
    if bearer_enabled and not token:
        return {"ok": False, "error": "HTTP_BEARER_TOKEN missing (~/mcp_agent_mail/.env)"}
    payload = json.dumps({
        # MCP servers may cache/replay duplicate JSON-RPC ids.  A fixed id made
        # sequential register/send calls consume an unrelated prior response.
        "jsonrpc": "2.0", "id": secrets.token_hex(8), "method": method,
        "params": params,
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        MCP_HTTP_URL, data=payload,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    for line in raw.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            break
    try:
        body = json.loads(raw)
    except ValueError as e:
        return {"ok": False, "error": f"invalid JSON-RPC response: {e}"}
    if isinstance(body, dict) and body.get("error"):
        return {"ok": False, "error": str(body["error"])}
    if not isinstance(body, dict) or "result" not in body:
        return {"ok": False, "error": "JSON-RPC response has no result"}
    return {"ok": True, "result": body["result"]}


_MCP_TOOL_PARAMETER_CACHE: dict[tuple[str, str], set[str]] = {}


def _mcp_tool_parameters(tool: str) -> set[str] | None:
    """Read the live tool schema so strict and lenient servers both work."""
    key = (MCP_HTTP_URL, tool)
    if key in _MCP_TOOL_PARAMETER_CACHE:
        return _MCP_TOOL_PARAMETER_CACHE[key]
    listing = _mcp_jsonrpc("tools/list", {})
    if not listing["ok"]:
        return None
    result = listing.get("result")
    if not isinstance(result, dict):
        return None
    for definition in result.get("tools") or []:
        if not isinstance(definition, dict) or definition.get("name") != tool:
            continue
        properties = (
            (definition.get("inputSchema") or {}).get("properties") or {}
        )
        allowed = set(properties)
        _MCP_TOOL_PARAMETER_CACHE[key] = allowed
        return allowed
    return None


def _mcp_call(method: str, args: dict, timeout: int = 15) -> dict:
    """Call one agent-mail tool, shaping arguments to its advertised schema."""
    allowed = _mcp_tool_parameters(method)
    prepared = args if allowed is None else {
        key: value for key, value in args.items() if key in allowed
    }
    response = _mcp_jsonrpc(
        "tools/call", {"name": method, "arguments": prepared}, timeout
    )
    if not response["ok"]:
        return response
    result = response.get("result")
    if not isinstance(result, dict):
        return {"ok": False, "error": "unexpected tools/call result"}
    if result.get("isError"):
        text = " ".join(
            str(block.get("text", ""))
            for block in result.get("content") or []
            if isinstance(block, dict)
        ).strip()
        return {"ok": False, "error": text or "agent-mail tool failed"}
    # Newer MCP servers expose the decoded payload in structuredContent.
    try:
        data = result.get("structuredContent")
        if not isinstance(data, dict):
            text = result["content"][0]["text"]
            try:
                data = json.loads(text)
            except ValueError:
                lowered = text.lower()
                if "error calling tool" in lowered or "validation error" in lowered:
                    return {"ok": False, "error": text.strip()}
                data = {"text": text}
    except (KeyError, IndexError, TypeError, ValueError) as e:
        return {"ok": False, "error": f"unexpected response shape: {e}"}
    return {"ok": True, "data": data}


def _runtime_agent_token(agent_name: str) -> str:
    """Read one exact runtime owner-token file without following symlinks."""
    if not _valid(agent_name):
        return ""
    token_key = re.sub(r"[^A-Za-z0-9_.-]", "_", agent_name)
    path = os.path.join(RUNTIME_DIR, f"agent_token_{token_key}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, encoding="utf-8") as token_file:
            token = token_file.read(4097).strip()
    except OSError:
        return ""
    return token if token and len(token) <= 4096 else ""


def do_spawn(payload: dict) -> dict:
    """spawn フォーム payload から子エージェントを spawn して child name を返す。

    payload: {parent?, standalone?, name?, dir?, role?, group?, task,
              provider?, model?, effort?}.
    """
    if "standalone" in payload and not isinstance(payload["standalone"], bool):
        return {"ok": False, "error": "standalone must be boolean"}
    standalone = payload.get("standalone", False)
    parent = (payload.get("parent") or "").strip()
    task = (payload.get("task") or "").strip()
    role = (payload.get("role") or "").strip()[:40]
    group = (payload.get("group") or "").strip()[:24]
    provider = (payload.get("provider") or "claude").strip().lower()
    model = (payload.get("model") or ("claude-sonnet-5" if provider == "claude" else _codex_models()[0])).strip()
    effort = (payload.get("effort") or "").strip().lower()
    worktree = bool(payload.get("worktree"))
    worktree_base = (payload.get("worktree_base") or "").strip()
    requested_name = (payload.get("name") or "").strip()
    work_dir = os.path.expanduser((payload.get("dir") or SOURCE_REPO).strip())

    if not standalone and (not parent or _NAME_RE.fullmatch(parent) is None):
        return {"ok": False, "error": "parent name invalid"}
    if standalone:
        parent = ""
    if not task:
        return {"ok": False, "error": "task description required"}
    if provider == "claude":
        if model not in _SPAWN_MODELS:
            return {"ok": False, "error": f"model not allowed for provider claude: {model}"}
        if effort:
            return {"ok": False, "error": "effort not supported for provider: claude"}
        program, model_str = _SPAWN_MODELS[model]
    elif provider == "codex":
        if model not in _codex_models():
            return {"ok": False, "error": f"model not allowed for provider codex: {model}"}
        effort = effort or "xhigh"
        if effort not in _CODEX_EFFORTS:
            return {"ok": False, "error": f"effort not allowed for provider codex: {effort}"}
        program, model_str = "codex-cli", model
    else:
        return {"ok": False, "error": f"provider not allowed: {provider}"}
    if requested_name and re.fullmatch(
            r"[A-Z][A-Za-z]{1,63}(?:-[A-Z][A-Za-z]{1,63})?",
            requested_name) is None:
        return {"ok": False, "error": "name invalid"}
    if requested_name and _spawn_name_status(requested_name) != "available":
        return {"ok": False, "error": "name is occupied or cannot be verified"}
    if not os.path.isdir(work_dir):
        return {"ok": False, "error": f"dir does not exist: {work_dir}"}
    if not os.path.exists(SPAWN_SCRIPT):
        return {"ok": False, "error": f"spawn script missing: {SPAWN_SCRIPT}"}
    project_key = _project_key()
    if not project_key:
        return {"ok": False, "error": "AGENTSTACK_PROJECT_KEY or AGENTSTACK_VAULT is not configured"}

    if not requested_name:
        requested_name = _suggest_any_spawn_name() or ""
        if not requested_name:
            return {"ok": False,
                    "error": "no available agent name could be verified"}

    task_short = task[:80]

    # 1) Always send an explicit hyphenated request name.  The response name
    # is authoritative and may be separator-less on a local patched server.
    child_token = secrets.token_urlsafe(32)
    reg = _mcp_call("register_agent", {
        "project_key": project_key,
        "program": program,
        "model": model_str,
        "task_description": task_short,
        "registration_token": child_token,
        "name": requested_name,
    })
    if not reg["ok"]:
        return {"ok": False,
                "error": f"register_agent failed: {reg.get('error')}"}
    registration = reg["data"] or {}
    child_name = registration.get("name", "")
    if not child_name or _NAME_RE.fullmatch(child_name) is None:
        return {"ok": False,
                "error": f"invalid child name from register: {child_name!r}"}
    server_token = registration.get("registration_token", "")
    if not isinstance(server_token, str):
        server_token = ""
    effective_child_token = server_token.strip() or child_token
    name_substituted = child_name != requested_name
    if name_substituted:
        logging.warning("spawn register normalized requested name %r to %r", requested_name, child_name)
        # A log line nobody reads is how this stayed invisible. Persist it so
        # the agent carries the discrepancy in the UI for as long as it exists.
        _record_name_substitution(child_name, requested_name)

    # 2) role/emoji/group annotation (best-effort, failure is non-fatal)
    annot_status = "skipped"
    if role or group:
        try:
            ar = _write_annotation(child_name, role, "", group)
            annot_status = "ok" if ar.get("ok") else f"fail:{ar.get('error')}"
        except Exception as e:  # noqa: BLE001
            annot_status = f"err:{e}"

    def retained_registration_error(error: str, **extra) -> dict:
        """Report a post-registration failure without pretending it rolled back.

        The dashboard's service credential can create registrations, but it is
        intentionally not used as an owner credential to delete them.  Keep
        this explicit so callers do not retry and silently create more junk
        identities.
        """
        result = {
            "ok": False,
            "error": (
                f"{error}; child registration '{child_name}' remains because "
                "the dashboard server has no permission to delete it"
            ),
            "child_name": child_name,
            "requested_name": requested_name,
            "name_substituted": name_substituted,
            "annot": annot_status,
            "registration_retained": True,
        }
        result.update(extra)
        return result

    # Registration defaults are contact-gated on stock agent-mail. Match the
    # normal launcher path and open the new child before delivering its task.
    # The live schema removes registration_token for lenient builds that do
    # not accept it, while strict builds receive the server-issued credential.
    contact = _mcp_call("set_contact_policy", {
        "project_key": project_key,
        "agent_name": child_name,
        "policy": "open",
        "registration_token": effective_child_token,
    })
    if not contact["ok"]:
        return retained_registration_error(
            f"set_contact_policy failed: {contact.get('error')}")

    # 3) Normal children receive the task through agent-mail.  Standalone
    # children have no parent/sender, so the launcher receives the full task
    # directly and no synthetic self-mail is created.
    if not standalone:
        parent_token = _runtime_agent_token(parent)
        if not parent_token:
            return retained_registration_error(
                f"parent registration token unavailable for '{parent}'")
        subject = f"タスク依頼: {task[:50]}"
        body_lines = [
            "> [via dashboard +NEW AGENT]",
            "",
            "## 依頼内容", "", task, "",
            "## 補足", "",
            f"- 親エージェント: {parent}",
            "- spawn 元: dashboard `+ NEW AGENT` (POST /api/spawn)",
            f"- mcp-agent-mail の project_key は `{project_key}` を使うこと "
            "(cwd ではない、特に worktree モード時は必須)",
        ]
        if worktree:
            body_lines += [
                f"- 分離 worktree モードで起動 (branch: exp/{child_name})",
                f"- worktree base: {worktree_base or 'HEAD'}",
                f"- worktree dir: /tmp/cc-worktrees/{child_name}",
            ]
        body_lines += [
            "- 完了したら親に reply してください。",
        ]
        snd = _mcp_call("send_message", {
            "project_key": project_key,
            "sender_name": parent,
            "to": [child_name],
            "cc": [parent],
            "subject": subject,
            "body_md": "\n".join(body_lines),
            "importance": "high",
            "sender_token": parent_token,
        })
        if not snd["ok"]:
            return retained_registration_error(
                f"send_message failed: {snd.get('error')}")

    # 4) spawn_child.sh --pre-registered を background で起動
    token_file = ""
    token_created = False
    log_fh = None

    def remove_spawn_credentials() -> None:
        """Remove both the one-shot handoff and any launcher-persisted copies."""
        nonlocal token_created
        paths = []
        if token_created and token_file:
            paths.append(token_file)
        token_key = re.sub(r"[^A-Za-z0-9_.-]", "_", child_name)
        paths.extend([
            os.path.join(RUNTIME_DIR, f"agent_token_{token_key}"),
            os.path.join(RUNTIME_DIR, "child-agents", f"{child_name}.json"),
        ])
        for path in paths:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as e:
                logging.warning(
                    "failed to remove failed-spawn credential %s: %s", path, e)
        token_created = False

    def kill_spawn_session() -> None:
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", f"={child_name}"],
                capture_output=True, text=True)
        except OSError:
            pass

    token_dir = os.path.join(RUNTIME_DIR, "spawn-tokens")
    try:
        os.makedirs(token_dir, mode=0o700, exist_ok=True)
        os.chmod(token_dir, 0o700)
        token_file = os.path.join(
            token_dir, f"{child_name}.{secrets.token_hex(8)}.token")
        open_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            open_flags |= os.O_NOFOLLOW
        fd = os.open(token_file, open_flags, 0o600)
        token_created = True
        with os.fdopen(fd, "w") as f:
            f.write(effective_child_token)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(token_file, 0o600)
    except Exception as e:  # noqa: BLE001
        remove_spawn_credentials()
        return retained_registration_error(f"spawn token write failed: {e}")

    args = [SPAWN_SCRIPT, "--pre-registered", child_name, "--child-token-file", token_file]
    if standalone:
        args.append("--standalone")
    if provider == "codex":
        args.append("--codex")
    args.extend(["--model", model_str])
    if provider == "codex":
        args.extend(["--effort", effort])
    if worktree:
        args.append("--worktree")
        if worktree_base:
            args.extend(["--worktree-base", worktree_base])
    args.extend([task[:4000] if standalone else task_short, work_dir])
    env = os.environ.copy()
    if standalone:
        env.pop("PARENT_AGENT", None)
    else:
        env["PARENT_AGENT"] = parent
    env["PROJECT_KEY"] = project_key
    # launchd の最小 PATH には ~/.local/bin が無く、spawn_child.sh が tmux 内で
    # 起動する `zsh -lc` は非対話シェルのため ~/.zshrc を source せず claude が
    # PATH に乗らない (cold start で claude 即落ち → tmux session が cleanup-
    # child-agent.sh で kill され "can't find pane" になる)。ここで明示的に
    # ~/.local/bin を先頭に挿しておく (Claude CLI の標準インストール位置)。
    home_local = os.path.expanduser("~/.local/bin")
    current_path = env.get("PATH", "")
    if home_local not in current_path.split(":"):
        env["PATH"] = f"{home_local}:{current_path}" if current_path else home_local
    try:
        log_dir = os.path.join(HERE, "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "spawn.log")
        log_fh = open(log_path, "ab", buffering=0)
        ts = datetime.now(timezone.utc).isoformat()
        log_fh.write(
            f"\n=== {ts} spawn child={child_name} parent={parent} "
            f"provider={provider} model={model_str} effort={effort or '-'} "
            f"standalone={standalone} worktree={worktree} ===\n".encode())
        proc = subprocess.Popen(args, stdout=log_fh, stderr=log_fh, env=env,
                                start_new_session=True)

        # The launcher performs readiness/death detection and consumes the
        # one-shot token before returning.  Wait for that verdict instead of
        # treating a briefly-created tmux session as success.
        if hasattr(proc, "wait"):
            try:
                returncode = proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
                kill_spawn_session()
                remove_spawn_credentials()
                return retained_registration_error(
                    "spawn launcher did not finish readiness checks within 120s")
            if returncode != 0:
                kill_spawn_session()
                remove_spawn_credentials()
                tail = ""
                try:
                    with open(log_path, encoding="utf-8", errors="replace") as f:
                        tail = f.read()[-1000:]
                except OSError:
                    pass
                return retained_registration_error(
                    f"spawn launcher exited with status {returncode}",
                    detail=tail)

        probe = subprocess.run(
            ["tmux", "has-session", "-t", f"={child_name}"],
            capture_output=True, text=True)
        if probe.returncode:
            kill_spawn_session()
            remove_spawn_credentials()
            tail = ""
            try:
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    tail = f.read()[-1000:]
            except OSError:
                pass
            return retained_registration_error(
                "spawn launcher exited before a live tmux session was created",
                detail=tail)
    except Exception as e:  # noqa: BLE001
        kill_spawn_session()
        remove_spawn_credentials()
        return retained_registration_error(f"spawn launch failed: {e}")
    finally:
        if log_fh is not None:
            log_fh.close()

    return {
        "ok": True,
        "child_name": child_name,
        "requested_name": requested_name,
        "name_substituted": name_substituted,
        "tmux_session": child_name,
        "annot": annot_status,
        "worktree": worktree,
        "standalone": standalone,
        "provider": provider,
        "model": model_str,
        "effort": effort or None,
    }


def do_exit(session: str) -> dict:
    """running/finished エージェントに `/exit` を送り、Claude を graceful exit させる。

    安全弁:
      - session 名 _valid() 必須、warm-*/pending-* は拒否
      - build_agents() の category を信頼源に running/finished のみ許可（gone は拒否）
      - _has_session() で tmux session が実在することを確認
      - attached=True は拒否せず、actions に警告を含める
    """
    if not _valid(session):
        return {"ok": False, "error": "invalid session name"}
    if session.startswith("warm-") or session.startswith("pending-"):
        return {"ok": False, "error": "warmup/pending sessions are protected"}

    target = None
    try:
        for r in build_agents():
            if r["name"] == session:
                target = r
                break
    except Exception as e:
        return {"ok": False, "error": f"failed to enumerate agents: {e}"}
    if target is None:
        return {"ok": False, "error": f"agent '{session}' not found"}
    if target["category"] not in ("agent", "finished"):
        return {"ok": False,
                "error": f"agent '{session}' category={target['category']} "
                         "- only running/finished are exitable",
                "category": target.get("category")}
    if not _has_session(session):
        return {"ok": False, "error": f"tmux session '{session}' not found"}

    actions = []
    if target.get("attached"):
        actions.append("warn-attached")

    # pane で動いているプロセスを確認
    # Claude Code は Python プロセス → "Python" / シェルゾンビは "zsh"/"bash" 等
    _SHELL_PROCS = {"zsh", "bash", "sh", "fish", "csh", "tcsh", "dash"}
    pane_cmd_r = subprocess.run(
        ["tmux", "display-message", "-t", session, "-p", "#{pane_current_command}"],
        capture_output=True, text=True,
    )
    pane_cmd = pane_cmd_r.stdout.strip().lower()

    if pane_cmd in _SHELL_PROCS:
        # Claude はすでに終了してシェルだけ残っているゾンビ状態。
        # /exit はシェルに効かないので shell の exit コマンドで tmux session を閉じる。
        r = subprocess.run(
            ["tmux", "send-keys", "-t", session, "exit", "Enter"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return {"ok": False, "error": f"tmux send-keys failed: {r.stderr.strip()}"}
        actions.append("shell-exit-sent")
        actions.append(f"zombie-pane:{pane_cmd}")
    else:
        r = subprocess.run(
            ["tmux", "send-keys", "-t", session, "-l", "/exit"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return {"ok": False, "error": f"tmux send-keys failed: {r.stderr.strip()}"}
        time.sleep(0.3)
        r = subprocess.run(
            ["tmux", "send-keys", "-t", session, "C-m"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return {"ok": False, "error": f"tmux send-keys failed: {r.stderr.strip()}"}
        actions.append("exit-sent")

    return {"ok": True, "session": session, "actions": actions}


# --------------------------------------------------------------------------- #
# Mail health (notify-daemon + watcher 可観測性)
# --------------------------------------------------------------------------- #
NOTIFY_STATE_FILE = os.path.join(RUNTIME_DIR, "notify-state.json")
SIGNAL_PROJECTS_DIR = os.path.join(SIGNALS_DIR, "projects")
_MAIL_HEALTH_CACHE: dict = {"ts": 0.0, "data": None}


def _slugify_project_key(project_key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", project_key.lower()).strip("-")


def _signal_agent_dirs() -> list[str]:
    project_key = _project_key()
    if project_key:
        return [os.path.join(SIGNAL_PROJECTS_DIR,
                             _slugify_project_key(project_key), "agents")]
    try:
        return [
            os.path.join(entry.path, "agents")
            for entry in os.scandir(SIGNAL_PROJECTS_DIR)
            if entry.is_dir()
        ]
    except OSError:
        return []


def _pidfile_process_running(
    pidfile: str,
    expected_command_marker: str,
    heartbeat_file: str = "",
) -> tuple[bool, int | None]:
    try:
        with open(pidfile, encoding="utf-8") as handle:
            pid = int(handle.readline().strip())
    except (OSError, ValueError):
        return False, None
    if pid <= 1:
        return False, None
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False, None

    command = ""
    proc_cmdline = f"/proc/{pid}/cmdline"
    try:
        with open(proc_cmdline, "rb") as handle:
            command = handle.read(65536).replace(b"\0", b" ").decode(
                "utf-8", errors="replace"
            )
    except OSError:
        try:
            process = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if process.returncode == 0:
                command = process.stdout
        except Exception:
            command = ""
    if command:
        if expected_command_marker not in command:
            return False, None
    else:
        try:
            heartbeat_age = time.time() - os.path.getmtime(heartbeat_file)
        except OSError:
            return False, None
        if heartbeat_age < 0 or heartbeat_age > 45:
            return False, None
    return True, pid


def _launchctl_job_running(label: str) -> bool:
    try:
        process = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True,
            text=True,
            timeout=3,
        )
        match = re.search(r'"?PID"?\s*=\s*(\d+)', process.stdout)
        return (
            process.returncode == 0
            and match is not None
            and int(match.group(1)) > 1
        )
    except Exception:
        return False


def mail_watcher_health() -> dict:
    now = time.time()
    cached = _MAIL_HEALTH_CACHE["data"]
    if cached is not None and now - _MAIL_HEALTH_CACHE["ts"] < 5:
        return cached

    result: dict = {
        "ok": True,
        "ts": int(now),
        "last_success_ts": None,
        "last_success_age_s": None,
        "recent_results": {},
        "signal_count": 0,
        "daemon_running": False,
        "watcher_running": False,
        "watcher_mode": None,
        "watcher_pid": None,
        "status": "unknown",
    }

    if os.path.exists(NOTIFY_STATE_FILE):
        try:
            with open(NOTIFY_STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
            latest_success = 0
            counts: dict[str, int] = {}
            cutoff = now - 600
            for entry in state.values():
                lr = entry.get("last_result", "")
                la = entry.get("last_attempt_epoch", 0) or 0
                if la >= cutoff:
                    counts[lr] = counts.get(lr, 0) + 1
                ls = entry.get("last_success_epoch", 0) or 0
                if ls > latest_success:
                    latest_success = ls
            result["recent_results"] = counts
            if latest_success:
                result["last_success_ts"] = latest_success
                result["last_success_age_s"] = int(now - latest_success)
        except (json.JSONDecodeError, OSError):
            pass

    try:
        count = 0
        for signal_dir in _signal_agent_dirs():
            if not os.path.isdir(signal_dir):
                continue
            for agent_dir in os.scandir(signal_dir):
                if not agent_dir.is_dir():
                    continue
                for f in os.scandir(agent_dir.path):
                    if f.name.endswith(".signal"):
                        count += 1
        result["signal_count"] = count
    except OSError:
        pass

    # 配送本体は mail-watcher に統合。GUI launchd domain が使えない環境でも
    # watcher 自身が持つ pidfile と command line を照合して実プロセスを判定する。
    watcher_launchd = _launchctl_job_running(MAIL_WATCHER_LABEL)
    watcher_pidfile, watcher_pid = _pidfile_process_running(
        MAIL_WATCHER_PIDFILE,
        "watch_agent_mail_signals.sh",
        MAIL_WATCHER_HEARTBEAT,
    )
    result["watcher_running"] = watcher_launchd or watcher_pidfile
    if watcher_launchd:
        result["watcher_mode"] = "launchd"
    elif watcher_pidfile:
        result["watcher_mode"] = "pidfile"
        result["watcher_pid"] = watcher_pid
    result["daemon_running"] = _launchctl_job_running(NOTIFY_DAEMON_LABEL)

    age = result.get("last_success_age_s")
    signals = result["signal_count"]
    watcher = result["watcher_running"]
    # red:    watcher 不在 / signal 大量滞留 (>50, 通常は配送時に即 unlink される)
    # yellow: watcher 生存だが pending signal が捌けていない (滞留 + 直近配送なし
    #         = 配送が遅れている兆候)
    # green:  それ以外。watcher 生存かつ (backlog 無し or 直近配送あり)。トラフィック
    #         が無いだけの健全 idle も green とし、誤警報 (健全なのに yellow) を防ぐ。
    if not watcher or signals > 50:
        result["status"] = "red"
    elif signals > 0 and (age is None or age > 120):
        result["status"] = "yellow"
    else:
        result["status"] = "green"

    _MAIL_HEALTH_CACHE.update(ts=now, data=result)
    return result


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # サイレント
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            try:
                with open(INDEX_HTML, "rb") as f:
                    self._send(
                        200,
                        _render_dashboard_index(f.read()),
                        "text/html; charset=utf-8",
                    )
            except FileNotFoundError:
                self._send(500, b"index.html missing", "text/plain")
        elif path in THEME_ASSETS:
            asset_path, content_type = THEME_ASSETS[path]
            try:
                with open(asset_path, "rb") as file:
                    self._send(200, file.read(), content_type)
            except FileNotFoundError:
                self._send(404, b"theme asset missing", "text/plain")
        elif path == "/api/version":
            version = _resolve_version()
            self._send(200, json.dumps({"name": "claude-agent-stack", "version": version, "api": 1}).encode(), "application/json; charset=utf-8")
        elif path == "/api/spawn-names":
            try:
                self._send(200, json.dumps(spawn_names_payload()).encode(), "application/json; charset=utf-8")
            except ValueError as e:
                self._send(503, json.dumps({"ok": False, "error": str(e)}).encode(), "application/json; charset=utf-8")
        elif path == "/api/name-status":
            query = parse_qs(urlparse(self.path).query)
            name = (query.get("name") or [""])[0]
            self._send(200, json.dumps({"name": name, "status": _spawn_name_status(name)}).encode(), "application/json; charset=utf-8")
        elif path == "/api/suggest-name":
            query = parse_qs(urlparse(self.path).query)
            scientist = (query.get("scientist") or [""])[0]
            name = suggest_spawn_name(scientist)
            payload = {"name": name} if name else {"error": "no available name found"}
            self._send(200 if name else 409, json.dumps(payload).encode(), "application/json; charset=utf-8")
        elif path == "/api/fs/dirs":
            query = parse_qs(urlparse(self.path).query)
            payload = spawn_directory_suggestions((query.get("path") or [""])[0])
            self._send(200, json.dumps(payload).encode(), "application/json; charset=utf-8")
        elif path == "/api/agents":
            body = json.dumps(
                {"ts": int(time.time()), "agents": build_agents()},
                ensure_ascii=False,
            ).encode()
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/custom-portraits":
            self._send(
                200,
                json.dumps(
                    _custom_portrait_map(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode(),
                "application/json; charset=utf-8",
            )
        elif path == "/api/graph":
            q = parse_qs(urlparse(self.path).query)
            show_all = (q.get("all") or ["0"])[0] in ("1", "true")
            try:
                days = float((q.get("days") or ["4"])[0])
            except ValueError:
                days = 4.0
            # spawn_only=1: for callers that only need the parent/child
            # lineage.  The ORRERY cockpit is one — it was pulling the full
            # node+edge payload every 6s just to read `spawn` out of it.
            spawn_only = (q.get("spawn_only") or ["0"])[0] in ("1", "true")
            try:
                payload = graph_payload(days, show_all)
            except Exception as e:  # noqa: BLE001
                payload = {"nodes": [], "edges": [], "spawn": [],
                           "error": f"{type(e).__name__}: {e}",
                           "timestamp_diagnostics": {
                               "invalid_count": 0, "fields": {},
                           },
                           "degraded": True}
            if spawn_only:
                payload = {"nodes": [], "edges": [],
                           "spawn": payload.get("spawn", []),
                           "spawn_only": True,
                           **({"error": payload["error"]}
                              if payload.get("error") else {}),
                           "timestamp_diagnostics": payload.get(
                               "timestamp_diagnostics",
                               {"invalid_count": 0, "fields": {}},
                           ),
                           "degraded": bool(payload.get("degraded"))}
            payload["ts"] = int(time.time())
            self._send(
                200,
                json.dumps(payload, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
            )
        elif path == "/api/history":
            q = parse_qs(urlparse(self.path).query)
            sess = (q.get("session") or [""])[0]
            try:
                lim = int((q.get("limit") or ["220"])[0])
            except ValueError:
                lim = 220
            payload = history_payload(sess, lim)
            self._send(
                200 if payload.get("ok") else 400,
                json.dumps(payload, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
            )
        elif path == "/api/agent-history":
            # Task E: 単一 agent の sparkline (?name=)
            # Task G: 複数 agent union replay  (?names=A,B,C)
            # Task G+: hours 省略時は events range に auto-fit
            q = parse_qs(urlparse(self.path).query)
            nm = (q.get("name") or [""])[0]
            names_raw = (q.get("names") or [""])[0]
            hrs_raw = (q.get("hours") or [""])[0]
            if hrs_raw == "":
                hrs = None
            else:
                try:
                    hrs = int(hrs_raw)
                except ValueError:
                    hrs = 24
            pane_raw = (q.get("include_pane_states") or [""])[0].lower()
            include_pane = pane_raw in ("1", "true", "yes", "on")
            if names_raw:
                names_lst = [s.strip() for s in names_raw.split(",") if s.strip()]
                payload = agent_history_payload(
                    hours=hrs, names=names_lst,
                    include_pane_states=include_pane,
                )
            else:
                payload = agent_history_payload(
                    name=nm, hours=hrs,
                    include_pane_states=include_pane,
                )
            self._send(
                200 if payload.get("ok") else 400,
                json.dumps(payload, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
            )
        elif path == "/api/edge-messages":
            q = parse_qs(urlparse(self.path).query)
            a = (q.get("a") or [""])[0]
            b = (q.get("b") or [""])[0]
            try:
                lim = int((q.get("limit") or ["60"])[0])
            except ValueError:
                lim = 60
            payload = edge_messages_payload(a, b, lim)
            self._send(
                200 if payload.get("ok") else 400,
                json.dumps(payload, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
            )
        elif path == "/api/messages-since":
            q = parse_qs(urlparse(self.path).query)
            try:
                since = int((q.get("since") or ["0"])[0])
            except ValueError:
                since = 0
            try:
                lim = int((q.get("limit") or ["80"])[0])
            except ValueError:
                lim = 80
            payload = messages_since_payload(since, lim)
            self._send(
                200,
                json.dumps(payload, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
            )
        elif path == "/api/annotations":
            self._send(
                200,
                json.dumps({"ok": True, "annotations": _annotations()},
                           ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
            )
        elif path == "/api/deliverables":
            q = parse_qs(urlparse(self.path).query)
            ag = (q.get("agent") or [""])[0]
            items = _deliverables_index().get(ag, []) if ag else []
            vault_name = os.path.basename(os.path.normpath(VAULT)) if VAULT else ""
            self._send(
                200,
                json.dumps(
                    {"ok": True, "agent": ag, "vault": vault_name, "items": items[:25]},
                    ensure_ascii=False,
                ).encode(),
                "application/json; charset=utf-8",
            )
        elif path.startswith("/assets/"):
            # 静的アセット配信。assets/ 直下の安全な拡張子のみ許可（path traversal 防止）。
            fname = path[len("/assets/"):]
            if "/" in fname or ".." in fname or not fname:
                self._send(404, b"not found", "text/plain")
                return
            if not (fname.endswith(".svg") or fname.endswith(".png")):
                self._send(404, b"not found", "text/plain")
                return
            fp = os.path.join(HERE, "assets", fname)
            try:
                with open(fp, "rb") as f:
                    data = f.read()
            except OSError:
                self._send(404, b"missing", "text/plain")
                return
            ct = "image/svg+xml" if fname.endswith(".svg") else "image/png"
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/portrait":
            q = parse_qs(urlparse(self.path).query)
            nm = (q.get("name") or [""])[0]
            hi = (q.get("hi") or ["0"])[0] in ("1", "true")
            fp = _portrait_file(nm, hi)
            if not fp:
                if not re.fullmatch(r"[A-Za-z][A-Za-z ._-]{0,23}", nm or ""):
                    self._send(404, b"no portrait", "text/plain")
                    return
                img = _portrait_fallback(nm, hi)
                self._send(200, img, "image/svg+xml")
                return
            try:
                with open(fp, "rb") as f:
                    img = f.read()
            except OSError:
                self._send(404, b"missing", "text/plain")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(img)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(img)
        elif path == "/api/ptty":
            q = parse_qs(urlparse(self.path).query)
            sess = (q.get("session") or [""])[0]
            payload = ttyd_ensure(sess)
            self._send(
                200 if payload.get("ok") else 400,
                json.dumps(payload, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
            )
        elif path == "/api/term":
            q = parse_qs(urlparse(self.path).query)
            sess = (q.get("session") or [""])[0]
            try:
                ln = int((q.get("lines") or ["500"])[0])
            except ValueError:
                ln = 500
            payload = term_capture(sess, ln)
            self._send(
                200 if payload.get("ok") else 400,
                json.dumps(payload, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
            )
        elif path == "/api/mail-watcher-health":
            payload = mail_watcher_health()
            self._send(
                200,
                json.dumps(payload, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
            )
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/jump", "/api/kill", "/api/exit",
                        "/api/annotate", "/api/spawn", "/api/reactivate",
                        "/api/jserr"):
            self._send(404, b"not found", "text/plain")
            return

        # All POST endpoints mutate local tmux/agent state.  Browsers must prove
        # same-origin, while CLI clients remain usable by omitting both browser
        # provenance headers.  Requiring JSON prevents simple-form CSRF.
        content_type = self.headers.get_content_type().lower()
        if content_type != "application/json":
            self._send(
                415,
                json.dumps({"ok": False,
                            "error": "Content-Type must be application/json"}).encode(),
                "application/json; charset=utf-8",
            )
            return
        origin = (self.headers.get("Origin") or "").strip()
        fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if origin or fetch_site:
            expected_origin = f"http://{self.headers.get('Host', '')}"
            if ((origin and origin != expected_origin)
                    or (fetch_site and fetch_site != "same-origin")):
                self._send(
                    403,
                    json.dumps({"ok": False,
                                "error": "cross-origin POST rejected"}).encode(),
                    "application/json; charset=utf-8",
                )
                return
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            self._send(
                400,
                json.dumps({"ok": False,
                            "error": "invalid Content-Length"}).encode(),
                "application/json; charset=utf-8",
            )
            return
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw or b"{}")
        except (TypeError, ValueError):
            self._send(
                400,
                json.dumps({"ok": False, "error": "invalid JSON body"}).encode(),
                "application/json; charset=utf-8",
            )
            return
        if not isinstance(body, dict):
            self._send(
                400,
                json.dumps({"ok": False,
                            "error": "JSON body must be an object"}).encode(),
                "application/json; charset=utf-8",
            )
            return
        session = body.get("session", "")
        if path == "/api/jump":
            result = do_jump(session)
        elif path == "/api/exit":
            result = do_exit(session)
        elif path == "/api/annotate":
            # 役割ラベルの upsert/削除。name は session でも name でも受ける。
            nm = body.get("name") or body.get("session") or ""
            result = _write_annotation(
                nm, body.get("role", ""), body.get("emoji", ""),
                body.get("group", ""))
        elif path == "/api/jserr":
            result = _log_js_error(body)
        elif path == "/api/spawn":
            result = do_spawn(body)
        elif path == "/api/reactivate":
            result = do_reactivate(session)
        else:
            mode = body.get("mode", "both")
            result = do_kill(session, mode)
        self._send(
            200 if result.get("ok") else 400,
            json.dumps(result, ensure_ascii=False).encode(),
            "application/json; charset=utf-8",
        )


# --------------------------------------------------------------------------- #
# do_reactivate — 生きているのに retired にされた agent を受信可能に戻す。
#
# agent-mail は 24 時間無活動の agent を毎時 retire する。終了した session に
# は妥当な掃除だが、**生きたまま idle だった常駐 agent**（司令塔・監視役）も
# 一緒に retire される。そして retired agent は送信と自分の inbox 読取は
# 素通りし、**受信だけが黙って拒否される** ので、本人も人間も気づけない。
# 他 agent のメールが bounce して初めて分かる。
#
# resume はこれを直せない。resume は「tmux が無い = 終了済み」を前提にした
# 復元路で、生きた session に当てても attach するだけで再登録は走らない。
# 直すには会話を捨てて再起動するしかなかった。
#
# ここは dashboard にしかできない仕事である。tmux が生きているかどうかを
# 知っているのは agent-mail ではなくこちら側だから。自動では戻さない:
# 黙って直すのは、今日一日で 4 つの形で踏んだ失敗そのものなので。
# --------------------------------------------------------------------------- #
def _mail_web_url(path: str) -> str:
    """agent-mail の web API を、設定済み endpoint と同じ host:port で叩く。

    ここは `http://127.0.0.1:8765` を直書きしていた。既定ポートで動いている
    限り正しく、それ以外では retire が黙って失敗する——「動いている環境では
    気づけない」種類の前提で、今日直したものと同じ形である。MCP endpoint が
    どこを指しているかは分かっているので、そこから導く。
    """
    base = MCP_HTTP_URL or "http://127.0.0.1:8765/mcp"
    parts = urllib.parse.urlsplit(base)
    return urllib.parse.urlunsplit((parts.scheme or "http", parts.netloc, path, "", ""))


def do_reactivate(session: str) -> dict:
    if not _valid(session):
        return {"ok": False, "error": "invalid session name"}
    if not _has_retired_at():
        return {"ok": False,
                "error": "this agent-mail has no retired_at column; "
                         "nothing can be retired on it"}
    project_key = _project_key()
    if not project_key:
        return {"ok": False,
                "error": "AGENTSTACK_PROJECT_KEY or AGENTSTACK_VAULT is not configured"}
    if not _has_session(session):
        return {"ok": False,
                "error": f"agent '{session}' has no live tmux session; "
                         "use resume to restore a finished one"}
    con = None
    try:
        con = _db()
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT a.id, a.retired_at FROM agents a "
            "JOIN projects p ON a.project_id=p.id "
            "WHERE a.name=? AND p.human_key=?",
            (session, project_key),
        ).fetchone()
    except Exception as e:
        return {"ok": False, "error": f"failed to read agent record: {e}"}
    finally:
        if con is not None:
            con.close()
    if row is None:
        return {"ok": False, "error": f"agent '{session}' not found in agent-mail"}
    if not row["retired_at"]:
        return {"ok": False, "error": f"agent '{session}' is not retired"}

    req = urllib.request.Request(
        _mail_web_url("/mail/api/unretire-agent"),
        data=json.dumps({"agent_id": row["id"]}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            payload = json.loads(r.read())
    except Exception as e:
        return {"ok": False, "error": f"unretire request failed: {e}"}
    if not payload.get("success"):
        return {"ok": False, "error": f"unretire refused: {payload}"}
    return {"ok": True, "session": session, "action": "reactivated"}


def _start_supervisor_watchdog():
    supervisor_fd_raw = os.environ.pop("AGENTSTACK_DASHBOARD_SUPERVISOR_FD", "")
    if not supervisor_fd_raw:
        return
    try:
        supervisor_fd = int(supervisor_fd_raw)
    except ValueError:
        return
    if supervisor_fd < 0:
        return

    def _watch_supervisor():
        try:
            while os.read(supervisor_fd, 4096):
                pass
        except OSError:
            pass
        finally:
            try:
                os.close(supervisor_fd)
            except OSError:
                pass
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_watch_supervisor, daemon=True).start()


JS_ERROR_LOG = os.path.join(HERE, "logs", "js-errors.log")


def _log_js_error(body: dict) -> dict:
    """Land a JS exception raised inside the UI, one JSON object per line.

    The dashboard is often viewed inside a webview with no devtools (ORRERY),
    where a thrown exception is invisible: it only shows up as "clicking that
    does nothing".  This gives such a failure somewhere to land, so a user can
    hand over one log line instead of a symptom.
    """
    try:
        os.makedirs(os.path.dirname(JS_ERROR_LOG), exist_ok=True)
        line = json.dumps(
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "where": str(body.get("where", ""))[:80],
                "msg": str(body.get("msg", ""))[:500],
                "src": str(body.get("src", ""))[:200],
                "line": body.get("line"),
                "col": body.get("col"),
                "stack": str(body.get("stack", ""))[:1500],
                "ua": str(body.get("ua", ""))[:120],
            },
            ensure_ascii=False,
        )
        with open(JS_ERROR_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def main():
    _start_supervisor_watchdog()

    # 前回(SIGKILL 等で atexit 未実行)の野良 ttyd を掃除してから開始
    subprocess.run(
        ["pkill", "-f", "ttyd -p .* tmux attach -t ="],
        capture_output=True,
    )
    threading.Thread(target=_ttyd_reaper, daemon=True).start()
    srv = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    print(f"agent-dashboard listening on http://{BIND_HOST}:{PORT}/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
