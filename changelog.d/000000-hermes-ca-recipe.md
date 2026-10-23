### 管理HermesのCA依存を検証する新世代を構築

Hermesの実行コードは固定したまま、公式更新からcertifi 2026.10.19とPM用uv 0.13.5の依存入力3件だけを取り込みます。旧入力・patch・適用後のhashを照合し、両PM導入と再同期で同じ入力を使います。baseと更新元のcommitを分け、native stampには変更済みsourceであることを記録します。

新世代はrecipeのdigestで識別し、旧v2やprofileを上書きしません。公開前と再利用時には、実際のCA bundle・version・解決済みlock・PM用uvの実行fileとartifactの整合を確認します。静的catalog検査はHermesや資格helperを実行しません。対象はMacとGNU/Linux（WSLを含む）のarm64/x64で、旧artifactが残るBionicなどは停止します。既存の稼働process・手動Hermes・システムCA・呼出し元のpip/uvは変更しません。[導入と検証](../integrations/hermes/README.md#installation-and-offline-checks)を参照してください。
