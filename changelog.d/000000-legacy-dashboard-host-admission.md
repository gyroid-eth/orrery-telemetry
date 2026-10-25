### legacy dashboard で不信な Host を操作前に拒否

全 HTTP メソッド・全パスの Host を固定の許可先と照合し、不信・欠落・重複・不正な Host を dispatch 前に拒否します。POST の Origin は、その Host に対応する固定 origin とだけ照合します。既定の loopback 利用は維持し、LAN / VPN 利用者は更新前に追加の固定 origin を [設定](../docs/configuration.md#lan--vpn-から使う場合の-host-設定) してください。installer が保存し、dashboard の各 service 起動経路へ引き継ぎます。

認証は追加しません。既存の global capability 認証と公開 static resource の扱いは維持します。API の endpoint・field・応答の意味は変わらないため、API generation は据え置きます。
