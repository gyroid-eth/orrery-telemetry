# dsh の標準 CLI 起動

薄い起動は upstream の dsh CLI をそのまま使い、ORRERY は子ごとの profile と provider/model/MCP/指示/skill の設定を生成します。通常の native tool と session の機能を使います。

`integrations/dsh/thin_config.py --input INPUT.json --out PLAN.json` は鍵を読まず、native argv・追加 env・cwd と guard の設定参照を返します。CLI の失敗は固定 reason の一行です。共有の標準 launcher が製品の Mail proxy を生成し、追加 MCP を合流してから入力します。stdio/http はコピーし、tool 単位の承認や filter を表現できない選択は拒否します。選択されていない MCP を空 map で止めたとは扱いません。dsh 用の明示 profile は固定 base/TUI bundles と生成した client 行だけをロードし、親の profile/plugin MCP はコピーしません。

install の opt-in は従来どおり `--enable-dsh`。同じ固定版でも新しい `-plain-datadir1` 世代を作り、検証済み tarball/lock を使って TUI の source は DATA_DIR を `DSH_TUI_DATA_DIR` で替える1行だけ変更します。旧 managed patch は使いません。owner receipt は `variant: plain-datadir` と `thin_config_contract: 1`、template は `generation_receipt` を参照します。旧 managed 世代は移行中も残し、古い receipt を薄い起動へ自動昇格させません。operator の template/manifest の配置は別作業です。

鍵は native credentials plugin が Keychain から一度だけ取得して memory に保持します。OS の入口は macOS の `/usr/bin/security`、他 OS は未対応です。取得失敗は固定 error、再取得 loop はありません。選択 ref は env/file に fallback せず、native の set/unset からも disk へ保存しません。標準 API-key adapter は credentials service を必須の依存として設定します。plugin の欠落を generator が静的に検出しますが、実登録を起動前に検査する preflight は作りません。

生成patchはbundledのcredentials行を無効にし、別idでKeychain pluginを挿入します。native patchの`name`は既存行の一致条件であり、pluginの置換には使いません。

通常の native tool はそのまま使えます。`orrery_tools.mjs` が native の tool 名・入力・実 cwd を、共通の #289 鍵読取 guard、登録、予約 hook へ渡します。判定の再実装や read/write/Mail だけの制限はありません。成功した編集の予約解放も既存 hook です。これは同一ユーザーの任意 code を隔離する sandbox ではありません。

固定版の補助plugin `working-activity` は依存packageが不足して起動時に警告するため、生成設定で無効にします。通常の状態行と`esc to interrupt`による作業中の判定は維持し、依存のlockやruntimeの世代は変更しません。

共通hookがexit 2で編集を拒否した場合は、公開のJSON理由またはstderr理由を先頭2000文字まで子へ返します。内部診断の形は除外し、他の非0は固定文、exit 0は公開の`systemMessage`だけを表示します。Hermesと同じ扱いです。

`helper_model` は省略/`same` または同じ backend の正式 ID。候補は manifest の `models`（省略時は `model` 一件）で、別資格の route は推測しません。明示の helper は native subagent の agentOptions に投影し、fork は親継承の既定を保ちます。`effort` は明示した場合だけ設定します。

native session 作成イベントから本人の `harness_state_dir/native-session.json` に ID を保存します。subagent の ID で上書きせず、保存失敗で起動は止めません。再開は `resume_session_id` を native `--resume` へ渡し、その子の profile/履歴を再利用します。MRU で本人の ID を推測しません。指示は共有 renderer の出力を読み、skill は共通の元へ link します。

再開時の任意入力 `native_profile_env: {DSH_HOME: 元の dir}` が生成予定と違う場合は、file/link を書く前に `native_resume_profile_changed` で拒否します。入力がない新規起動は従来どおりです。

TUI の状態は本人の `harness_state_dir/tui` に保存し、起動時に選択した provider/model をその中の `model.json` へ設定します。native `/model` が変更するのもこの子の file だけです。他の子と利用者の `~/.dsh-tui` は共有せず、HOME は変更しません。resume は同じ state dir を再利用します。env 未指定の通常の upstream TUI は従来どおり `~/.dsh-tui` を使います。

標準launcherからの子は英語の画面で起動します。生成時に`DSH_TUI_LANG=en`と子の`lang.json`を設定し、同じdirの`onboarding.json`を完了済みにして初回設定・移行pickerが初期入力を塞ぐことを防ぎます。利用者のTUI設定は読み書きしません。

この最小 patch は固定版の preimage と適用後 hash を別世代の receipt に記録します。旧い `plain` と旧 managed の世代は書き換えずに残し、現在の generator は明示の `plain-datadir` を要求します。upstream に同等の設定が入ったら patch を外します。

## Compaction の予算

動的 model の選択 context `C` は従来どおり server の `meta.n_ctx`、`context_override`、64,000 の順に決めます。生成する通常出力予算 `O` は `min(8192, floor(C/4))` です。`C` が16未満なら別の値へ置き換えず `invalid_compaction_context` で拒否します。server/override/default は由来であり、fallback の値は実測ではありません。

選択した provider/model の exact route だけに、TUI の公開 `preset-patch` で `standard` / `ptc` / `cordis` の compaction を設定します。`H=floor(C/10)`、summary 出力上限は `min(O,H)`、threshold ratio は 0.8、retain ratio は 0.16 です。native の閾値は `floor(min(C*0.8, C-O-H))`、保持量は `floor((C-O)*0.16)` です。例えば `C=131072`、`O=8192` では閾値104857、headroom 13107、summary上限8192、保持量19660です。これらは生成時の設定値であり、モデルの認可、serverの実 context や compaction の成功を保証しません。native は最新 request の出力上限を使って閾値を再計算するため、明示的な変更後は値が異なります。別 model に切り替えた場合に、元 model の予算を適用しません。`minimal` preset の compaction 設定は変更しません。

固定 backend は `model_definition.id` が選択モデルと一致し、正の `contextWindow` / `maxTokens` が有効な予算を作れる場合だけ同じ policy を付けます。それ以外は既存 native default のままです。生成された `launch.json` の `compaction.policy` / `reason` は、適用または見送りの理由を示します。既定を引き継ぐ場合も native の大きな固定 headroom は小さい context と整合しないことがあります。値が不明な場合は推測せず、backend の定義を確認してください。
