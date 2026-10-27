### Added
- Codex の子の既定 model と effort を `AGENTSTACK_CODEX_DEFAULT_MODEL` と `AGENTSTACK_CODEX_DEFAULT_EFFORT` で設定できるようにしました。installer で指定した値は `env.sh` と service に保存され、明示した `--model` と `--effort` はそれぞれの既定より優先します。

### Changed
- `sol` / `terra` / `luna` / `astra` の短縮名は、新しい catalog にある同じ系統の最新版を選ぶようにしました。新しい版が出ても製品の release を待たずに使えます。catalog にその系統が無いときは短縮名を拒否し、catalog が無い・古いときは従来の対応を使います。
