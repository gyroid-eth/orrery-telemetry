### 起こし直したあとも Computer use の Off を名簿に表示

Computer use を Off にして agent を起こし直すと、名簿の `computer_use` が `off` ではなく空になり、cockpit の札がどれも選ばれていない表示になっていました。Off の選択を保存し、名簿と札に `off` を返すようにしました。画面操作の道具を渡さない動きは変わりません。古い版で Off にした agent は、この版で Off を選び直して起こし直した時点から表示が直ります。
