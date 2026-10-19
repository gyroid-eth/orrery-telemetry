### dsh と Hermes の任意導入

`install.sh --enable-dsh --dsh-node /absolute/node` と `--enable-hermes` で、固定版の各runtimeとmanaged adapterを個別に導入できます。既定は取得なし、更新時は保存した選択を継承します。鍵や既存ログインは導入しません。

runtimeは `harnesses/<harness>/<世代>` に配置し、通常uninstallはinstaller所有の世代だけを削除します。利用者のprofiles・記憶・skillsと手動のHermes環境は保持します。[導入手順](../docs/install.md)を参照してください。
