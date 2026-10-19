### managed 子の設定を更新・ログイン後も保持

dshの固定Node参照をenv.shへ保存し、旧導入はcurrent.jsonから補完します。`--managed-spawn-config`で私有manifestの参照を保存し、dashboardのlaunchd/systemd/背景起動へ引き継ぎます。

dshの製品雛形から試し用の要求回数・出力token上限を除去しました。未指定はモデルとハーネスの既定を使い、明示した正の整数の上限だけ適用します。要求timeoutと許可先・usageの検査は維持します。
