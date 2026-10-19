### DeepSeekの表示を明示opt-inにする

installerの `--enable-dsh` でDeepSeekの候補とロゴ配信を有効化し、
`--disable-dsh` で無効化できる。既定は無効で、flagなしの更新は既存選択を保つ。
runtimeは未接続と表示し、この操作でruntime取得・鍵・登録・API要求を行わない。
provider catalogに任意のdsh候補を追加するため、API generationを19から20へ上げる。
