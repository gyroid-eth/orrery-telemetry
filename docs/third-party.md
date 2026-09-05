# 第三者コンポーネント

[前: トラブルシューティング](troubleshooting.md) · [README に戻る](../README.md)

ORRERY Telemetry は identity、mail、reservation、signal の正本として、repository
内の `packages/agentstack_mail` から構築する ORRERY Mail を同梱します。installer が
外部 mail server repository を clone または実行する経路はありません。

ORRERY Mail には predecessor から継承または意味的に派生したコードがあります。
その provenance、原 copyright、固定 snapshot は
[`NOTICE.md`](../packages/agentstack_mail/NOTICE.md)、継承・派生部分の適用 license は
[`UPSTREAM_LICENSE`](../packages/agentstack_mail/UPSTREAM_LICENSE)、ORRERY Telemetry 側で新たに書いた部分（file 名の AgentStack は旧名称）の
license は [`AGENTSTACK_LICENSE`](../packages/agentstack_mail/AGENTSTACK_LICENSE) が正本です。
これは runtime component の attribution であり、別の third-party service を installer
が選択できるという意味ではありません。

| Component | 配置 | 役割 | License の正本 |
| --- | --- | --- | --- |
| ORRERY Telemetry | この repository | launcher、hook、dashboard、skill、installer | [`LICENSE`](../LICENSE) |
| ORRERY Mail | `packages/agentstack_mail` | identity、mail、reservation、signal、SQLite | [`NOTICE.md`](../packages/agentstack_mail/NOTICE.md)、[`UPSTREAM_LICENSE`](../packages/agentstack_mail/UPSTREAM_LICENSE)、[`AGENTSTACK_LICENSE`](../packages/agentstack_mail/AGENTSTACK_LICENSE) |
| Claude Code / Codex CLI | user install | agent runtime | vendor terms |
| tmux / uv / git / Python | user environment | local runtime dependency | 各 upstream |

uninstall は mail state を既定で保持します。削除する場合だけ
`agentstack-uninstall --purge-data` を使い、先に `--dry-run` で exact path を確認して
ください。DB には task と message body が含まれ、dashboard は独自の remote 認証を
持たないため、既定の localhost bind を維持してください。
