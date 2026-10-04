# ドキュメント索引

[English](README.en.md) · [README](../README.md)

操作の入口は画面内の **Your first flight → help map → Full tour**。cockpit の `Settings → Getting started` から開けます。ここは install・update・困ったときと、必要に応じて読む詳しい参照の目次です。

Telemetry 自体の操作は、header の `HELP MAP`（NETWORK では `SETTINGS` の隣）から見回せます。単独の画面でも cockpit 内の埋め込みでも、DECK / NETWORK の操作に注記します。詳細パネルは閉じてから押します。

## はじめる

- [ORRERY cockpit](https://github.com/gyroid-eth/orrery/blob/master/README.md#クイックスタート) — 1行で両方を入れ、画面の初回ガイドから始める。
- [Telemetry 単独で使い始める](getting-started.md) — 対象者・用語と、単独 install、agent 起動、Mail の通し確認。

## 使い方

- [Dashboard](dashboard.md) — DECK/NETWORK、履歴、RESUME/EXIT、REPLAY と詳細の参照。
- [委任と child agent](delegation.md) — ORRERY /delegate、組み込み子との違いと渡す道具の選択。
- [Launcher と identity](launchers.md) — agent-start、名前、resume、/delegate と /log、通知の流れ。
- [Obsidian と一緒に使う](obsidian.md) — ログ・論文ノート・タスクを vault と Daily Note に置く。

## install・update

- [Update](install.md#upgrade) — 既存設定を引き継いで更新する手順。
- [インストール](install.md) — 要件、単独 install、検証、upgrade/uninstall と移行手順。
- [設定](configuration.md) — AGENTSTACK_*、保存・公開範囲、model・child の設定を調べる。
- [Codex App 統合](codex-app.md) — optional provider として Desktop の task/subagent を載せる。
- [Antigravity / Gemini](antigravity.md) — optional provider の追加と認証・機能の境界。
- [常駐 agent](persistent-agents.md) — enrollment、interactive/headless 起動、credential の復旧。
- [Mail service の更新](agentstack-mail-update.md) — 稼働中 Mail の build を検証して差し替える運用手順。

## Windows・WSL

- [WSL2](install.md#windowswsl2で入れる) — 標準の Windows 導入は Ubuntu 内で行い、Windows のブラウザで開く。
- [native Windows helper（実験的）](windows-local.md) — 標準 WSL2 導入とは別の、community による Mail/dashboard 起動。
- [native Windows Codex launcher（実験的）](windows-codex-launcher.md) — 事前登録した子を専用 Windows tmux から起動する境界。
- [native Windows spawn（実験的）](windows-spawn.md) — native NEW AGENT の名前 catalog 取得と、SPAWN が無効である境界を読む。

## 困ったとき

- [トラブルシューティング](troubleshooting.md) — service、通知、spawn、認証、WSL の止まり方を切り分ける。

## 設計・開発者向け

- [Hooks と helper](hooks.md) — event、block/release/cleanup と運用 guard の仕様。
- [API reference](api.md) — route、request/response と互換世代の契約。
- [デザイン言語](design.md) — dashboard の見え方と動きの基準。
- [Embedded tour events（英語）](embedded-tour-events.md) — 埋め込み Telemetry が cockpit の checklist に実操作を通知する契約。
- [ORRERY Mail の内部構成](agentstack-mail.md) — 同梱 package の境界、provenance、cutover と正本。
- [Mail 更新の設計メモ](agentstack-mail-update-design.md) — 差し替え方式の理由と未決事項。操作は更新手順を参照。
- [Mail 性能 gate の設計](agentstack-mail-performance-gate.md) — 設計のみの benchmark と採否基準。常設 benchmark の実行手順ではない。
- [Claim/enrollment 設計](agentstack-mail-claim-enrollment-design.md) — product-decision ledger に対応する enrollment の設計資料。
- [第三者コンポーネント（日本語）](third-party.md) — ORRERY Mail、provider ロゴ、肖像画像の license と来歴。
- [開発への入口](../CONTRIBUTING.md) — build・検証・変更を送るときの手順。

英語版の無い文書は原語を明記しています。画像/GIFは各文書に付随する資料です。索引の網羅性は `python3 scripts/check_docs_index.py`（repository の根から）で確認できます。
