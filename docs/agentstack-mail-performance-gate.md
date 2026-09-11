# ORRERY Mail performance gate design

> English version: [agentstack-mail-performance-gate.en.md](agentstack-mail-performance-gate.en.md)

Status: design only. This document does not add a benchmark job, choose a
budget, or authorize an authority switch.

## Purpose and boundary

Behavior differential は、durable な Git log の中で計測ミリ秒と duration 由来の Rich
speed icon/footer を意図的に消し去る。それは他の点では厳密な compatibility gate を
deterministic に保つが、同時に遅い Core を live と同等に見せてしまう。performance
gate はその normalization より前で latency を観測し、独立に fail しなければならない。

最初の gate が対象とするのは、既存の serial success-path scenarios と versioned な
tool contract のみである。Concurrent reservations、負荷下の retirement、crash/stale-signal
recovery は、それぞれの pending product decisions が解決されるまで対象外のままとする。
gate は checked-in の frozen-live provenance から再構築した worker-private state を
使わなければならない。install 済みの service や既存の database を開いてはならない。

## What to measure

公開の `call_tool` boundary の周りで、performance-only の probe から
`time.perf_counter_ns()` を使う。Rich の表示テキストを clock として parse しては
ならない。

| Metric | Boundary | Why it is separate |
|---|---|---|
| Cold readiness | process start から server construction、および正確な tool-surface enumeration まで | tool timer がどれも捉えない import/schema/startup の regression を捉える |
| Per-operation latency | 3 つの ordered scenarios それぞれの各呼び出しの直前から直後まで | SQLite・archive・Git・signal の作業を保持したまま、regression を 1 つの contract operation に帰属させる |
| Workflow latency | 各 scenario の最初の operation から最後の operation まで | noisy な単発呼び出しでは隠れる累積 overhead と相互作用を検出する |
| Emitted speed class | 各 instrumented call について、normalization していない duration から導いた class | threshold-boundary の flake を blocking にせずに、Rich speed-class の blind spot を報告する |

Responses と durable state は引き続き Behavior oracle を通す。誤った state での
速い run は performance pass ではない。probe は raw nanoseconds、operation name と
ordinal、side、repetition、run order、Python/dependency fingerprint、source hashes、
runner identity を JSON artifact に記録する。

## What to compare

計測する repetition ごとに、同一の temporary filesystem の下で fresh な live と
Core の state を作成する。side は交互の順序（`live/Core`、次に `Core/live`）で
実行し、cache warming や host drift が常に一方に有利にならないようにする。
warm-up pair は破棄し、paired な `Core / live` ratio を計算する。異なる machine
の unpaired な sample を比較してはならない。

独立した 2 つの budget が必要である。

1. relative budget は、同一 run 内で Core と frozen live side を比較する。host が
   全体的に遅い場合でも extraction overhead を検出する。
2. versioned な absolute Core budget は、dependency や filesystem の変化により両
   side が一緒に遅くなった場合でも、user-visible な latency を保護する。

各 operation と workflow について、median、p95、paired median ratio、speed class を
報告する。Blocking には relative-ratio budget と absolute-p95 budget を用いる。
Speed class は report-only とする。class threshold 付近の sample は bistable で
あり、operationally 無意味な変化が required check を red と green の間で alternate
させてしまうことがあるためである。class の downgrade は summary で可視化されな
ければならないが、後で審査された design が明示的な margin や hysteresis を追加
しない限り、exit code は変えない。Aggregate-only の比較では不十分である。ひとつの
遅い inbox や reservation path が多数の安価な呼び出しに隠れてしまうことがあるため
である。

実装 PR では numeric limit を発明してはならない。まず選定した blocking runner 上で
少なくとも 30 回の paired repetition から calibration artifact を集め、outlier を
検査する。提案する PR lane は 2 回の discarded warm-up pair と 10 回の measured
pair を median-ratio check に用いる。release lane は 3 回の discarded warm-up pair
と 30 回の measured pair を p95 check と absolute check に用いる。

calibration artifact には、すべての metric と両方の sample count について、観測
された noise floor と、そのバッチが選定した confidence で識別できる最小の
regression を記録しなければならない。budget は、その lane で測定した noise floor
より厳しくしてはならない。より小さな変化を検出する必要があるなら、pair count を
増やすか runner の noise を減らしてから required check にすること。

審査済みの値は versioned な `performance-budget-v1.json` に commit する。この
fixture は live/Core の source hashes、Python と dependency set、operation order、
warm-up と measured の pair count、noise-floor method とその結果、class boundaries、
per-metric limits を固定する。したがって budget を変更することは審査された
product change であって、red build への自動応答ではない。limit を緩める変更は
すべて、遅くなった metric、観測された原因、その例外を裏付ける measurement
artifact を明示しなければならない。単に fixture を更新して build を green にする
ことは許されない。

## Where it blocks

timing assertion を Behavior differential に入れるのではなく、専用の required PR
check `agentstack-mail-performance` を追加する。この PR check は 10-pair の
calibrated batch を実行し、relative-ratio budget を強制しつつ speed class を報告
する。
より長い release check は p95 batch を実行し、指定された target-macOS runner 上で
absolute budget を強制する。その安定した runner と baseline が存在するまで、
absolute lane は report-only であり、authority cutover は no-go のままである。
`ubuntu-latest` の数値を Mac の latency guarantee として提示してはならない。

performance job は通常の test matrix とは分離する。repository のテストからの
parallel load が計測を汚染しないようにするためである。network アクセスや LLM
アクセスは持たず、state root を再利用することもない。Dependency/source の drift、
sample の欠落、operation trace の不一致、worker error、認識できない speed class
は、required check を黙って skip するのではなく、無効な benchmark として
fail closed させる。

## How it fails

有効な threshold failure の場合、非ゼロで exit し、実行可能な最小限の表を出力する。
内容は operation、live の median/p95、Core の median/p95、paired ratio、speed
classes、超過した versioned limit である。完全な raw JSON と compact な summary を
CI artifact として upload する。Infrastructure-invalid な run は performance
regression とは別に識別するが、どちらの状態も green ではない。Retry は evidence を
集めてもよいが、budget を書き換えたり inconclusive な run を pass に変えたりして
はならない。

初期実装が完了したと言えるのは、mutation test によって次のことが証明されて
からである。synthetic な Core delay が per-operation ratio と release-p95 の両方の
path を fail させること、class-only の boundary flip が exit code を変えずに
報告されること、そして timing sample の削除や source fingerprint の変更もまた
fail すること。

## What this gate cannot detect

この最初の gate は、concurrent reservation の latency や fairness（D10）、work が
concurrent に届いている最中の retirement（D11）、crash・recovery・stale-consumer
notification の latency（D12）を計測しない。これらは無視できる edge case ではなく
central な production condition である。除外されているのは、この最初の paired
gate に deterministic な contention・retirement・watcher recovery の workload が
ないためである。それらの product semantics は選定済みだが、この gate に通ること
は、それらの条件下での latency について何も語らない。

また、持続的な throughput collapse、memory/file-descriptor の増加、長時間稼働時の
queue buildup、network transport overhead、optional な LLM path、選定した runner
以外での machine-specific な挙動も検出できない。PR lane の paired な Linux
result はあくまで relative である。指定された Mac runner と absolute budget が
存在するまで、PR pass も report-only の Mac sample も、absolute な
target-machine guarantee ではない。除外された次元にはそれぞれ、authority
cutover の前に、選定済み semantics に基づく load・soak・fault gate が別途必要で
ある。
