# Codex の shell guard を更新する日の操作

B と履歴照合は同日に適用します。先に更新すると、既存の Codex 子はフックの確認画面で入力待ちになります。機体ごとに、既存の子が引き継ぐ承認元の `CODEX_HOME` を使ってください。作業中の子を終え、通常のプラグイン更新経路でプラグインを更新してから始めます。

[プラグインのみを更新する手順](https://github.com/gyroid-eth/orrery/blob/master/docs/en/codex-app.md#refreshing-an-existing-plugin-after-a-core-update)

## macOS の1台目

macOS の承認元 HOME で対話型の `codex` を開きます。起動画面で **Enter** を押して **Review hooks** に進みます。

```text
Hooks need review
› 1. Review hooks
  2. Trust all and continue
  3. Continue without trusting (hooks won't run)
```

**↑ / ↓** と **Enter** で **PreToolUse** を開きます。変更された AgentStack の shell ガードの matcher が `*`、command が `"$PLUGIN_ROOT/scripts/run-key-read-guard.sh"` であることを確認します。関連する AgentStack のレビュー項目を確認できたら、**Esc** で一覧へ戻り **t** を押してまとめて承認します。別のプラグインの変更が表示された場合は、対象行だけを選び **t** を押します。**Esc** で閉じ、通常の新規 Codex 子で確認します。

## macOS の2台目

2台目のローカル端末か SSH の対話端末から、その機体の承認元 HOME で `codex` を開きます。**Hooks need review** が表示されたら **Enter** を押します。すでに入力欄にいる場合は **/** でコマンド一覧を開き、`hooks` を選んで **Enter** を押します。

```text
Hooks
PreToolUse
Enter: review · t: trust all · Esc: close
```

**↑ / ↓** で **PreToolUse** を選び **Enter** を押します。AgentStack の新しい matcher `*` と既存の wrapper を確認します。**Esc** で一覧へ戻り、レビュー対象が確認済みの AgentStack の項目だけなら **t** を押します。ほかの項目が混じる場合は、対象行で **t** を押します。**Esc** で閉じ、新規 Codex 子で確認します。

## WSL

WSL の対話端末から、WSL 側の承認元 HOME で `codex` を開きます。**Enter** で **Review hooks** を開きます。

```text
Hooks need review
› 1. Review hooks
  2. Trust all and continue
  3. Continue without trusting (hooks won't run)
```

**↑ / ↓** と **Enter** で **PreToolUse** を選び、AgentStack の matcher `*` と既存の wrapper を確認します。**Esc** で一覧へ戻って **t** を押します。ほかのプラグインの変更も出ている場合は、AgentStack の対象行だけを選んで **t** を押します。**Esc** で閉じ、WSL の通常の新規 Codex 子で確認します。

3台それぞれの承認元 HOME で行えば、同じ承認を継承する新規の子に再度の操作は要りません。既存の独立 HOME は、同じ承認元から再生成してから確認してください。Mobile Terminal は1行入力と Enter に対応します。レビュー一覧の移動と単独キーの承認には対話端末を使います。

- [OpenAI のフック承認手順](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)
- [ORRERY Mobile Terminal の入力仕様](https://github.com/gyroid-eth/orrery/blob/master/docs/en/mobile.md#inspect-a-pane-and-send-input-from-your-phone)
