### launchd から managed provider と worker の module を解決する

作業dirが `/` で PYTHONPATH が無い dashboard でも DeepSeek／Strata の catalog を返し、dsh／Hermes の worker が同じインストール先の adapter と core を読み込めるようにした。catalog と worker の失敗時は秘密を含めない診断を1行出す。
