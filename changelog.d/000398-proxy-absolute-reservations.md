### 共通 bound proxy の予約を managed guard と一致させる

共通 proxy の予約取得・更新・解放が物理絶対 path を受け付け、managed `workspace-v1` の編集 guard と同じ予約を参照できるようになりました。相対 path / glob の既存互換と `..` 拒否は維持します。managed の拒否文と agent 向け案内に必要な path を示し、[更新後の取り直し手順](../docs/codex-app.md#agent-の利用手順)を日英で追記しました。guard を相対照合へ戻す変更や、既存予約の自動変換はありません。
