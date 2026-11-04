### Claude・dsh・Hermes の会話記録を本人の証拠から対応付け

legacy の `/api/agents` は Claude / Claude Code・dsh・Hermes の本人の会話を検証し、`history_binding` と理由、native ID の `history_session_id`、元の receipt 時刻 `history_receipt_ts` を共通の契約で返します。`bound` のときだけ `transcript_path` を公開し、不明・競合は `unbound`、現在の起動より古いと分かる証拠は `stale` として path を null にします。cwd・最新履歴・本文の名前からは推測しません。

Claude は自己登録 receipt と JSONL の native ID、dsh は新しい起動の callback と persistence が返した場所・inode、Hermes は子専用 SQLite DB と exact session ID を照合します。dsh の Zstd JSONL と Hermes の SQLite は、それぞれ対応した reader が必要です。Codex の既存の契約と fresh の保護された履歴経路は維持し、API の互換世代は3 provider をまとめて34から35へ1回だけ上げます。[共通契約と provider ごとの条件・限界](../docs/api.md#get-apiagents)を参照してください。

dsh は別 root の保存先・flush・receipt 発行が失敗しても起動単位の競合を保持し、古い会話を復活させません。新 terminal より古い receipt は `stale` とし、登録照合は既存のまとめ読取と接続解放を共有して roster の行数分の SQLite 接続を増やしません。

root 観測や競合 marker 自体の書込みが失敗しても lock を保持し、競合側と所有側で失敗状態を共有して旧会話の公開・lock 解除を止めます。書込みの回復だけでは同じ起動を復活させません。
