### WSL・Linux で古い Mail の pidfile から復旧する

ORRERY Mail の起動時、管理対象の runner を記録した pidfile よりも後に同じ PID の別プロセスが始まったと確認できれば、古い pidfile を片付けて起動を続ける。稼働中の runner と判別できない場合や、PID の再利用を確認できない場合の起動保護は維持する。
