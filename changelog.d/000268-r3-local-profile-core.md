### ローカルモデルのプロファイル検査と認証の共通 core

`local:<name>` の安全なプロファイル読取・全体 digest、共通の bounded auth runner、
Codex/Claude 用の非秘密 argv/env/config 導出を独立したモジュールに追加した。
偽 peer と実 runner の試験を分け、[その1の範囲と残る red](../docs/r3-local-profile-core.md)を記録する。
既存の子の起動・resume・保存・dashboard・installer には未接続で、通常の model 選択や
本人設定・trust・skill の動きは変わらない。API generation の変更はない。
