### Fixed
- Mail watcherは、宛先のsessionが無いことを確認した通知を取り下げ、一時的な取得失敗だけを再試行するようにしました。新しい通知やMail本体は保持します。leaseのguard数と標準logの世代サイズにも上限を設けました。
