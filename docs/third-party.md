# 第三者コンポーネント

> English version: planned.

[前: トラブルシューティング](troubleshooting.md) · [README に戻る](../README.md)

## `mcp_agent_mail`

ORRERY Telemetry は [mcp-agent-mail](https://github.com/Dicklesworthstone/mcp_agent_mail) を次の正本として使います。

- project と agent identity
- inbox / outbox / thread
- registration token による identity ownership
- file reservation
- signal と delivery state

dashboard や launcher が独自の mail registry を持たないのは、UI や tmux session の寿命と協調データの寿命を分離するためです。

## 現行 release: 非同梱

現行 installer は、この repository 内の provenance snapshot を runtime
source として deploy しません。通常の install では外部 clone を取得します。
`packages/agentstack_mail/provenance/` の Git bundle と dirty patch は
承認済みの機能抽出 baseline を再現するための開発・監査 artifact であり、
現行 service からは実行されません。

installer は:

1. `AGENTSTACK_AGENT_MAIL_REPO`、または既定 upstream URL を使う
2. `AGENTSTACK_MAIL_DIR`、既定 `~/mcp_agent_mail` へ clone
3. 既存 clone があれば remote を確認して再利用
4. DB と `.env` を upstream directory に保持

します。

uninstall でも agent-mail clone、DB、`.env` は既定で保持します。AgentStack の UI を消しても協調履歴を失わないためです。削除する場合だけ `agentstack-uninstall --purge-data` を使い、事前に `--dry-run` で exact path を確認してください。

## 目標 architecture: `agentstack-mail`

承認済みの移行先は、live Python AgentMail から AgentStack が利用する
contract と依存 closure を抽出し、`agentstack-mail` として改名・同梱する
構成です。現在は `packages/agentstack_mail` に contract/provenance だけを
固定しており、production server でも installer の既定値でもありません。

新しい AgentStack-authored files は repository の PolyForm license、
AgentMail からコピーまたは意味的に派生した部分は原 copyright と rider を
含む upstream license を保持します。package の `NOTICE.md` と2つの
license file が per-component boundary の正本です。

## License

ORRERY Telemetry 本体は **PolyForm Perimeter License 1.0.1** です。正本は repository の [`LICENSE`](../LICENSE) です。source-available であり、OSI の意味での open source ではありません。

Perimeter の中核は Noncompete 条項です。利用・改変・再配布は目的を問わず可能ですが、**本ソフトウェアと競合する製品を他者へ提供すること**はできません。license は競合を広く定義しており、無償配布、別言語や別プラットフォームへの移植、service / library / plug-in としての提供も含みます。

`mcp_agent_mail` は upstream の **MIT License with OpenAI/Anthropic Rider** に従います。**本 repository の license は `mcp_agent_mail` に及びません。** 取得後の正本:

```text
${AGENTSTACK_MAIL_DIR:-$HOME/mcp_agent_mail}/LICENSE
```

**Rider は `mcp_agent_mail` 側だけの条項です。** ORRERY Telemetry 本体には Rider はありません（本 repository は Claude Code / Codex CLI を動かすための道具なので、利用者がそれらの API へ本 software を送信することを妨げない条文にしています）。`mcp_agent_mail` の Rider の適用範囲は upstream の `LICENSE` 全文で確認してください。

この文書は要約であり、license 条項そのものではありません。配布・派生物・host・benchmark・dataset / training / evaluation への利用を判断するときは、必ず各 component の `LICENSE` 全文を確認してください。

## Component boundary

| Component | 配置 | データ / 役割 | License の正本 |
| --- | --- | --- | --- |
| ORRERY Telemetry | この repository | launcher、hook、dashboard、skill、installer | [`LICENSE`](../LICENSE) |
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

scientist portrait catalog と dashboard assets は ORRERY Telemetry 配布物の一部です。private portrait は `AGENTSTACK_PORTRAITS_DIR` で repository 外に分離できます。

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
