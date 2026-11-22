# provider assetの出典

アプリのロゴ `orbit.svg` / `orbit-light.svg` は ORRERY 用のオリジナル
vectorです。第三者providerのロゴではありません。両者は同じ形のdark/light版で、
headerはページのtheme、faviconはブラウザのlight設定に合わせます。
dashboard coreの配布と公開demoには、既存のassetsコピー経路で含まれます。

Qwenの利用は運営者の判断（2026-10-19）。
公式 [Qwen組織](https://github.com/QwenLM) の
[avatar PNG](https://avatars.githubusercontent.com/u/141221163?v=4) をSVGへ埋め込み、
元のalphaと形状を保ったfilterでbone `#ece2cc` にした。再描画はしていない。
専用の再配布・再着色の許可は、確認した公式資料では見つからなかった。
Qwenのcode licenseをlogoの許諾とみなさない。lightでは既存の黒logo filterを使う。
その他のassetは [第三者コンポーネント](../../docs/third-party.md#provider-のロゴ) を参照。

## GLM (`glm.svg`)

GLMの利用は運営者の判断（2026-11-08）。
公式 [Z.aiサイト](https://chat.z.ai/) の
[logo SVG](https://z-cdn.chatglm.cn/z-ai/static/logo.svg) を埋め込んだ。
[公式organizationのavatar](https://avatars.githubusercontent.com/u/223098841?v=4) は暗い背景が不透明なため、背景pathを分離できる公式サイトのSVGを使った。
背景と枠を描く独立したpath（class `st194`）だけopacityを0にし、3つの前景図形の座標・形状と正方形のviewBoxは変えていない。トレース・再描画はしていない。
Qwenと同じalpha filterでbone `#ece2cc` にし、lightでは既存の黒logo filterを使う。
確認した [Z.ai Terms of Use](https://chat.z.ai/legal-agreement/terms-of-service)（更新日2026-04-14）のV.2は、事前の書面の同意なしのlogo・商標等の利用を制限している。
専用の再配布・再着色の許可は、確認した公式資料では見つからなかった。
この制限を承知の上で、運営者が2026-11-08にbone色の公式ロゴを使うと決めた記録であり、権利者からの許諾を主張しない。
