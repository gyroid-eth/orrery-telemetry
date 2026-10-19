### local preview の入口と保存契約の準備

legacy の登録入口で明示 preview を型検査し、通常の cloud 登録の前段にも設定済み local owner の検査を追加しました。
API の互換世代は21です。実行 consumer は接続作業中であり、この段階では preview も登録前に未対応として停止します。
未検証の preview は native の権限上限・隔離を検証済みにはしません。

明示的な legacy operator の local-plan/local-apply は、固定の親の既存所有認証と同じ configure producer を通して prepared intent と資源を保存します。
設定完了は起動承認ではなく、通常の native gate は変更しません。登録の保証は通常の子と同等であり、atomic create-only は未保証として実行記録へ残します。

旧 resume・cleanup・登録が preview の保存欄や新 pending を上書き・削除しないよう拒否し、通常 credential の保存と最終 local cleanup を同じ名前 lock で再検査します。credential の保存失敗を登録成功として返しません。
内部の明示 resolve は、保存済みの親と子の所有認証だけで登録を確定し、応答不明から新規登録を再送しません。登録確定も provider の起動承認には数えません。
dashboard の失敗後にも同名の保護済み owner を削除・回復で上書きせず、保護/観測不能を明示する結果を返します。
private HOME の指示も明示 configure の同じ plan/manifest に含め、通常の利用者の指示や未所有 file を上書きしません。設定資源の検査であり、実 CLI の読込みや起動の受入ではありません。
preview の認証 helper も process 観測の前後に生成済み指示を再検査し、変更や欠落を鍵 command の前で拒否します。
同じ reader で native 設定と選択した skill link も再検査します。skill の列挙不能・余分な entry・link 自体の差し替えも拒否し、実 CLI の権限や skill の実効の受入とは区別します。
最初の preview は tools={} の Mail・予約・shell・Skill に限定し、外部 MCP の空でない tools は作用前に拒否します。private な MCP 設定と明示 Python の pin も同じ configure/再検査へ含めます。実 proxy の起動の受入はまだ別の段です。
内部 proxy factory は既存の stdio/core client を再利用し、各 call の同 owner・active launch・共通 process 検査で Mail の作用前に拒否します。別の identity 台帳は作らず、実 P/Q・hook・public 起動の受入とは区別します。
内部 P/Q entry は同 owner の unknown を子作成前に保存し、同 PID の exec と親の wait を記録します。子作成後の検査が失敗しても実 child を回収し、unknown を成功化しません。argv は共通 projection、env は閉じた legacy 値を使い、公開 spawn と hook/remote cleanup の成功経路は接続作業中です。
内部 SessionStart/End は同じ active 検査と owner CAS で receipt だけを保存し、End を回収・予約解放・retire の成功にしません。legacy hook の Python は明示 configure の pin に揃えます。実 native hook の受入ではありません。
global の prepare/resume Python API は明示 tools の文法を作用前に検査し、未知の組込み名を CHILD_TOOLS_INVALID で拒否します。正規の非空選択や既存の native gate は維持します。
preview の内部編集 hook は共通の active/session 検査と予約の renew・競合 readerへ接続します。post-edit の自動解放は行わず、明示の Mail release・期限切れ・終了 joinまで保持します。他の writer をTTL中止めうる違いを説明し、通常の子の90秒解放とTTLは変えません。実native受入や終了joinの完成とは区別します。
