# 絶対 path の正規化と予約 adapter

[English](absolute-reservations.en.md)

## 適用範囲

共通 normalizer と明示した proxy/hook/CLI adapter を提供します。候補予約の唯一の server は [S2c の SQLite runtime](global-reservations.md)です。PR2 の in-memory ReservationServer・PersistentReservations の copy/flush・reservation_activity の probe/collector は削除しました。既定の Mail・proxy・hook は旧 mode のままで、activation=false。明示した fixture transport でだけ候補 adapter を接続します。

## 同じ正規化

`reservation_paths.normalize_path()` を server/client/CLI/hook が共用します。server は絶対 path を要求します。client は観測済み cwd を明示し、その cwd で相対 path を絶対化します。旧 `project_key` は受け取って無視し、anchor にしません。旧 raw lease の anchor が不明なら、絶対 path での取り直しまたは移行証拠を求めます。現在の旧 lease はこの PR で変換しません。

存在する親の symlink と `..` を解決し、新規 file の末尾を保持します。Unicode を無条件に NFC 化せず、既存 file の case/Unicode/hardlink の alias は filesystem の同一性で比較します。`*` は `/` を越えず、`**` は複数階層に対応します。`/a/b` と `/a/bb` は別範囲です。glob 同士の部分的な重なりを確定できない場合は保守的に競合とします。glob 内の symlink directory も解決するので、link 先の新規 file を見落としません。scope 解決は metadata だけを読み、1,000 steps/0.25秒、比較は10,000 pairs/0.25秒で打ち切り、`GLOB_SCOPE_UNKNOWN` として要求を止めます。広い予約を黙って非競合にしません。

glob を含む末尾の `..`、不明な symlink anchor、曖昧な separator は診断で拒否します。`tool://`・`resource://`・`service://` は filesystem と別型の exact match です。POSIX の `~` や Windows drive を推測で展開しません。Windows drive は確認済みの WSL mapping を client に明示し、Mail server と同じ filesystem の `/mnt/...` へ変換します。別 host の filesystem は対象外です。

正規化だけを試す CLI（Mail へ接続せず、予約を作りません）:

```sh
python -m agentstack_mail.reservation_clients --cwd /workspace/repo 'src/new file.py'
python -m agentstack_mail.reservation_clients --wsl-drive c=/mnt/c 'C:\workspace\new.py'
```

## 候補の呼出経路

新規 file の ASCII case の比較も、同じ volume の既存 directory に case alias があると確認できた場合だけ同一視します。名前の末尾は書き換えず、case-sensitive volume は別名として扱います。Unicode の無条件な casefold/NFC 変換はしません。

Unicode は対象 directory の filesystem を read-only の native metadata で確認します。現行 APFS の canonical-equivalence と HFS+ の Unicode 3.2/exclusion の規則を、未作成 leaf、glob の照合、glob 比較に共用します。名前の保存表記は変えません。Linux は既知の byte 区別する filesystem と directory の casefold flag を確認し、NFC/NFD の別 file を同一視しません。不明な mount/API/Unicode 版は `UNICODE_RULES_UNKNOWN`（Linux casefold directory は `FILESYSTEM_RULES_UNKNOWN`）として取得を拒否します。OS 名だけで一律に NFC 化せず、runtime の確認のために file を作ることもありません。[APFS の名前の契約](https://developer.apple.com/library/archive/documentation/FileManagement/Conceptual/APFS_Guide/FAQ/FAQ.html)と[HFS+ の Unicode 規則](https://developer.apple.com/library/archive/technotes/tn/tn1150.html)を根拠にします。


case-insensitive な filesystem の候補では、非 ASCII を含む path（directory 名を含む）を `CASE_RULES_UNKNOWN` として取得拒否します。ASCII directory の別名を確認しても、filesystem の版付き非 ASCII case table を保証できないためです。Python の無条件な casefold は使いません。非 ASCII glob や ASCII glob が非 ASCII entry を探査する場合も、不明な比較は取得・coverageを拒否します。case-sensitive な filesystem の Unicode canonical-equivalence と名前の区別は維持します。この制限は版を固定した filesystem の case table を実装するまで続き、既定の旧 mode には影響しません。

`ReservationClient` は authenticated binding resolver と candidate dispatch を必須とし、acquire/check/renew/release で同じ正規化を使います。既存 proxy の `candidate_reservations()` は、既存 `_resolve()` で各呼出の binding/token を確認し、観測した cwd と明示した backend でこの client を作ります。既定の `tools/call` には接続しません。旧 proxy の相対 path 制約も維持します。

`hook_guard()` と `hook_release()` は同じ client を使います。resolver が未参加と確認した session は noop、認証済みの参加 session はどのフォルダでも予約を要求します。binding 不明、認証失敗、通信断は block です。勝手な登録や token 探索は行いません。共通 shell の `reservation_absolute_paths()` は同じ CLI に明示した cwd を渡す候補 helper で、既存 hook からの呼出はありません。sandbox、OS の権限、Codex add-dir、閲覧許可は変更しません。

release adapter は旧 hook と同じ失敗結果の判定を保ちます。tool_result/tool_response/tool_output の error、success:false、failed/blocked status、エラー文字列（入れ子を含む）は noop で、binding 解決と release の dispatch を行いません。成功した Edit の後は解放でき、失敗後の再試行では予約を維持します。

server の batch acquire は競合が1件でもあれば全件拒否します。共有同士は許可し、片方が exclusive なら競合です。check は具体的な file に限ります。renew/release は owner を確認し、path selector は正規化した同一の予約 pattern を選びます。ID と path は片方だけを指定します。selector 省略はその owner の ACTIVE 予約だけ、明示した空 path list は拒否します。TTL/延長は60–86,400秒で、期限切れを renew して復活させません。

## 期限と解放

S2c は未解放かつ期限前だけを ACTIVE とします。期限切れは導出だけで、履歴の解放時刻や revision を書き換えません。活動 probe、Git activity、grace と collector はありません。renew は現在の期限へ延長秒数を足し、期限切れを復活させません。owner の明示解放・退役時の ACTIVE 解放・管理 force のみが状態を変えます。詳細と旧 mode との差、試験移設は [S2c の契約](global-reservations.md)を参照してください。

既定 app の活動/liveness と57-path performance gateは別の既存実装として維持します。実 client/hook の結線は後続 4b/4c、運用切替は PR7 です。
