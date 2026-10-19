### 明示したprojectを解決できないgraphを正常表示しない

legacy dashboardで明示したproject keyが見つからない場合や、その解決でSQLiteエラーが起きた場合、別の既定projectへ切り替えず、空のgraphと理由・`degraded: true`を返します。通常のgraphとspawn-onlyの両応答で理由を保持します。project keyが未指定の場合の既定動作は変わりません。
