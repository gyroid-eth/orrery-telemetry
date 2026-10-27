### Changed
- dsh の compact の予算を選択した model の context に合わせました。動的 model の出力上限は context の 1/4 と 8192 の小さい方になり、選択した model の route だけに headroom 10%・閾値 0.8・保持 0.16 の policy を入れます。context 131072 では compact が始まる量が 57344 から 104857 になります。minimal と他の model は変わりません。
