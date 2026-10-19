### global 起動の nonce を位置引数として保持

global mode の起動・再開で、起動ごとの照合値が `-` で始まると内部 CLI が option と誤認して失敗する問題を修正しました。detached wrapper と provider 起動の両方で option を終端し、owner・起動世代・process の照合は維持します。
