### 空の Mail へ向けた project 非依存の接続

旧 Mail を保管したまま別 instance を空から用意し、利用者のagentが日英手順で登録・設定・必要な本文の持越し・復旧を進める経路を追加します。自動の無損失移行や旧資格の継承は行いません。

API世代を33から34へ上げ、明示fresh dashboardのNew Agentと有限rosterにMail instance／agent IDを付けます。standaloneの準備元と論理上の親を分け、一意な親名または明示IDで共通自動子起動へ接続します。旧運用は既定のままで、未接続APIは旧DBへfallbackせず拒否します。切替完了はまだ宣言しません。[移行手順](../docs/fresh-global-update.md)の未受入事項を確認してください。
