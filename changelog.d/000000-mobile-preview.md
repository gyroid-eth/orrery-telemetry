### スマホの会話の記録で、fileと画像のpreviewを開けるようにする（History の opt-in の中・既定オフ）

`--enable-history`を付けたreaderでは、Conversationのtoolの呼び出しに出てきたfileと画像のchipを押すと、今の中身をpreviewで開けます。readerはcockpitの固定のroute `GET /telemetry/conversation/preview`をloopbackで呼び、記録・binding・作業folderの範囲・canonical path・大きさ・画素数・秘密らしい名前の確認は、cockpitがそのたびに行います。スマホが送るのはagentの名前とreaderが発行した参照のIDだけで、pathもcockpitの参照も送りも受け取りもしません。textは先頭32 KiBを文字として表示し、HTMLやSVGも実行しません。画像は画素だけのPNG（1辺8192 px・8 Mpx・4 MiBまで）に限ります。**画像はredactできない**ので、撮影に写った秘密はそのまま届きます。Close・背景移行でpreviewを消します。詳しくは[スマホで状況を見る](../docs/mobile.md#スマホでagentの会話の記録を読む)を参照してください。
