### Dashboard の CPU 負荷を下げる

`/api/agents` の答えを、計算を始めてから 4 秒だけ全 scope で使い回します。spawn・kill・exit・annotate・reactivate など POST の操作のあとは、すぐ作り直すので、操作の結果は次の読み取りに出ます。API の形と各行の内容は変わりません。実行中でない行の会話 record の結び付き、Mail の最後の指示、時刻の変換も、変わっていない間は読み直しません。
