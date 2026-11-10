### 子を立てる手順に、サーバーから model を読む Local の指定を足す

delegate skill の DeepSeek・Local の子を立てる手順で、provider には catalog が返した接続の id をそのまま送るようにしました。サーバーから model の一覧を読む Local では、選んだ model 行の `url_index` と `url_key` を写して送ります。これまでの例ではこの2つが無く、起動が `spawn_pair_unavailable` で断られていました。
