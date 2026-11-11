### fresh の登録で Claude Channels を起動

準備済みの fresh context を指定した常駐 profile から、通常の起動・本人照合・予約 hook を共用して Claude Channels を起動できます。停止時は provider の終了を待って終了記録を残し、同じ本人で再起動します。異常終了は結果不明として次の起動を止めます。旧形式の profile の動作は維持します。
