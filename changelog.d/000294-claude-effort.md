### Claude の子にも Effort を選べるようにする

起動画面で Claude を選んだときも Effort を選べるようになりました。選べる段階は Claude Code の公式文書にあるモデルごとの表（Opus 4.6 と Sonnet 4.6 は xhigh なし、それ以前の対応外のモデルは無し）に従い、選んだ値は `claude --effort` で渡します。指定しなければ従来どおり何も渡しません。Effort を指定した子は、事前起動の待機セッションを使わずに新しく起動します。Codex の Effort の挙動は変わりません。Helper model は Claude Code に専用の指定手段がないため、対応していません。
