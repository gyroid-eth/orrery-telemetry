### WSL でも Gemini を対話で起動し、一時ブラウザーを選べる

WSL（Linux）でも、Gemini（Antigravity）を親なし・親ありとも Mac と同じ対話の session で起動するようにしました。これまでは親なしの起動が断られ、親ありの子は 1 回答えると終わっていました。一時ブラウザーの判定で使う Node の見つけ方も共通にし、dashboard の PATH に node が無くても、dsh 用の Node や nvm の Node を使います。WSL の Codex でも一時ブラウザーを選べるようになります。
