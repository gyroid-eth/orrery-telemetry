### Androidホーム画面のショートカットからスマホ閲覧画面を開けるよう修正

本人・Hostと指定されたOriginの照合を保ったまま、queryなしの厳密な`/`へのGET/HEADで、単一の`Sec-Fetch-Site: cross-site`・`Sec-Fetch-Mode: navigate`・`Sec-Fetch-Dest: document`が揃う画面遷移だけを受け付けます。画面の殻を返す例外で、`/view/*`、画像・JS・CSS・font、POSTなどの操作、不一致Originや曖昧なpathの許可には広げません。CSP・no-store・既定オフと既存の認証・framing検査を維持し、API世代は変更しません。実Android・Serveの確認は配備後の別受入です。
