### doctor が Codex の予約の確認を誤って「不明」と出さないように

doctor は Codex の plugin の状態を、まず既定の手元の marketplace（`agentstack-local`）だけに問い合わせる。全体の一覧は外の marketplace にも問い合わせるので 1 分以上かかることがあり、5 秒の待ち時間を超えて「不明」になっていた。手元で見つからないときだけ、これまでどおり全体の一覧で確かめる。apply_patch の予約の確認も、子と同じ login shell の PATH で Codex を起動する。
