### WSL の Gemini で My Chrome と Computer use を選べる

WSL で Gemini（Antigravity）を起動するときも、Windows の Chrome を使う My Chrome と、Windows Cua Driver による画面の閲覧・操作を選べます。Mac と WSL の Gemini には、起動時に ORRERY Mail と、選択したブラウザーの MCP ツールの呼び方が伝わります。

Mac と WSL の Gemini は、起動時に選んだ MCP ツールの準備を Antigravity で確認してから、最初のタスクを送ります。

Mac と WSL の NEW AGENT で、My Chrome に接続できないときは起動前に理由と準備手順を表示します。Chrome の停止やリモートデバッグの不通を確認できたときは、一時ブラウザーまたは Off が初期値になります。Telemetry API 58 の起動カタログに My Chrome の状態を追加しました。

WSL の一時ブラウザーは、WSL 内で Google Chrome または Chromium と Node.js を使えるときに選べます。

### 通知の送信後に入力欄が空になったことを確認する

Enter や Tab の直後に一瞬だけ空欄が見えても、通知がまだ入力欄に残っていれば送信済みにはしません。同じ入力欄が空の状態を保ったことを確認してから送信済みとします。

### 手動で送った通知の貼り直しを防ぐ

一度貼り付けを始めた通知は、同じ下書きが端末に残っている場合にだけ再試行します。下書きが消えたあとに送信を確認できないときは、貼り直さず Mail の遅延表示に出します。既読や確認応答で、その通知の待ち状態を解消できます。

### Antigravity の折りたたまれた長い通知を送信する

Antigravity が長い通知を `[Pasted text #N M chars]` と表示したとき、通知係が貼り付けた通知全体の文字数を照合して送信します。同じ下書きが残った場合は、貼り直さず再試行します。

複数行を `[Pasted text #N +L lines]` と表示する場合は、貼り付けた行数を照合します。
