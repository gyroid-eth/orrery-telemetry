### 新しいMail環境で修正版のCA bundleを要求

新しく構築するORRERY Mail環境の依存に`certifi>=2026.10.19`を追加しました。再導入時もこの下限を適用します。2026.10.18でも影響するemSign rootは除去済みですが、影響しないrootとの互換性のため2026.10.19以上を採用します（[Pythonの公式案内](https://discuss.python.org/t/information-for-python-users-regarding-emudhra-emsign-ca-certificate-security-incident/109576)）。

稼働中のMailをそのまま使う通常のkeepでは、既存venvを変更しません。既存Mailへの適用は[通常のbuild切替手順](../docs/agentstack-mail-update.md#--update-mail-による差し替え)で行います。この変更はmanaged Hermesの固定依存や、pip/uv自身が内蔵するCA bundleの更新を含みません。
