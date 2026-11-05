### 別の場所の install を uninstall しても、スマホの reader の launchd job を止めない

`scripts/uninstall.sh` は、スマホの reader の job（`org.agentstack.mobile-relay`）を、uninstall する install のものであるときだけ止めて plist を消します。対象は、既定の場所（`~/.agentstack`）の install、manifest がその job を記録している install、job の `--config` がその install の `mobile.json` を指している install、job の reader の script がその install の中にある install の 4 つです。`--install-dir` で別の場所の install を uninstall したときは、job に触れません。

通常の uninstall の動作は変わりません。利用者への影響はありません。

試験の 1 つが、本物の HOME のまま uninstall.sh を走らせていました。そのため、試験を回したコンピューターで動いていた reader の job が消えたことがあります。この試験は一時的な HOME で走るようにしました。また、試験の間に本物の `~/Library/LaunchAgents` から ORRERY の plist（`org.agentstack.*`・`org.orrery.*`）が消えたら、あるいは ORRERY の job が loaded から absent に変わったら、試験全体を失敗させる確認を足しました。増えた側の変化は見ません。この確認は読むだけで、launchd を変えません。
