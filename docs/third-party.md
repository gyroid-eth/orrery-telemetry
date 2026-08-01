# 第三者コンポーネント

> English version: planned.

[前: トラブルシューティング](troubleshooting.md) · [README に戻る](../README.md)

## `mcp_agent_mail`

claude-agent-stack は [mcp-agent-mail](https://github.com/Dicklesworthstone/mcp_agent_mail) を次の正本として使います。

- project と agent identity
- inbox / outbox / thread
- registration token による identity ownership
- file reservation
- signal と delivery state

dashboard や launcher が独自の mail registry を持たないのは、UI や tmux session の寿命と協調データの寿命を分離するためです。

## 非同梱

`mcp_agent_mail` の source はこの repository に同梱しません。

installer は:

1. `AGENTSTACK_AGENT_MAIL_REPO`、または既定 upstream URL を使う
2. `AGENTSTACK_MAIL_DIR`、既定 `~/mcp_agent_mail` へ clone
3. 既存 clone があれば remote を確認して再利用
4. DB と `.env` を upstream directory に保持

します。

uninstall でも agent-mail clone、DB、`.env` は既定で保持します。AgentStack の UI を消しても協調履歴を失わないためです。削除する場合だけ `agentstack-uninstall --purge-data` を使い、事前に `--dry-run` で exact path を確認してください。

## License

claude-agent-stack 本体は **MIT License (with OpenAI/Anthropic Rider)** です。正本は repository の [`LICENSE`](../LICENSE) です。

`mcp_agent_mail` も upstream の **MIT License with OpenAI/Anthropic Rider** に従います。取得後の正本:

```text
${AGENTSTACK_MAIL_DIR:-$HOME/mcp_agent_mail}/LICENSE
```

Rider は通常の MIT 許諾に加え、OpenAI, L.L.C.、Anthropic, PBC、それぞれの Affiliate、およびそれらのために行動する者を Restricted Parties と定義し、提供・アクセス・利用などを制限します。

この文書は要約であり、license 条項そのものではありません。配布・派生物・host・benchmark・dataset / training / evaluation への利用を判断するときは、必ず各 component の `LICENSE` 全文を確認してください。

## Component boundary

| Component | 配置 | データ / 役割 | License の正本 |
| --- | --- | --- | --- |
| claude-agent-stack | この repository | launcher、hook、dashboard、skill、installer | [`LICENSE`](../LICENSE) |
| mcp_agent_mail | 外部 clone | identity、mail、reservation、signal、SQLite | upstream clone の `LICENSE` |
| Claude Code | user install | Claude runtime | vendor terms |
| Codex CLI | user install | Codex runtime | vendor terms |
| tmux / uv / git / Python | user environment | local runtime dependency | 各 upstream |

dependency を同梱扱いにせず境界を明記することで、upgrade、uninstall、data retention、license の判断を component ごとに行えます。

## Credits

- [mcp-agent-mail](https://github.com/Dicklesworthstone/mcp_agent_mail): identity、messaging、file-reservation substrate
- [tmux](https://github.com/tmux/tmux): session isolation と terminal multiplexing
- [Ghostty](https://ghostty.org/): 推奨 terminal integration
- Claude Code と Codex CLI: dashboard が観測・起動する agent runtimes

scientist portrait catalog と dashboard assets は claude-agent-stack 配布物の一部です。private portrait は `AGENTSTACK_PORTRAITS_DIR` で repository 外に分離できます。

## Security と privacy

- agent-mail DB には task と message body が入る
- dashboard は既定で localhost bind
- dashboard に独自認証 layer はない
- bearer token は agent-mail `.env` から読む
- token file は mode `0600`
- uninstall は mail data を既定で保持

backup、remote access、purge の方針はこの data boundary を前提に決めてください。

## 関連文書

- [インストール](install.md)
- [Hooks と運用 helper](hooks.md)
- [Codex App 統合](codex-app.md)
- [設定](configuration.md)
- [トラブルシューティング](troubleshooting.md)
