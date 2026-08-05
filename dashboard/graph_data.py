import json
import math
import os
import sqlite3
from datetime import datetime, timezone


def _env_path(name: str, default: str = "") -> str:
    value = os.environ.get(name, default).strip()
    return os.path.expanduser(value) if value else ""


DB_PATH = _env_path("AGENTSTACK_MAIL_DB", "~/mcp_agent_mail/storage.sqlite3")
# agent-mail project human_key。未設定なら PROJECT_ID fallback で degrade する。
PROJECT_HUMAN_KEY = (
    os.environ.get("AGENTSTACK_PROJECT_KEY", "").strip()
    or _env_path("AGENTSTACK_VAULT", "")
)
PROJECT_ID = 1  # フォールバック既定（projects に human_key 不一致のとき）

# 「活発度」の集計窓（秒）。DB 内の最新メッセージ時刻を基準にする。
ACT_WINDOW_SEC = 6 * 3600


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
    """Normalize agent-mail timestamps to whole UTC epoch seconds.

    Rust agent-mail stores numeric timestamps as Unix microseconds.  Legacy
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
    """project human_key から project id を解決。
    見つからなければ PROJECT_ID(=1) にフォールバック。"""
    if not PROJECT_HUMAN_KEY:
        return PROJECT_ID
    try:
        row = con.execute(
            "SELECT id FROM projects WHERE human_key = ?",
            (PROJECT_HUMAN_KEY,),
        ).fetchone()
        if row and row[0] is not None:
            return int(row[0])
    except sqlite3.Error:
        pass
    return PROJECT_ID


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
        PROJECT_ID = _resolve_project_id(con)

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
