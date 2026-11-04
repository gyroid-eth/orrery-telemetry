### dsh・Hermes の子へ Codex と同じ Chrome MCP 定義を渡す

dsh・Hermes の子は既定で Codex の有効な Chrome MCP 定義だけを継承し、`--autoConnect` を含む同じ command・args・env・cwd で普段の Chrome に接続します。`mail-only` は自動追加せず、未設定・無効なら Mail のみです。native で表現できない tool filter/approval は拒否し、明示 browser の承認条件も維持します。[子の道具選択](../docs/delegation.md)を参照してください。
