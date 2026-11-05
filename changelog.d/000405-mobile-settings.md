### スマホの閲覧画面をcockpitのSettingsからオンにし、会話の記録を既定で読めるようにする

新しい`agentstack-mobile`（`status`・`apply`・`off`）が、スマホのreaderの設定`~/.agentstack/mobile.json`（mode 0600）とlaunchdのjob `org.agentstack.mobile-relay`を管理します。cockpitの**Settings → Phone**はこのcommandを呼びます。Tailscaleのsign in・Serve・Funnelは読むだけで、Serve・Funnel・policyは変えません。policyは本人が**I reviewed the policy**を押した日時を残すだけで、確かめはしません。手で起動したreaderがportを使っていれば、止めずに断ります。installはcommandを置くだけでjobを作らず、uninstallはjobを止めます。readerは`--config`でこのfileを読みます。会話の記録（History）は、policyの確認（`--phone-policy-reviewed`）があれば既定でオンになり、`--disable-history`で切れます。確認がなければオフのまま起動し、`/view/history`が`policy_not_reviewed`を返します。以前の`--enable-history`もそのまま使えます。boundの記録だけを出すこと、pathを出さないことなどの約束は変えていません。詳しくは[スマホで状況を見る](../docs/mobile.md#cockpitのsettingsからオンにする)を参照してください。

**手で起動しているreaderを更新する人へ:** `--phone-policy-reviewed`を付け、`--enable-history`を付けずに起動していたreaderは、更新後の再起動から会話の記録とfile・画像のpreviewもスマホに出します。出したくなければ、再起動の前に`--disable-history`を足してください。起動時にもこの変更を1行で知らせます。

手で起動したreaderが動いている間、Settingsは**MANUAL**と表示し、オン・オフのどちらも何も変えません（`port_in_use`）。loginかaddressを変えるとpolicyの確認は取り消されます。launchdが起動に失敗したときはPhone accessをオフに戻します。cockpitのdemo modeでは、Phoneの節はlogin・address・ServeのURLを出しません。
