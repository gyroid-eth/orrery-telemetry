### 管理された子の起動を legacy の opt-in に追加

DeepSeek／Strata と dsh／Hermes を分けて選ぶ子の起動を追加。要求 ID による再照会、実 plugin-ready と task の成否の区別、私有作業dir、stop・release・retire 確認後の clean を接続。既定は無効。Hermes の同じ固定profileの同時起動は profile_busy で拒否する。API 世代を 19 から 20 に更新。設定と限界は [契約](../docs/managed-spawn-contract.md) を参照。
