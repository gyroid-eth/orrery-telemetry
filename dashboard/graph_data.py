import json
import os
import sqlite3
from datetime import datetime


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

# 「活発度」の集計窓（秒）。DB 内の最新メッセージ時刻を基準にした
# 相対窓なので TZ 非依存（_to_epoch のローカル解釈オフセットが相殺される）。
ACT_WINDOW_SEC = 6 * 3600


def _to_epoch(ts_str: str) -> int:
    if not ts_str:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(datetime.strptime(ts_str, fmt).timestamp())
        except ValueError:
            pass
    return 0


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
    if not os.path.exists(DB_PATH):
        return {"nodes": [], "edges": [], "spawn": []}
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()
        # 設定された project human_key から project id を解決する。
        PROJECT_ID = _resolve_project_id(con)

        # activity: 直近 ACT_WINDOW_SEC のメッセージ参加数（送信+受信）を
        # エージェント別に集計。窓は DB 内最新メッセージ時刻からの相対なので
        # _to_epoch のローカル解釈オフセットが相殺され TZ 非依存。
        act_map: dict[str, int] = {}
        cur.execute(
            "SELECT MAX(created_ts) AS mx FROM messages WHERE project_id = ?",
            (PROJECT_ID,),
        )
        mx_row = cur.fetchone()
        latest = _to_epoch(mx_row["mx"]) if mx_row and mx_row["mx"] else 0
        if latest:
            cutoff = datetime.fromtimestamp(
                latest - ACT_WINDOW_SEC
            ).strftime("%Y-%m-%d %H:%M:%S")
            cur.execute(
                """
                SELECT nm, SUM(c) AS c FROM (
                    SELECT sa.name AS nm, COUNT(*) AS c
                    FROM messages m
                    JOIN agents sa ON sa.id = m.sender_id
                    WHERE m.project_id = ? AND m.created_ts >= ?
                    GROUP BY sa.name
                    UNION ALL
                    SELECT ra.name AS nm, COUNT(*) AS c
                    FROM messages m
                    JOIN message_recipients mr ON mr.message_id = m.id
                    JOIN agents ra ON ra.id = mr.agent_id
                    WHERE m.project_id = ? AND m.created_ts >= ?
                    GROUP BY ra.name
                ) GROUP BY nm
                """,
                (PROJECT_ID, cutoff, PROJECT_ID, cutoff),
            )
            act_map = {r["nm"]: r["c"] for r in cur.fetchall()}

        # nodes
        cur.execute(
            """
            SELECT name, model, program, task_description,
                   retired_at, last_active_ts
            FROM agents
            WHERE project_id = ?
            """,
            (PROJECT_ID,),
        )
        nodes = [
            {
                "name": r["name"],
                "model": r["model"],
                "program": r["program"],
                "task": r["task_description"] or "",
                "retired": r["retired_at"] is not None,
                "last_active": _to_epoch(r["last_active_ts"]),
                "act": act_map.get(r["name"], 0),
            }
            for r in cur.fetchall()
        ]

        # edges: sender → recipient aggregated
        cur.execute(
            """
            SELECT
                sa.name AS source,
                ra.name AS target,
                COUNT(*) AS cnt,
                MAX(m.created_ts) AS last_ts,
                mr.kind
            FROM messages m
            JOIN message_recipients mr ON mr.message_id = m.id
            JOIN agents sa ON sa.id = m.sender_id
            JOIN agents ra ON ra.id = mr.agent_id
            WHERE m.project_id = ?
              AND sa.project_id = ?
              AND ra.project_id = ?
            GROUP BY sa.name, ra.name, mr.kind
            """,
            (PROJECT_ID, PROJECT_ID, PROJECT_ID),
        )
        edges = [
            {
                "source": r["source"],
                "target": r["target"],
                "count": r["cnt"],
                "last_ts": _to_epoch(r["last_ts"]),
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
        cur.execute(
            """
            SELECT ra.name AS child, sa.name AS parent,
                   m.created_ts AS ts, m.importance AS imp,
                   ra.inception_ts AS c_inc, sa.inception_ts AS p_inc
            FROM messages m
            JOIN message_recipients mr ON mr.message_id = m.id
            JOIN agents ra ON ra.id = mr.agent_id
            JOIN agents sa ON sa.id = m.sender_id
            WHERE m.project_id = ?
              AND sa.id <> mr.agent_id
            ORDER BY m.created_ts ASC
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
                and r["p_inc"] < r["c_inc"]   # 同形式文字列の辞書順=時刻順
            ):
                spawn.append(
                    {"source": r["parent"], "target": child,
                     "type": "spawn"}
                )

        return {"nodes": nodes, "edges": edges, "spawn": spawn}

    finally:
        con.close()


if __name__ == "__main__":
    print(json.dumps(build_graph(), ensure_ascii=False))
