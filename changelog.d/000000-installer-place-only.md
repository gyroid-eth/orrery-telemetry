### service を操作せず stage に payload を配置する

installer の `--place-only` は、明示した新しい stage 専用の配置先へ既存の payload・template・env・manifest を生成します。service の probe・起動・停止と、利用者の settings・skill link の変更を行いません。既定の live 配置先や既存の path は作用前に拒否します。

`--print-inventory` は同じ payload の render 後の SHA・mode・種類を、ファイルを作らず JSON で出力します。
