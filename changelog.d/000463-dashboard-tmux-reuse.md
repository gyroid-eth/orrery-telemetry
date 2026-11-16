### Dashboard の tmux の起動を減らす

provider の停止を見る 5 秒ごとの確認が、動いている Codex の agent ごとに毎回 tmux で PARENT_AGENT を読み直していたのを、session ごとに 1 回にしました。`/api/graph` の親子の読み取りも、10 秒ごとに全 session へ起動していた tmux を、新しい session だけにしました。`/api/graph` の答えも `/api/agents` と同じく 4 秒だけ使い回し、POST の操作のあとは捨てます。provider の停止を見る間隔は 5 秒から 10 秒にしたので、停止の検知は、停止から通知まで 5〜10 秒だったのが 10〜20 秒になり、最大で約 10 秒遅れます。API の形と各値は変わりません。
