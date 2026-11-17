### Mail 復旧後の Claude 再登録で現在の会話を記録

Mail の起動前などに開始して shell 登録できなかった Claude session は、SessionStart の情報を後続の Bash に引き継ぎ、同じ identity の `agentstack-reregister` 成功時に現在の会話の receipt を保存します。通常起動の追加待機・Mail 呼び出しはありません。他人の代筆・別 project・古い receipt の拒否は維持し、credential がない identity の自動 enroll は行いません。`CLAUDE_ENV_FILE` がない起動や更新前の session は対象外です。詳しくは [hook reference](../docs/hooks.md#record-session-indexpy) を参照してください。
