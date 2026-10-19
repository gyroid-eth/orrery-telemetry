# dsh 専用の起動計画

Nodeの取り違えとenv継承は通常起こり得、空成功や鍵漏れの害が大きいため、
起動計画は固定実体と明示入力だけから組み立てる。

`integrations/dsh/launch_plan.py` はpureな生成moduleで、共有launcherには未接続。
file/hashの実読取、Node実行、Keychain、登録、API、process起動は行わない。

1. 共通部品でowner・local gate・固定実体/版/digestを検証する。
2. 検証結果を `LaunchInput` として `build_launch` へ渡す。
3. 返るpatch断片をmanaged profileへ反映し、guard/TUI/実効設定を確認する。
4. 最後に `runtime_environment(plan, key)` を単一runtimeへ渡す。
   返却envはログ/保存しない。補助processには `auxiliary_environment(plan)`、
   proxyには製品共通factoryのenvを使う。

Nodeは絶対pathとSHA-256を必要とし、24.2未満・版不明を拒否する。
argvはshellを挟まず、固定Nodeの `--import` でguardを先に読み、固定dsh entryへ
`--profile orrery-dsh-tui --patch <managed patch>` を渡す。長いtaskや鍵は入れない。
このprofile、guard entry、patchのmaterialize/起動検証は後続の接続作業である。

public envには元のHOMEと専用DSH/TUI/session/cache rootだけを明示し、ambientの
NODE_OPTIONS、API key、Mail token等を継承しない。
`ORRERY_DSH_TUI_DATA_ROOT` と `ORRERY_DSH_LIMITS` は後続のmanaged entry/pluginとの
契約であり、上流が既に対応するenvだとは扱わない。

patchはsession-titleと補助LLM title、llm-retryを無効にし、providerの
maxRetriesを0、maxTokensを要求上限と同じ値にする。envの要求数・時間の上限は
後続guardが送信前に強制する値で、このmoduleだけでは要求数を止められない。
事後usageによる停止を総入力tokenのhard capとは呼ばない。

この計画の完成と、TUIを通常の子として起動可能であることは別である。
共有owner/proxy/process/local gateをdsh用に作り直さず、統合後に同じconsumerで
opt-in無効/有効のcatalog・spawn・asset配信を対照する。

```sh
python3 tests/test_dsh_launch_plan.py
```

試験はdummy鍵と合成pathだけを使い、実Node・鍵・ネットワークを使わない。
