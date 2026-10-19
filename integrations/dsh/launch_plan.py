"""dsh 専用argv/envの純粋生成。ファイル・鍵・processは読み書きしない。

Node取り違え・env継承は通常起こり得、空成功や鍵漏れの害が大きい。
実体・owner・proxy・process・local admissionの検査は製品の共通部品が担う。
このmoduleの版/digestは呼出し元の検証済み入力で、実体検査の代用ではない。
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import PurePosixPath
import re


class LaunchPlanError(ValueError):
    """通常の起動に必要な入力が不足又は矛盾している。"""


@dataclass(frozen=True)
class FilePin:
    path: str
    sha256: str


@dataclass(frozen=True)
class Limits:
    session_requests: int = 3
    turn_requests: int = 3
    max_output_tokens: int = 2048
    timeout_seconds: int = 120


@dataclass(frozen=True)
class LaunchInput:
    node: FilePin
    node_version: str
    dsh_entry: FilePin
    guard_entry: FilePin
    patch_path: str
    home: str
    worktree: str
    state_root: str
    attempt_id: str
    model: str = "deepseek-flash"
    effort: str = "low"
    limits: Limits = Limits()


@dataclass(frozen=True)
class LaunchPlan:
    argv: tuple[str, ...]
    cwd: str
    public_env: dict[str, str]
    patch_rows: tuple[dict, ...]
    pins: tuple[FilePin, ...]


def _absolute(path: str) -> str:
    if type(path) is not str or not PurePosixPath(path).is_absolute() or "\x00" in path:
        raise LaunchPlanError("起動pathは明示した絶対pathで指定してください")
    return path


def _pin(pin: FilePin) -> None:
    _absolute(pin.path)
    if type(pin.sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", pin.sha256):
        raise LaunchPlanError("固定実体のSHA-256が必要です")


def _limits(limits: Limits) -> dict:
    values = {
        "session_requests": limits.session_requests,
        "turn_requests": limits.turn_requests,
        "max_output_tokens": limits.max_output_tokens,
        "timeout_seconds": limits.timeout_seconds,
    }
    if any(type(v) is not int or v <= 0 for v in values.values()):
        raise LaunchPlanError("要求・出力・時間の上限は正の整数で指定してください")
    return {**values, "retry_count": 0, "fallback": False,
            "token_control": "post_usage_stop"}


def build_launch(value: LaunchInput) -> LaunchPlan:
    """共有launcherが検証後の入力から呼ぶ。shellやambient envを使わない。"""
    for pin in (value.node, value.dsh_entry, value.guard_entry):
        _pin(pin)
    if type(value.node_version) is not str:
        raise LaunchPlanError("検証済みNode版が必要です")
    version = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value.node_version)
    if not version or tuple(map(int, version.groups())) < (24, 2, 0):
        raise LaunchPlanError("Node 24.2以上の固定実体が必要です")
    for path in (value.patch_path, value.home, value.worktree, value.state_root):
        _absolute(path)
    if not value.attempt_id or type(value.attempt_id) is not str:
        raise LaunchPlanError("attempt IDが必要です")
    if type(value.model) is not str or not value.model.strip():
        raise LaunchPlanError("明示したmodelが必要です")
    # 通常のthinking選択で不正patchを作らないよう、固定provider schemaと揃える。
    if value.effort not in {"off", "low", "high", "max"}:
        raise LaunchPlanError("effortが対応する値ではありません")
    limits = _limits(value.limits)
    state = PurePosixPath(value.state_root)
    env = {
        "PATH": f"{PurePosixPath(value.node.path).parent}:/usr/bin:/bin",
        # HOMEは元の値。隔離先は専用の明示rootで渡す。
        "HOME": value.home,
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "TERM": "xterm-256color",
        "DSH_HOME": str(state / "dsh"),
        "DSH_TUI_SESSION_ROOT": str(state / "sessions"),
        "NODE_COMPILE_CACHE": str(state / "node-cache"),
        # ORRERY_*は後続のmanaged entry/pluginとの契約。上流既存envではない。
        "ORRERY_DSH_TUI_DATA_ROOT": str(state / "tui"),
        "ORRERY_DSH_ATTEMPT_ID": value.attempt_id,
        "ORRERY_DSH_LIMITS": json.dumps(limits, sort_keys=True, separators=(",", ":")),
    }
    rows = (
        {"id": "session-title", "disabled": True},
        {"id": "session-title-llm", "disabled": True},
        {"id": "llm-retry", "disabled": True},
        {"id": "llm-deepseek", "config": {
            "apiKeyEnv": "DEEPSEEK_API_KEY",
            "baseURL": "https://api.deepseek.com/anthropic",
            "reasoningEffort": value.effort,
            "maxTokens": value.limits.max_output_tokens,
            "retryPolicy": {"mode": "normal", "maxRetries": 0},
        }},
        {"id": "agent-default-model", "config": {
            "provider": "deepseek-official", "model": value.model,
            "reasoningEffort": value.effort,
        }},
    )
    return LaunchPlan(
        argv=(value.node.path, "--import", value.guard_entry.path,
              value.dsh_entry.path, "--profile", "orrery-dsh-tui",
              "--patch", value.patch_path),
        cwd=value.worktree,
        public_env=env,
        patch_rows=rows,
        pins=(value.node, value.dsh_entry, value.guard_entry),
    )


def runtime_environment(plan: LaunchPlan, api_key: str) -> dict[str, str]:
    """検証後の最後の引渡し用。返却値はログ/保存せず、単一runtimeだけに渡す。"""
    if type(api_key) is not str or not api_key or "\x00" in api_key:
        raise LaunchPlanError("runtimeへ渡す鍵がありません")
    return {**plan.public_env, "DEEPSEEK_API_KEY": api_key}


def auxiliary_environment(plan: LaunchPlan) -> dict[str, str]:
    """補助process用の最小env。proxy envの生成は共通factoryを使う。"""
    return {key: plan.public_env[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TERM")}
