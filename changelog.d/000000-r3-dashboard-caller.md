### 隔離 global の HTTP 起動に承認済み caller を渡す入口

`POST /api/global/spawn` は既存の最小入力に加え、明示した親 client と完全な caller を受け取り、実 dashboard 入口から共通 operator へ渡します。必須欄・任意欄の省略を承認済み intent と照合し、cwd を書き換えたり owner から入力を補ったりしません。未知欄・型違い・混在・不正 tools は副作用前に拒否し、global から旧登録へは流れません。

API 世代を19から20へ上げ、`global_spawn_full_caller:1` を追加します。最小 HTTP 入力の schema・正常挙動と legacy は維持しますが、最小入力の identity/intent 不一致も共通の固定 reason で503から409へ改善します。HTTP の foreground 実行（`prepare_only:false`＋`caller.detached:false`）は副作用前に400 `HTTP_FOREGROUND_UNSUPPORTED` で拒否し、detached を変更しません。prepare-only なら detached が false でも task 準備まで許可し、Python/CLI の foreground 契約は変えません。この capability は配送入口だけを表し、設定/権限の承認や未検証 local CLI の実行許可を増やしません。画面・catalog/status・全入口・native の受入は未完です。[HTTP](../docs/api.md)と[起動契約](../docs/global-delivery-client.md)を参照してください。

### CLI 版の差分を共通比較し、同じ launch と認証 helper に結ぶ内部契約

承認時と観測時の CLI metadata・実効範囲・非 CLI 資源・model/endpoint/auth/context
を比較する共通 core を追加します。版だけの差と、安全な同一導入先での版＋本体更新を
同じ比較証拠として既存 launch に記録し、auth helper もその証拠と実 process の対応を
再照合します。同版での bytes 変更、他の source/runner/依存物の drift、観測不能は拒否します。
承認済み intent/plan の書換えや provider PID の別保存はありません。

この段は偽の観測値を使う比較・実 R2・helper の接続試験までです。採用済み native
observer が無い実 CLI は引き続き unsupported で、local 子の運用開始、native 権限の
強制、実 CLI の refresh/header/skill 受入を完了とは扱いません。公開 caller への版通知も
後段で、今回の内部契約には追加の API 世代変更はありません。
[比較と残る境界](../docs/r3-local-profile-core.md#cli-版の比較契約native-受入とは別)を参照してください。
