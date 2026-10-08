# 絶対 path の予約候補（PR2）

[English](absolute-reservations.en.md)

## 適用範囲

#213 の PR2 は、共通 normalizer、認証・owner を検証する候補 server、bound proxy の明示 factory、hook の guard/release adapter、CLI の正規化、予約ごとの activity/GC を用意します。既定の Mail tool、DB schema、proxy の tool 呼出、install 設定、既存 hook は旧 mode のままです。環境変数で候補へ切り替える設定はありません。merge だけでは運用は変わりません。

`ReservationServer` は明示的に作る隔離された in-memory store です。認証 callback は必須で、caller が owner ID を指定する API はありません。永続 DB への adapter、旧 lease の import、instance/binding の切替、実 hook の session resolver 接続は後続 PR です。候補 store を production server として使わないでください。dashboard の API 世代も変えません。

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

Unicode は対象 directory の filesystem を read-only の native metadata で確認します。現行 APFS の canonical-equivalence と HFS+ の Unicode 3.2/exclusion の規則を、未作成 leaf、glob の照合、activity 展開に共用します。名前の保存表記は変えません。Linux は既知の byte 区別する filesystem と directory の casefold flag を確認し、NFC/NFD の別 file を同一視しません。不明な mount/API/Unicode 版は `UNICODE_RULES_UNKNOWN`（Linux casefold directory は `FILESYSTEM_RULES_UNKNOWN`）として取得を拒否し、probe は unknown として早期回収を止めます。OS 名だけで一律に NFC 化せず、runtime の確認のために file を作ることもありません。[APFS の名前の契約](https://developer.apple.com/library/archive/documentation/FileManagement/Conceptual/APFS_Guide/FAQ/FAQ.html)と[HFS+ の Unicode 規則](https://developer.apple.com/library/archive/technotes/tn/tn1150.html)を根拠にします。


case-insensitive な filesystem の候補では、非 ASCII を含む path（directory 名を含む）を `CASE_RULES_UNKNOWN` として取得拒否します。ASCII directory の別名を確認しても、filesystem の版付き非 ASCII case table を保証できないためです。Python の無条件な casefold は使いません。非 ASCII glob、過去の候補 lease、ASCII glob が非 ASCII の entry を探査する場合も unknown として早期回収を止めます。case-sensitive な filesystem の Unicode canonical-equivalence と名前の区別は維持します。この制限は版を固定した filesystem の case table を実装するまで続き、既定の旧 mode には影響しません。

`ReservationClient` は authenticated binding resolver と candidate dispatch を必須とし、acquire/check/renew/release で同じ正規化を使います。既存 proxy の `candidate_reservations()` は、既存 `_resolve()` で各呼出の binding/token を確認し、観測した cwd と明示した backend でこの client を作ります。既定の `tools/call` には接続しません。旧 proxy の相対 path 制約も維持します。

`hook_guard()` と `hook_release()` は同じ client を使います。resolver が未参加と確認した session は noop、認証済みの参加 session はどのフォルダでも予約を要求します。binding 不明、認証失敗、通信断は block です。勝手な登録や token 探索は行いません。共通 shell の `reservation_absolute_paths()` は同じ CLI に明示した cwd を渡す候補 helper で、既存 hook からの呼出はありません。sandbox、OS の権限、Codex add-dir、閲覧許可は変更しません。

release adapter は旧 hook と同じ失敗結果の判定を保ちます。tool_result/tool_response/tool_output の error、success:false、failed/blocked status、エラー文字列（入れ子を含む）は noop で、binding 解決と release の dispatch を行いません。成功した Edit の後は解放でき、失敗後の再試行では予約を維持します。

server の batch acquire は競合が1件でもあれば全件拒否します。共有同士は許可し、片方が exclusive なら競合です。check は具体的な file に限ります。renew/release は owner を確認し、path selector は正規化した同一の予約 pattern を選びます。ID と path の両指定は和集合です。selector 省略はその owner の予約全件、明示した空 path list は拒否します。TTL/延長は60–86,400秒で、期限切れを renew して復活させません。

## 活動と回収

renew は有効な現在の期限に延長秒数を足します。対象の無い renew は agent の活動を更新しません。

`probe_activity()` は各予約の固定 prefix と実対象の metadata を調べ、各実対象の所属 repo と repo-relative pathspec の Git timestamp を調べます。複数 repo や symlink の先も、それぞれの repo を調べます。file 内容と Mail DB は読みません。空 glob/新規 file でも正しい anchor を確認し、対象を消した直近の Git commit を探査します。probe 全体は3秒/1,000 steps、Git subprocess は残り時間で timeout します。権限拒否、timeout、上限、anchor の変更、不明な repo/Git 失敗は `activity_unknown` です。最近の file/Git/Mail/agent 活動と初回書込 grace を守り、unknown は stale の証拠にしません。virtual は filesystem 探査を行いません。

`collect()` は内部の明示 GC で1回100件までを扱います。stale 候補は再 probe し、lease revision と agent/Mail activity の世代を commit 前に再確認します。probe 中の renew/Mail 活動、最終 probe の file 活動があれば早期解放しません。最後の metadata 確認の後の OS file 書込を排除する transaction は保証しません。期限切れと owner release は unknown と独立で、unknown を理由に TTL を延ばしません。

## 検証と後続

一時 HOME の fixture で別 cwd、同じ絶対 file、symlink・新規 file・glob・Unicode・case・空白・WSL、認証・owner・TTL、別 repo の Git 活動、空 glob、削除 commit、probe unknown、probe 中の活動変更、候補 hook の未参加と通信断、既存 proxy の旧 mode を確かめます。日常運用で新方式を使えるようにするのは、永続化/移行の検証と writer gate、hook/binding 接続を実装した後の切替 PR です。
