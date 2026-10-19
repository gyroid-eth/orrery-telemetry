"""dsh 専用の取得計画と確認データ。I/O や runtime の起動許可は扱わない。

通常の installer からは読み込まない。未選択なら空の計画を返す。
確認データの型・参照の一致は検査するが、人間の認証を証明しない。
将来の呼出し元は製品の確認経路を使い、owner/proxy/process/local gate は
共通部品へ委譲する。この module だけでは provider を有効化できない。
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re


class OptInError(ValueError):
    """取得計画又は確認データが閉じた契約に一致しない。"""


# 公開 package の固定版。取得・integrity 検証そのものは後続の installer が担う。
_PACKAGES = (
    (
        "@deepseek-ai/dsh",
        "0.2.1-rc.2",
        "https://registry.npmjs.org/@deepseek-ai/dsh/-/dsh-0.2.1-rc.2.tgz",
        "sha512-tvY+yhfKRifthbqPe/OK0AYxM5ZkvqfYq/jMYgJcEUxiRRL1Vwra3Ti5TB6AXgSWQdiPHYh/Vp6SJa9yHwMNuw==",
    ),
    (
        "@deepseek-harness-tui/dsh-tui",
        "0.16.1",
        "https://registry.npmjs.org/@deepseek-harness-tui/dsh-tui/-/dsh-tui-0.16.1.tgz",
        "sha512-xk8Nviy55RvLnAdjYj04DekUu6D1Pc0kEa5R+2EXoL3H/h6ZEzv8L0bxtSTrWxtNdXd4ufOXMu4TQ7HjrpYhbg==",
    ),
)


def build_plan(*, opt_in: bool = False) -> dict:
    """明示 bool から新しい計画を作る。env・HOME・設定・ネットワークは読まない。"""
    if type(opt_in) is not bool:
        raise OptInError("opt-in は明示的な bool で指定してください")
    return {
        "schema_version": 1,
        "provider": "dsh",
        "phase": "planned" if opt_in else "disabled",
        "downloads": [
            {"package": name, "version": version, "url": url, "integrity": integrity}
            for name, version, url, integrity in _PACKAGES
        ] if opt_in else [],
        # Node 自動取得は未実装。未選択なら前提条件の取得計画も作らない。
        "requirements": [{"runtime": "node", "minimum": "24.2.0",
                          "acquisition": "existing-pinned-binary"}] if opt_in else [],
    }


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise OptInError("計画は JSON の閉じたデータで指定してください") from exc


def _validate_plan(plan: object) -> dict:
    if type(plan) is not dict or type(plan.get("phase")) is not str:
        raise OptInError("取得計画の形式が一致しません")
    phase = plan["phase"]
    if phase not in {"disabled", "planned"}:
        raise OptInError("取得計画の phase が一致しません")
    expected = build_plan(opt_in=phase == "planned")
    # bool/int の相互等価や未知 field も許可しない。
    if _canonical(plan) != _canonical(expected):
        raise OptInError("取得計画が固定された内容から変わっています")
    return expected


def plan_digest(plan: object) -> str:
    """確認対象の固定内容を参照する digest。起動・owner の証明ではない。"""
    return hashlib.sha256(_canonical(_validate_plan(plan))).hexdigest()


def authorized_downloads(plan: object, confirmation: object = None) -> list[dict]:
    """取得してよい固定項目を返すだけ。取得・設定変更・有効化は行わない。"""
    checked = _validate_plan(plan)
    if checked["phase"] == "disabled":
        # 未選択では確認の解決も不要。呼出し元に取得項目を一つも渡さない。
        return []
    fields = {"schema_version", "decision", "plan_sha256", "approval_id",
              "actor", "confirmed_at"}
    if type(confirmation) is not dict or set(confirmation) != fields:
        raise OptInError("取得前の確認が必要です")
    if type(confirmation["schema_version"]) is not int or confirmation["schema_version"] != 1:
        raise OptInError("確認データの版が一致しません")
    if confirmation["decision"] != "approved":
        raise OptInError("取得は承認されていません")
    for field in ("approval_id", "actor"):
        value = confirmation[field]
        if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
            raise OptInError("確認データの識別子が一致しません")
    stamp = confirmation["confirmed_at"]
    if type(stamp) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamp):
        raise OptInError("確認時刻は UTC の秒単位で指定してください")
    try:
        datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise OptInError("確認時刻が不正です") from exc
    if confirmation["plan_sha256"] != plan_digest(checked):
        raise OptInError("確認対象と取得計画が一致しません")
    return checked["downloads"]
