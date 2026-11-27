### WSL で起動中の子の worktree を Windows 側の prune から守る

Git の管理領域が Windows drive 上にある場合、子の worktree を作るときに理由付きでロックします。子が終了すると ORRERY のロックを解除し、変更の確認に使えるよう worktree を残します。
