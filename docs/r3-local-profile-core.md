# R3 ローカルモデル core — その1

[English](r3-local-profile-core.en.md) · [契約 fixture](r3-local-profile-fixtures.md)

`local:<name>` の入力検査、非秘密の intent と provider 設定への導出、共通 auth runner
を独立したモジュールにする段階。**通常の子の起動経路には未接続**であり、まだ
`spawn_child.sh --model local:...` を使える機能ではない。runtime delivery、shell、
dashboard、installer、本人設定・trust・skill の設定は変更しない。

## 境界と private API

| モジュール | 仕事 | 担当しないこと |
| --- | --- | --- |
| `bin/lib/local_model_profiles.py` | 明示 operator root の profile 読取、schema/UID/mode/link/URL/env 検査、全 profile の canonical digest | HOME/cwd からの検索、catalog/cloud への fallback、承認・配備 |
| `bin/lib/runtime_launch_intent.py` | 正規化済み caller＋承認済み context からの不変 intent、全 intent digest、再照合と共通 argv/env/config 投影 | owner の保存・revision・pending、起動 envelope、native permission engine、既存 launch receipt の状態判断 |
| `bin/lib/local_model_auth.py` | 固定文脈での bounded subprocess、runner/依存物の照合、既存 owner 参照からの helper 実行 | token の env 化・保存、別 auth 台帳、登録、provider の起動、通常起動での自動配備 |

`resolve_launch_intent(caller, context)` の context は、operator の明示 configure が
所有する入力である。HTTP 入力や子の環境をそのまま渡さない。`effective_scope` は
既存側が合成・承認した実効 plan であり、この core が native 規則を解析して新たな
許可を作るものではない。source ラベルは resolver を分岐させない。

`materialize_launch_intent(...)` は profile と設定 source、scope、runner の差分を
読取だけで検査する。`execution` は起動／resume で共通の **部分投影**であり、
実行可能な完全 command ではない。`inherit_env=false`、
`requires_native_validation=true` を持ち、caller の env は採用しない。
consumer は既存の固定 provider HOME/安全な基本 env、tools/MCP・skill、native
resume envelope を維持したうえで、最終 config の credential/provider/settings
競合と scope の native enforcement を検証する必要がある。検証できないものを
起動可能として扱わない。Claude の `dontAsk` だけで権限の上限が完成したとはしない。

## Profile と digest

root は絶対 path・同 UID・0700、profile は同 UID・0600・regular・link 数1。
dirfd と NOFOLLOW で祖先もたどり、symlink と unsafe writable parent を拒否する。
OS 所有の sticky temporary directory は祖先として許す。読取は64 KiBまで。
名前は `[a-z][a-z0-9_-]{0,47}`、未知 field・空/制御文字 model・不正数値を拒否する。
window/output は TOML の符号付き64 bit正整数で `output < window` とする。
この宣言は CLI の実効 context を実測した証明ではない。

remote は HTTPS、query/fragment/userinfo は不可。HTTP は認証無し loopback だけ。
env は Codex が空、Claude が `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` の1–100だけで、
model/auth/接続先/HOME/PATH/identity/loader の別口を作らない。

profile digest は schema 検査後の全 profile、intent digest は `intent_digest` field
自身だけを除いた全 intent の canonical UTF-8 JSON（key sort、空白無し、array順保持）。
両 adapter、auth argv と承認済み runner/依存物・文脈、実効 scope と設定 source の
digest を含み、token・cache・mtime は保存しない。

## Auth helper の導出

認証がある場合、呼出側は既存の唯一の owner を `context.intent_owner` で明示する。
新たな auth 設定 file や推測した owner path は作らない。Codex の command-backed
auth と Claude の safely quoted `apiKeyHelper` は同じ runner＋owner/digest/provider
参照を使う。helper は owner・runner/依存物・profile・settings を再照合してから
固定 cwd、`PATH=/usr/bin:/bin`、`LANG=C` と承認済み HOME/USER/LOGNAME だけで取得する。
5秒・stdout 8 KiB（末尾 LF 1個だけ許可）、stderr は捨て、異常は固定 reason のみ。
stdout は bounded pipe として読み、timeout/超過時は自身が作った process group を終了する。

配備の所有者は後続の明示 configure。runner 自体は stdlib のみの1 fileで、
承認済み絶対 interpreter と `-I`（isolated startup）の shebang、0500・親0700を
持つ配備物を参照する。runner の import 前から caller の PYTHONPATH/user site を信用しない。
この段階は installer や instruction_configure へ接続せず、自動配備しない。
CLI 内の refresh は300000 msへ導出するが、実際の発火・header・既存ログインの
無継承は後続 native gate。token を取得するのは helper であり argv/env に載せない。

Codex の設定根拠は [公式 config reference](https://learn.chatgpt.com/docs/config-file/config-reference)。
`auth.command` の stdout と cwd/timeout/refresh を使い、`env_key`・直書き bearer・
`requires_openai_auth` を認証付き定義に混ぜない。output 予約は context-output から
compact limit に導出し、架空の `model_max_output_tokens` は書かない。

## 既存 red の分類と試験

fixture #266 の対象122件、追加の core/runner 試験40件、既存の計測器/負対照120件と
公開用チェック58件を直列実行し、**340 passed / 範囲外6件 deselected** を確認した。
開始時 load1 は12.98。表の件数は #266 の収録ケース数である。
全 R3 の実行は282 passed／保存・revision の6 failed／未接続 adapter の43 errors。
予期しない core failure はなく、全体が green になったとは扱わない。
既存関連3 file は変更前後とも286 passed（既存の Authlib 警告2件）。

| #266 のケース | その1の対象 | 残す境界 |
| --- | --- | --- |
| profile 67 | green | configure/CLI の対応能力は別 gate |
| auth 15 | green | native CLI の helper 起動/TTL は含まない |
| intent 40 | 純粋な digest/設定導出/入力 drift が green | 実 caller の一致とは数えない |
| intent 6 | red のまま | `save/load_launch_intent`・revision・pending と既存 owner の接続は未実装 |
| native 38、caller 5 | missing adapter の red のまま | 実 CLI・実入口・hook/Mail・skill・lifecycle の受入 |
| instruments/review controls 120 | 既存の負対照を変更せず green | 計測器の成功と native 成功を区別 |

`test_core_projection.py` は生成 TOML/settings を読み、試験用に明示配備した実 runner
を実行し、偽 TLS peer に model/path/header/tool turn と rotation を送る **試験用 HTTP
consumer**。native CLI や製品の protocol gateway の代わりではない。Claude header は
試験で選んだ形であり、CLI がその形を使うとの主張はしない。実装前の各負対照も
継続し、確認のために contract の期待値・skip/xfail を緩めない。

全試験は repo-local venv、隔離 HOME/TMPDIR/runtime、直列、開始時 load15未満。
既存関連286件を前後で実行する。live・Keychain・本物の鍵・実 CLI は使わない。
