### R3 ローカルモデルの実装前受入 fixture

ローカルモデルのプロファイルに対する tests-only の契約 fixture と負対照を追加。
製品の対応はまだ未実装であり、契約試験は意図的に red のまま。
[試験の範囲と未接続部分](../docs/r3-local-profile-fixtures.md)を明示した。
通常の起動・モデル選択・API generation に変更はない。

講評後に、認証・鍵更新の wire、複合操作両端の副作用、pending 実保存物、
LaunchIntent の独立 digest、実 caller の捕捉に対応する負対照を強化。
Claude/Codex の local の子で通常の skill を使う red の受入仕様と、未収録規則の表も追加した。
再講評後に、入口ごとの新規 capture/nonce と no-op の負対照、resume の明示 envelope
以外の全 argv 照合を追加した。実 caller/native adapter は引き続き未接続。
