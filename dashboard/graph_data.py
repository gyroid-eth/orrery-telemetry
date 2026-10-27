import json
import hashlib
import math
import secrets
import threading
from collections import OrderedDict
from contextlib import contextmanager
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from urllib.parse import urlparse


def _env_path(name: str, default: str = "") -> str:
    value = os.environ.get(name, default).strip()
    return os.path.expanduser(value) if value else ""


def _listener_mail_db() -> str:
    """Return the SQLite file opened by the configured live mail listener.

    Both the native and legacy databases can remain on disk after a migration,
    so file existence does not identify the active writer.  The installer
    normally supplies ``AGENTSTACK_MAIL_DB``; this read-only listener probe is
    for direct dashboard runs where that generated environment is absent.
    """
    endpoint = os.environ.get(
        "AGENTSTACK_MCP_URL", "http://127.0.0.1:18765/mcp"
    ).strip()
    try:
        port = urlparse(endpoint).port or 8765
    except ValueError:
        return ""
    lsof = shutil.which("lsof")
    if not lsof and os.path.isfile("/usr/sbin/lsof"):
        lsof = "/usr/sbin/lsof"
    if not lsof:
        return ""
    try:
        listeners = subprocess.run(
            [lsof, "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if listeners.returncode != 0:
        return ""
    databases: set[str] = set()
    for pid in listeners.stdout.split():
        if not pid.isdigit():
            continue
        try:
            opened = subprocess.run(
                [lsof, "-a", "-p", pid, "-Fn"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if opened.returncode != 0:
            continue
        for line in opened.stdout.splitlines():
            path = line[1:] if line.startswith("n") else ""
            if path.endswith(".sqlite3") and os.path.isfile(path):
                databases.add(os.path.realpath(path))
    return next(iter(databases)) if len(databases) == 1 else ""


def _default_mail_db() -> str:
    """Resolve the active listener before considering on-disk defaults.

    The installer passes AGENTSTACK_MAIL_DB, so this default only applies when
    the dashboard is run directly.  Prefer the database actually opened by the
    configured listener; only fall back to this stack's database when no live
    writer can be identified.
    """
    live = _listener_mail_db()
    if live:
        return live
    home = os.path.expanduser("~")
    return os.path.join(home, ".agentstack", "mail", "storage.sqlite3")


DB_PATH = _env_path("AGENTSTACK_MAIL_DB") or _default_mail_db()
# ORRERY Mail project human_key。未設定なら PROJECT_ID fallback で degrade する。
PROJECT_HUMAN_KEY = (
    os.environ.get("AGENTSTACK_PROJECT_KEY", "").strip()
    or _env_path("AGENTSTACK_VAULT", "")
)
PROJECT_ID = 1  # フォールバック既定（projects に human_key 不一致のとき）

# 「活発度」の集計窓（秒）。DB 内の最新メッセージ時刻を基準にする。
ACT_WINDOW_SEC = 6 * 3600


_LIVE_PARENT_CACHE: dict[str, object] = {"ts": 0.0, "value": None}
_LIVE_PARENT_TTL = 10.0


def _live_parents() -> dict[str, str]:
    """child session name -> parent agent name, read from live tmux sessions.

    The message-derived lineage below infers a parent from the child's oldest
    high-importance inbound message, which was the delegation mail. Spawning
    with the task embedded in the prompt removes that mail, and with it the
    only record that the child had a parent at all -- so a child stayed
    unattached until it happened to report back. The spawner exports
    PARENT_AGENT into the child's tmux session, which states the relationship
    directly and is available the moment the session exists.

    Reading it costs one tmux call per session -- 238ms of a 326ms graph build
    on a machine with 42 of them -- so the answer is cached briefly. Lineage
    only changes when something is spawned, and the node itself appears without
    waiting for this.
    """
    now = time.monotonic()
    cached = _LIVE_PARENT_CACHE["value"]
    if cached is not None and now - float(_LIVE_PARENT_CACHE["ts"]) < _LIVE_PARENT_TTL:
        return dict(cached)  # type: ignore[arg-type]
    tmux = shutil.which("tmux")
    if not tmux:
        _LIVE_PARENT_CACHE.update(ts=now, value={})
        return {}
    try:
        listing = subprocess.run(
            [tmux, "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if listing.returncode != 0:
        # No server, or one that was briefly unreachable -- the exit code does
        # not say which. Only successful listings are cached, so a momentary
        # failure cannot pin an empty lineage for the whole TTL. Retrying costs
        # one `list-sessions`; the expensive part is the per-session lookup
        # below, which this path never reaches.
        return {}
    parents: dict[str, str] = {}
    for session in listing.stdout.split("\n"):
        session = session.strip()
        if not session:
            continue
        try:
            shown = subprocess.run(
                [tmux, "show-environment", "-t", session, "PARENT_AGENT"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if shown.returncode != 0:
            continue                   # unset for this session
        value = shown.stdout.strip()
        # "-PARENT_AGENT" marks the variable as removed; only NAME=VALUE counts.
        if not value.startswith("PARENT_AGENT="):
            continue
        parent = value.split("=", 1)[1].strip()
        if parent:
            parents[session] = parent
    _LIVE_PARENT_CACHE.update(ts=now, value=dict(parents))
    return parents


class _TimestampDiagnostics:
    def __init__(self) -> None:
        self.fields: dict[str, int] = {}

    def invalid(self, field: str, count: int = 1) -> None:
        if count > 0:
            self.fields[field] = self.fields.get(field, 0) + count

    def payload(self) -> dict:
        return {
            "invalid_count": sum(self.fields.values()),
            "fields": dict(sorted(self.fields.items())),
        }


def _to_epoch(
    value: object,
    *,
    field: str = "timestamp",
    diagnostics: _TimestampDiagnostics | None = None,
) -> int | None:
    """Normalize ORRERY Mail timestamps to whole UTC epoch seconds.

    Rust ORRERY Mail stores numeric timestamps as Unix microseconds.  Legacy
    Python builds store ISO text.  Invalid non-empty values stay distinct from
    the real Unix epoch: callers receive ``None`` and diagnostics record the
    field instead of silently turning corruption into a plausible zero.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        if isinstance(value, bool):
            raise ValueError("boolean is not a timestamp")
        if isinstance(value, int):
            return value // 1_000_000
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("non-finite timestamp")
            return int(value / 1_000_000)
        if not isinstance(value, str):
            raise TypeError(f"unsupported timestamp type: {type(value).__name__}")
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (OSError, OverflowError, TypeError, ValueError):
        if diagnostics is not None:
            diagnostics.invalid(field)
        return None


def _sql_epoch(column: str) -> str:
    """SQLite expression matching :func:`_to_epoch` for DB-owned values."""
    return f"""CASE typeof({column})
        WHEN 'integer' THEN CAST({column} / 1000000 AS INTEGER)
        WHEN 'real' THEN CAST({column} / 1000000.0 AS INTEGER)
        WHEN 'text' THEN CAST(strftime('%s', {column}) AS INTEGER)
    END"""


def _sql_invalid(column: str) -> str:
    normalized = _sql_epoch(column)
    return f"""CASE
        WHEN {column} IS NULL THEN 0
        WHEN typeof({column}) = 'text' AND trim({column}) = '' THEN 0
        WHEN ({normalized}) IS NULL THEN 1
        ELSE 0
    END"""


def _has_column(con: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in con.execute(f"PRAGMA table_info({table})"))


def _resolve_project_id(con) -> int:
    """明示keyを解決し、未指定の場合だけ既定projectを使う。"""
    if not PROJECT_HUMAN_KEY:
        return PROJECT_ID
    row = con.execute(
        "SELECT id FROM projects WHERE human_key = ?",
        (PROJECT_HUMAN_KEY,),
    ).fetchone()
    if row and row[0] is not None:
        return int(row[0])
    raise LookupError("Mail project not found")


def mobile_mail_metadata(
    con: sqlite3.Connection, project_id: int, window: str,
    *, now: float | None = None, event_limit: int = 200, cursors=None,
) -> dict:
    """Read delivery metadata only; never select message content or change ACK.

    Counts are recipient rows by direction/kind in the requested event-time
    window. The bounded timeline is separate from those complete aggregates.
    Same-second rows sort by message id then recipient id, both descending.
    """
    try:
        from dashboard.mobile_projection import WINDOWS as windows
    except ModuleNotFoundError:
        from mobile_projection import WINDOWS as windows
    if window not in windows or not 1 <= event_limit <= 200:
        raise ValueError('invalid mobile Mail window or limit')
    checked = int(time.time() if now is None else now)
    lower = checked - windows[window] if windows[window] is not None else None
    epoch = _sql_epoch('m.created_ts')
    cte = f'''WITH delivery AS (
        SELECT m.id AS message_id, mr.agent_id AS recipient_id,
               sa.name AS source, ra.name AS target, mr.kind AS kind,
               {epoch} AS event_at
        FROM messages m
        JOIN message_recipients mr ON mr.message_id=m.id
        JOIN agents sa ON sa.id=m.sender_id AND sa.project_id=m.project_id
        JOIN agents ra ON ra.id=mr.agent_id AND ra.project_id=m.project_id
        WHERE m.project_id=? AND mr.kind IN ('to','cc')
    ) '''
    bounds = 'event_at IS NOT NULL AND event_at<=?'
    args = [project_id, checked]
    if lower is not None:
        bounds += ' AND event_at>=?'
        args.append(lower)
    # An extra row detects truncation without presenting a partial aggregate
    # as a complete graph. This limit also bounds the metadata HTTP response.
    edge_rows = con.execute(cte + f'''
        SELECT source,target,kind,COUNT(*) AS count,MAX(event_at) AS last_ts
        FROM delivery WHERE {bounds} GROUP BY source,target,kind
        ORDER BY count DESC,source,target,kind LIMIT 10001
    ''', args).fetchall()
    timeline = con.execute(cte + f'''
        SELECT message_id,recipient_id,source,target,kind,event_at
        FROM delivery WHERE {bounds}
        ORDER BY event_at DESC,message_id DESC,recipient_id DESC LIMIT ?
    ''', [*args, event_limit + 1]).fetchall()
    invalid = int(con.execute(cte + 'SELECT COUNT(*) FROM delivery WHERE event_at IS NULL',
                              [project_id]).fetchone()[0])
    edges = [dict(zip(('source','target','kind','count','last_ts'), row)) for row in edge_rows[:10000]]
    events = []
    for message_id, recipient_id, source, target, kind, event_at in timeline[:event_limit]:
        # Stable per-recipient-row key: does not expose Mail's internal ids.
        key = hashlib.sha256(f'{project_id}:{message_id}:{recipient_id}'.encode()).hexdigest()[:32]
        events.append({'key': key, 'source': source, 'target': target,
                       'kind': kind, 'event_at': event_at, 'count': 1})
    registry = MOBILE_MAIL_CURSORS if cursors is None else cursors
    scope = _mobile_scope(con, project_id)
    next_cursor = registry.issue(scope, window, tuple(timeline[event_limit-1][:2])+ (timeline[event_limit-1][5],),
                                 event_limit) if len(timeline)>event_limit else None
    return {'status': 'available', 'window': window, 'events': events, 'scope':scope,
            'next_cursor':next_cursor,
            'edges': edges, 'events_truncated': len(timeline) > event_limit,
            'edges_truncated': len(edge_rows) > 10000,
            'invalid_timestamps': invalid, 'degraded': bool(invalid or len(edge_rows) > 10000),
            'observed_at': checked}


class MobileMailCursors:
    """Opaque bounded process-local metadata positions, never body handles."""
    def __init__(self, *, clock=time.monotonic, limit=256, ttl=600):
        self.clock, self.limit, self.ttl = clock, limit, ttl
        self._items, self._lock = OrderedDict(), threading.Lock()

    def issue(self, scope, window, boundary, size=200, *, pair=None, direction='both', kind='mail', seen=0):
        with self._lock:
            self._expire()
            key=secrets.token_hex(16)
            self._items[key]=(self.clock()+self.ttl,scope,window,boundary,size,pair,direction,kind,seen)
            while len(self._items)>self.limit:self._items.popitem(last=False)
            return key

    def _expire(self):
        for key,value in list(self._items.items()):
            if value[0]<=self.clock():self._items.pop(key,None)

    def resolve(self,key,scope,window,*,pair=None,direction='both',kind='mail'):
        with self._lock:
            self._expire()
            value=self._items.get(key)
            if value is None or (value[1],value[2],value[5],value[6],value[7])!=(scope,window,pair,direction,kind):
                raise LookupError('mobile_cursor_expired')
            if kind=='cache':self._items.pop(key,None)
            return value[3],value[4],value[8]


MOBILE_MAIL_CURSORS=MobileMailCursors()
_MOBILE_NONCE=secrets.token_hex(16)


@contextmanager
def mobile_mail_read(con):
    """A read transaction and a two-second SQLite work budget, no Mail writes."""
    deadline=time.monotonic()+2
    own=not con.in_transaction
    if own:con.execute('BEGIN')
    con.set_progress_handler(lambda: int(time.monotonic()>deadline),1000)
    try:yield
    finally:
        con.set_progress_handler(None,0)
        if own:con.rollback()


def _mobile_scope(con,pid):
    db=next((row[2] for row in con.execute('PRAGMA database_list') if row[1]=='main'),'')
    if db:
        st=os.stat(db);identity=(os.path.realpath(db),st.st_dev,st.st_ino)
    else:identity=('memory',id(con))
    return hashlib.sha256(repr((_MOBILE_NONCE,pid,identity)).encode()).hexdigest()[:32]


def _mobile_delivery(con,pid,window,now,pair=None,direction='both'):
    try:from dashboard.mobile_projection import WINDOWS, name
    except ModuleNotFoundError:from mobile_projection import WINDOWS, name
    if window not in WINDOWS:raise ValueError('invalid_window')
    if direction not in {'both','forward','reverse'}:raise ValueError('invalid_direction')
    args=[pid,int(now)]
    bounds='event_at IS NOT NULL AND event_at<=?'
    if WINDOWS[window] is not None:
        bounds+=' AND event_at>=?';args.append(int(now)-WINDOWS[window])
    if pair is not None:
        if len(pair)!=2:raise ValueError('invalid_pair')
        a,b=map(name,pair)
        if a==b and direction!='both':raise ValueError('invalid_direction')
        if direction=='both':bounds+=' AND ((source=? AND target=?) OR (source=? AND target=?))';args.extend((a,b,b,a))
        else:
            bounds+=' AND source=? AND target=?';args.extend((a,b) if direction=='forward' else (b,a))
    elif direction!='both':raise ValueError('invalid_pair')
    cte=f'''WITH delivery AS (
        SELECT m.id AS message_id,mr.agent_id AS recipient_id,sa.name AS source,ra.name AS target,
               mr.kind AS kind,{_sql_epoch('m.created_ts')} AS event_at
        FROM messages m JOIN message_recipients mr ON mr.message_id=m.id
        JOIN agents sa ON sa.id=m.sender_id AND sa.project_id=m.project_id
        JOIN agents ra ON ra.id=mr.agent_id AND ra.project_id=m.project_id
        WHERE m.project_id=? AND mr.kind IN ('to','cc')) '''
    return cte,bounds,args


def _mobile_event(pid,row):
    mid,rid,source,target,kind,stamp=row
    return {'key':hashlib.sha256(f'{pid}:{mid}:{rid}'.encode()).hexdigest()[:32],
            'source':source,'target':target,'kind':kind,'event_at':stamp,'count':1}


def mobile_mail_page(con,pid,window,cursor=None,*,now=None,cursors=None,pair=None,direction='both'):
    now=time.time() if now is None else now
    registry=MOBILE_MAIL_CURSORS if cursors is None else cursors
    scope=_mobile_scope(con,pid)
    cte,bounds,args=_mobile_delivery(con,pid,window,now,pair,direction)
    size=200
    if cursor is not None:
        (mid,rid,stamp),size,_=registry.resolve(cursor,scope,window,pair=pair,direction=direction)
        bounds+=' AND (event_at,message_id,recipient_id)<(?,?,?)';args.extend((stamp,mid,rid))
    with mobile_mail_read(con):
        rows=con.execute(cte+f'''SELECT message_id,recipient_id,source,target,kind,event_at
            FROM delivery WHERE {bounds} ORDER BY event_at DESC,message_id DESC,recipient_id DESC LIMIT ?''',
            [*args,size+1]).fetchall()
    more=len(rows)>size
    last=rows[size-1] if more else None
    token=registry.issue(scope,window,(last[0],last[1],last[5]),size,pair=pair,direction=direction) if more else None
    return {'status':'available','scope':scope,'window':window,'events':[_mobile_event(pid,r) for r in rows[:size]],
            'next_cursor':token,'observed_at':int(now),'pair':list(pair) if pair else None,'direction':direction}


def _mobile_content(con,pid,mid):
    # Read bounded values only AFTER canonical project/delivery/period admission.
    row=con.execute('''SELECT typeof(subject),substr(subject,1,513),typeof(body_md),
        substr(CAST(body_md AS BLOB),1,65537),length(CAST(body_md AS BLOB))
        FROM messages WHERE id=? AND project_id=?''',(mid,pid)).fetchone()
    if row is None or row[0]!='text' or row[2]!='text':raise LookupError('mobile_content_unavailable')
    subject,raw=row[1],row[3]
    cut=raw[:65536]
    try:body=cut.decode('utf-8')
    except UnicodeDecodeError as exc:
        if len(raw)<=65536 or exc.end!=len(cut) or exc.reason!='unexpected end of data':raise ValueError('mobile_content_encoding') from None
        body=cut[:exc.start].decode('utf-8')
    value={'subject':subject[:512],'body':body,'subject_truncated':len(subject)>512,'body_truncated':row[4]>len(body.encode())}
    revision=hashlib.sha256(json.dumps([value,row[4]],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()[:32]
    return value,revision


def mobile_mail_detail(con,pid,window,key,*,mode='content',now=None):
    try:from dashboard.mobile_projection import EVENT_KEY
    except ModuleNotFoundError:from mobile_projection import EVENT_KEY
    if not isinstance(key,str) or not EVENT_KEY.fullmatch(key) or mode not in {'content','revision'}:raise ValueError('invalid_event')
    now=time.time() if now is None else now
    cte,bounds,args=_mobile_delivery(con,pid,window,now)
    scope=_mobile_scope(con,pid)
    con.create_function('mobile_event_key',2,lambda mid,rid:hashlib.sha256(f'{pid}:{mid}:{rid}'.encode()).hexdigest()[:32],deterministic=True)
    with mobile_mail_read(con):
        found=con.execute(cte+f'''SELECT message_id,recipient_id,source,target,kind,event_at
            FROM delivery WHERE {bounds} AND mobile_event_key(message_id,recipient_id)=? LIMIT 2''',[*args,key]).fetchall()
        if len(found)!=1:raise LookupError('mobile_event_unavailable')
        event=_mobile_event(pid,found[0]);value,revision=_mobile_content(con,pid,found[0][0])
    fingerprint=hashlib.sha256(f'{scope}:{found[0][0]}'.encode()).hexdigest()[:32]
    result={'scope':scope,'event':event,'fingerprint':fingerprint,'revision':revision}
    if mode=='content':result.update(value)
    return result


def mobile_mail_cache_page(con,pid,cursor=None,*,now=None,cursors=None,max_bytes=4*1024*1024):
    """Loopback-only bounded 24h distinct-message batch; never a relay route."""
    now=time.time() if now is None else now
    scope=_mobile_scope(con,pid);registry=MOBILE_MAIL_CURSORS if cursors is None else cursors
    cte,bounds,args=_mobile_delivery(con,pid,'day',now)
    seen=0
    if cursor:
        (mid,stamp),_,seen=registry.resolve(cursor,scope,'day',kind='cache')
        bounds+=' AND (event_at,message_id)<(?,?)';args.extend((stamp,mid))
    room=min(32,2000-seen)
    with mobile_mail_read(con):
        found=con.execute(cte+f'''SELECT message_id,MAX(recipient_id),source,target,kind,event_at
            FROM delivery WHERE {bounds} GROUP BY message_id
            ORDER BY event_at DESC,message_id DESC LIMIT ?''',[*args,room+1]).fetchall()
        items=[];last=None;reason=None
        for row in found[:room]:
            # MAX(recipient_id) with SQLite's single MAX selects the same row's
            # other columns, retaining one canonical to/cc delivery per message.
            value,revision=_mobile_content(con,pid,row[0])
            item={'scope':scope,'event':_mobile_event(pid,row),'revision':revision,
                  'fingerprint':hashlib.sha256(f'{scope}:{row[0]}'.encode()).hexdigest()[:32],**value}
            trial={'scope':scope,'items':[*items,item],'next_cursor':'f'*32,'complete':False,'reason':'count_limit'}
            if len(json.dumps(trial,separators=(',',':'),allow_nan=False).encode())>max_bytes-512:
                reason='serialized_batch_limit';break
            items.append(item);last=(row[0],row[5])
    more=len(found)>len(items)
    seen+=len(items)
    if more and seen>=2000:reason='count_limit'
    next_cursor=registry.issue(scope,'day',last,kind='cache',seen=seen) if more and last and seen<2000 else None
    return {'scope':scope,'items':items,'next_cursor':next_cursor,
            'complete':not more,'reason':reason,'observed_at':int(now)}


def build_graph() -> dict:
    diagnostics = _TimestampDiagnostics()
    if not os.path.exists(DB_PATH):
        return {
            "nodes": [], "edges": [], "spawn": [],
            "timestamp_diagnostics": diagnostics.payload(), "degraded": False,
        }
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()
        # 設定された project human_key から project id を解決する。
        try:
            PROJECT_ID = _resolve_project_id(con)
        except (LookupError, sqlite3.Error) as exc:
            return {
                "nodes": [], "edges": [], "spawn": [],
                "error": (
                    "Mail project not found"
                    if isinstance(exc, LookupError)
                    else "Mail project lookup failed (SQLite error)"
                ),
                "timestamp_diagnostics": diagnostics.payload(),
                "degraded": True,
            }

        # activity: 直近 ACT_WINDOW_SEC のメッセージ参加数（送信+受信）。
        # INTEGER microseconds と legacy ISO TEXT を同じ epoch seconds にしてから
        # MAX/filter する。raw MAX は mixed DB で TEXT を必ず勝たせてしまう。
        act_map: dict[str, int] = {}
        created_epoch = _sql_epoch("created_ts")
        created_invalid = _sql_invalid("created_ts")
        cur.execute(
            f"""SELECT MAX({created_epoch}) AS mx,
                       SUM({created_invalid}) AS invalid_cnt
                FROM messages WHERE project_id = ?""",
            (PROJECT_ID,),
        )
        mx_row = cur.fetchone()
        latest = mx_row["mx"] if mx_row and mx_row["mx"] is not None else None
        diagnostics.invalid(
            "messages.created_ts",
            int(mx_row["invalid_cnt"] or 0) if mx_row else 0,
        )
        if latest is not None:
            cutoff = latest - ACT_WINDOW_SEC
            message_epoch = _sql_epoch("m.created_ts")
            cur.execute(
                f"""
                WITH normalized_messages AS (
                    SELECT m.id, m.sender_id, {message_epoch} AS created_epoch
                    FROM messages m
                    WHERE m.project_id = ?
                )
                SELECT nm, SUM(c) AS c FROM (
                    SELECT sa.name AS nm, COUNT(*) AS c
                    FROM normalized_messages m
                    JOIN agents sa ON sa.id = m.sender_id
                    WHERE m.created_epoch >= ?
                    GROUP BY sa.name
                    UNION ALL
                    SELECT ra.name AS nm, COUNT(*) AS c
                    FROM normalized_messages m
                    JOIN message_recipients mr ON mr.message_id = m.id
                    JOIN agents ra ON ra.id = mr.agent_id
                    WHERE m.created_epoch >= ?
                    GROUP BY ra.name
                ) GROUP BY nm
                """,
                (PROJECT_ID, cutoff, cutoff),
            )
            act_map = {r["nm"]: r["c"] for r in cur.fetchall()}

        # nodes
        retired_select = (
            "retired_at" if _has_column(con, "agents", "retired_at")
            else "NULL AS retired_at"
        )
        cur.execute(
            f"""
            SELECT name, model, program, task_description,
                   {retired_select}, last_active_ts, inception_ts
            FROM agents
            WHERE project_id = ?
            """,
            (PROJECT_ID,),
        )
        nodes = []
        for r in cur.fetchall():
            last_active = _to_epoch(
                r["last_active_ts"],
                field="agents.last_active_ts",
                diagnostics=diagnostics,
            )
            # Spawn uses SQL-normalized inception values below. Parse each agent
            # once here solely so a malformed value remains observable rather
            # than disappearing from the lineage without explanation.
            _to_epoch(
                r["inception_ts"],
                field="agents.inception_ts",
                diagnostics=diagnostics,
            )
            nodes.append({
                "name": r["name"],
                "model": r["model"],
                "program": r["program"],
                "task": r["task_description"] or "",
                "retired": r["retired_at"] is not None,
                "last_active": last_active,
                "act": act_map.get(r["name"], 0),
            })

        # edges: first aggregate by identity ids, then re-aggregate by displayed
        # names. This retains the existing identity semantics without the slow
        # four-table name join on every message/recipient row.
        edge_epoch = _sql_epoch("m.created_ts")
        edge_invalid = _sql_invalid("m.created_ts")
        cur.execute(
            f"""
            WITH pair AS (
                SELECT m.sender_id AS s, mr.agent_id AS r, mr.kind AS kind,
                       COUNT(*) AS cnt,
                       MAX({edge_epoch}) AS last_ts,
                       SUM({edge_invalid}) AS invalid_cnt
                FROM messages m
                JOIN message_recipients mr ON mr.message_id = m.id
                WHERE m.project_id = ?
                GROUP BY m.sender_id, mr.agent_id, mr.kind
            )
            SELECT sa.name AS source, ra.name AS target,
                   SUM(p.cnt) AS cnt, MAX(p.last_ts) AS last_ts, p.kind,
                   SUM(p.invalid_cnt) AS invalid_cnt
            FROM pair p
            JOIN agents sa ON sa.id = p.s AND sa.project_id = ?
            JOIN agents ra ON ra.id = p.r AND ra.project_id = ?
            GROUP BY sa.name, ra.name, p.kind
            """,
            (PROJECT_ID, PROJECT_ID, PROJECT_ID),
        )
        edges = [
            {
                "source": r["source"],
                "target": r["target"],
                "count": r["cnt"],
                "last_ts": r["last_ts"],
                "kind": r["kind"],
            }
            for r in cur.fetchall()
        ]

        # spawn(親子): 各エージェントの「最古の inbound メッセージ」を見て、
        # それが high/urgent かつ 送信者が自分より古い(inception_ts が前)
        # なら その送信者を親とみなす。
        #   - 最古 inbound に限定 → spawn 時の委任タスク(子の最初の受信)を捕捉
        #   - high/urgent 限定 → 通常のやり取りを除外
        #   - 親 inception < 子 inception → 逆向き/相互エッジを構造的に排除
        #     （親は必ず子より先に存在する）
        message_epoch = _sql_epoch("m.created_ts")
        child_epoch = _sql_epoch("ra.inception_ts")
        parent_epoch = _sql_epoch("sa.inception_ts")
        cur.execute(
            f"""
            WITH inbound AS (
            SELECT ra.name AS child, sa.name AS parent,
                   {message_epoch} AS ts, m.importance AS imp,
                   {child_epoch} AS c_inc, {parent_epoch} AS p_inc,
                   m.id AS message_id
            FROM messages m
            JOIN message_recipients mr ON mr.message_id = m.id
            JOIN agents ra ON ra.id = mr.agent_id
            JOIN agents sa ON sa.id = m.sender_id
            WHERE m.project_id = ?
              AND sa.id <> mr.agent_id
            )
            SELECT child, parent, ts, imp, c_inc, p_inc
            FROM inbound
            WHERE ts IS NOT NULL
            ORDER BY ts ASC, message_id ASC
            """,
            (PROJECT_ID,),
        )
        spawn = []
        seen = set()
        # Live sessions first: PARENT_AGENT is the spawn relationship itself,
        # not a trace of it, so it holds for a child that has not spoken yet.
        node_names = {n["name"] for n in nodes}
        for child, parent in _live_parents().items():
            if child in node_names and parent in node_names and child != parent:
                seen.add(child)
                spawn.append(
                    {"source": parent, "target": child, "type": "spawn"}
                )
        for r in cur.fetchall():
            child = r["child"]
            if child in seen:
                continue            # ASC なので最初=最古 inbound
            seen.add(child)
            if (
                (r["imp"] or "").lower() in ("high", "urgent")
                and r["p_inc"] and r["c_inc"]
                and r["p_inc"] < r["c_inc"]
            ):
                spawn.append(
                    {"source": r["parent"], "target": child,
                     "type": "spawn"}
                )

        diagnostic_payload = diagnostics.payload()
        return {
            "nodes": nodes,
            "edges": edges,
            "spawn": spawn,
            "timestamp_diagnostics": diagnostic_payload,
            "degraded": diagnostic_payload["invalid_count"] > 0,
        }

    finally:
        con.close()


if __name__ == "__main__":
    print(json.dumps(build_graph(), ensure_ascii=False))
