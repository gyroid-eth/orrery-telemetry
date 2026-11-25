### すべての harness で、通知と slash command を確かめて送る

Claude Code・Codex・dsh・Hermes・Antigravity のそれぞれの入力欄と候補欄を読み、選ばれた候補が送った command と一致したときだけ Enter を送ります。これまで dsh と Hermes では `/log` を送ると `/login` が、Antigravity では `/logout` が選ばれたまま Enter を送ることがありました。
dsh の入力欄は長い文の先頭を隠すため、通知を貼っても Enter を送らず入力欄に残っていました。見えている末尾が貼った通知と一致すれば送ります。Hermes が送信の直後に描く表示も読み、受領を確かめます。
