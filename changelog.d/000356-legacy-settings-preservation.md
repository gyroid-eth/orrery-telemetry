### 更新時に保存済みの設定を保持

`env.sh` に保存された非既定値を、`AGENTSTACK_CHOSEN_SETTINGS` の有無や列挙内容にかかわらず引き継ぐようにしました。子のターミナルを自動で開かない設定（`AGENTSTACK_AUTO_OPEN_CHILD=0`）や独自の肖像フォルダーが、更新時に既定値へ戻るのを防ぎます。今回明示した値・空値による個別reset・`--reset-settings` は引き続き優先し、設定の値自体を削除した場合は既定値に戻ります。ORRERY Mail の識別情報と配置を保持する規則も変わりません。

子のターミナル自動表示と肖像フォルダーに実際の変更がある場合は、installer の計画に旧値 → 新値を表示します。[設定の引き継ぎ](../docs/install.md#前回の設定の引き継ぎ)を日英で更新しました（[#354](https://github.com/gyroid-eth/orrery-telemetry/issues/354)）。
