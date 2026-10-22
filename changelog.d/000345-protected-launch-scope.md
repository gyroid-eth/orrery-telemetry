### 予約の保護範囲を起動先フォルダ基準に変更

新規の top-level agent と対応する delegated child の予約保護範囲を、Git リポジトリ全体ではなく実際に起動した作業フォルダから計算するようにしました。共有フォルダの追加指定は `AGENTSTACK_EXTRA_PROTECTED_ROOTS` で行い、installed `env.sh` の旧保護範囲を更新時に一度だけ引き継ぎます。旧設定が曖昧な場合は更新を停止し、移行の確認と予約解放・関係セッションの再起動を案内します。Mail の名前空間は変更しません。resume / persistent と warm pool の追加保証は後続の対象です。API 世代は変更しません。

詳細は [保護設定の移行](../docs/configuration.md#reservation-の保護範囲と移行) を参照してください。
