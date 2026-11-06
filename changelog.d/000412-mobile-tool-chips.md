### スマホの会話の記録で、file の tool の行にも file と画像の chip を出す

cockpit は file や画像の参照を、file の tool の呼び出し（Read・Write・Edit・apply_patch・SendUserFile など）の行に付けます。スマホの Conversation は、chip を発言の吹き出しにだけ描き、tool の行では描いていなかったため、実際の記録では preview を開ける chip が出ませんでした。tool の行にも chip を描くようにしました。preview の守り（cockpit の確認・大きさ・PNG の確かめ）は変えていません。
