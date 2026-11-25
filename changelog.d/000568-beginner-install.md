### 初回導入で詰まる所を直す

Hermes を使うときは、始める前に xz と libatomic を調べ、足りなければ入れる command を 1 行で示して止まるようにしました。
lsof が無い環境でも、自分の dashboard が持つ port を「使用中」と取り違えなくなりました。
Hermes の初回取得は固定 commit だけを浅く取り、進み具合を逐次表示します。
selftest と doctor は、共通部分の合否と Claude の MCP 登録の未承認を分けて示します。
ChatGPT MCP の既定 port を 8792 にして、cockpit の 8791 と分けました。
`--dry-run` が HOME に .pyc を書かなくなりました。
