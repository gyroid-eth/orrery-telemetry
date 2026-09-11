# ORRERY Mail claim and enrollment design

> English version: [agentstack-mail-claim-enrollment-design.en.md](agentstack-mail-claim-enrollment-design.en.md)

Status: supporting design for the normative product-decision ledger. D7's
selection、implementation state、cutover state は
`packages/agentstack_mail/fixtures/differential-expected-divergences-v2.json`
にのみ存在する。この文書は enrollment・rotation・recovery・principal・
administrator の mechanism を選定するものではない。D6 の選定済み
upstream-parity behavior を変更するものでも、D7 を implement・enforce する
ものでも、authority cutover を実施するものでもない。live service や
database への access を authorize するものでもない。D1 の narrow で選定済みの
failure-atomicity rule は input constraint であり、ここで reopen する選択では
ない。

## One-line summary

安全な path には、secret を含まない enrollment transport、null-token row の
外にある authority、明示的な rotation と loss recovery、authenticated な
macro bootstrap、そして将来の strict な D6 behavior や選定済み D7 boundary を
enforce する前の additive で shadowed な migration が必要である。

このパケット全体を通じて、**claim** は authority を確立または復元する flow
（enrollment、recovery、administrator approval、binding-backed continuity、
audited transfer）を指す umbrella term として用いる。ここで別途選定された
primitive・endpoint・proof source ではない。

## D7 rationale and design implications

manifest ledger が D7 の正確な選定済み scope の唯一の source である。この文書は
その decision を再述するものではない。ここでは、その選定になぜ enrollment の
前提条件、principal/admin の別設計、lifecycle test が必要なのかを記録する。

Name-only な retirement は timing-selectable な receive-denial 攻撃である。後の
unretire では、row が retired だった間に拒否された send を回復できない。現時点で
影響を受ける row がゼロだと観測する machine inventory があっても、その事実が
product guarantee になるわけではない。

現在の D7 の runtime behavior は変更されておらず、shadow verdict も enforce
されていない。ledger は D7 を selected・not implemented・cutover no-go と記録
している。D6 は pre-existing な upstream parity として selected かつ
implemented されているが、cutover は依然 no-go である。この selection は
Path A の omitted-token behavior を保持するものであり、より strict な
enforcement を authorize するものではない。既定の deployment model は依然、
authorization enforcement のない単一の local principal であり、principal の
subdivision は後続の作業である。

24-tool permission catalog は、その D7 の行が normative ledger を反映する場合を
除き、prospective で non-binding な inventory である。特に、その候補である
`send_message` の owner/admin rule は D6 の selected upstream-parity rule を
置き換えるものではなく、その他の行も enforcement を authorize するものではない。

## New credential issuance and return

### Boundary and current evidence

このパケットは differential の観測結果を受けて決めるべき decision を narrow に
絞るものである。以下の non-binding な lean を requirement に変えるものではない。
既存 installation における各 credential state の prevalence は不明であり、
将来の inventory は別途 authorize されなければならず、raw credential を
export してはならない。

checked-in の implementation は次の constraint を確立している。

- `register_agent` は caller が指定した `registration_token` を受け付け、既存の
  token を保持し、どちらもなければ新しい random token を生成する。
- `_agent_to_dict` は意図的に `registration_token` を除外している。隣接する
  comment には、以前 return していた際に JSONL conversation log 中に plaintext
  で 1,554 回を超えて出現していたことが記録されている。
- `Agent.registration_token` は nullable であり、現在は indexed な plaintext
  value として保存されている。
- Frozen live は `ctx` を除くすべての bound argument を Rich logging に送って
  いる。Core は現在、Rich serialization の前に credential-suffixed な
  top-level argument を redact し、authorization の shadow observation を
  4 つの credential-free field に限定している。
- Frozen live は、競合する D1 re-registration が拒否される前に identity
  metadata と archive mutation に到達することを許している。Core の selected
  D1 rule は現在、明示的に競合する token を durable mutation の前に reject し、
  null-token row に対する concurrent registration に一人の atomic writer しか
  いないよう conditional な DB write を用いている。first-DB-writer の結果は、
  unsafe な compatibility arbitration として保持されているのであって、accepted
  claim proof ではない。D6 の selected upstream parity は、omitted-token の
  sending を owner proof に変えることなく保持している。selected D7 boundary は
  実装されておらず、D1 の writer result を owner authority として
  reinterpret するものでもない。
- `macro_start_session` は credential を受け付けず、直接 `_get_or_create_agent`
  を呼び、その後、新しく作成された null-token identity に対して file の
  reservation と inbox state の読み取りを行うことができる。
- D3 は 2 つ目の new-null path を dynamically に確認している。approve された
  cross-project send は、token management なしに target project 内で
  `_get_or_create_agent` を呼び、`registration_token` が null な
  target-local な sender alias を作成する。これは D7 の既存の
  stop-all-new-null な前提条件についての measured evidence であって、D7 の
  selected boundary への変更ではない。
- D6 の下で selected されている現在の behavior は、tokenized な sender が
  `sender_token` を省略することを許している。現在の Core は依然、row の
  token が null の場合に name-only の owner operation を条件付きで許して
  いる。これは selected だが未実装の D7 boundary ではない。
- D1 の result/profile test は、end-to-end の secret non-disclosure を確立
  していない。Core の新しい Rich redaction は既知の top-level token path を
  閉じているが、nested な alias、exception、将来の transport surface には
  依然 end-to-end の canary gate が必要である。
- 既存の launcher はすでに有用な primitive を示している。caller が生成する
  token、mode `0700` の directory の下にある mode `0600` の file、atomic な
  replacement、literal な argv ではなく stdin による transport、そして
  model-facing な tool surface の外に token を保つ proxy である。Codex App
  bridge も、owner-token file とは別に durable な external-ID binding を
  保持している。

これらは implementation の observation であって、現在の storage や launcher
design を承認するものではない。

Enrollment には confidentiality requirement の異なる 2 つの output がある。

1. identity result: canonical な project/name、credential version、
   enrollment state、non-secret な receipt または fingerprint。
2. 再利用可能な owner credential: high-entropy な bearer secret、あるいは
   それと同等の non-exportable な binding。

通常の tool result は 1 つ目の output を運んでよい。product が明示的に
plaintext-return option を選び、その archive への露出を受け入れない限り、
2 つ目の output を運んではならない。

### Issuance and delivery options

| Option | Security and operational shape | What breaks / who is affected | Existing-data effect |
|---|---|---|---|
| Caller が token を生成し、通常の `register_agent` の argument として渡す。server は receipt/fingerprint のみ返す | 単純で既によく理解されているが、secret が model/tool request に入り、両方を redact しない限り現在の Rich kwargs にも入る | model-driven な caller、trace、log collector が secret を意識する必要がある。token を生成・保持できない caller は enroll できない | 既知の caller token は引き続き使用可能。生成済みだが利用不能な token と null-token の row は回復されない |
| Caller が token を生成し、信頼された launcher/proxy が private file、file descriptor、または authenticated な local channel からそれを注入する | secret を model-facing な schema と result から遠ざける。既存の launcher と Codex App の接点と整合する | direct な remote MCP client には同等の secure transport が必要。server は caller が任意に選んだ filesystem path を受け付けてはならない | 既存の private token file と bridge binding は、ownership と permission の check を経て初めて再利用可能になり得る。null row が自動的に claim されることはない |
| Server が token を生成し、pre-authorized な secret sink に一度だけ書き込み、receipt/fingerprint を返す | server が entropy を制御し、通常の result に secret を出さない。成功には atomic な DB/sink protocol が必要 | remote な sink、crash recovery、sink authentication、retry semantics が protocol を増やす。sink を持たない headless client は enroll できない | 新しい enrollment には使えるが、以前生成されて利用不能になった token を再構築することはできない |
| Server が生成した token を一度だけ plaintext で返す | 補助 channel を持たない汎用的な client でも動く | 既知の JSONL/result の漏洩を再現し、すべての intermediary、UI、logger、transcript store が result を保護する必要がある | row の migration は不要だが、漏洩済みの古い token は retrospective な保護ではなく rotation が必要 |
| Server が短命で単回使用の enrollment handle を発行し、2 つ目の trusted channel がそれを redeem する | identity 作成と secret 配送を分離し、未完了の enrollment を expire させられる | handle 自体が一時的な bearer secret になる。新しい pending state、expiry、replay 保護、2 回目の round trip が必要 | 既存の row は、handle を割り当てられただけでは authority を得ない。別途 claim proof が必要なままである |
| Trusted bridge が non-exportable な process/session binding を保持し、agent に bearer を露出せずに call を authenticate する | model process から再利用可能な secret を排除し、既に Codex App proxy に似ている | 汎用的な client、process migration、disaster recovery、bridge unavailability に別途 path が必要 | disputed claim より前に記録された binding のみが evidence になり得る。現在 unbound な row には別の route が必要 |

この選択は組み合わせでもよい。例えば proxy が caller-generated な
credential をローカルで注入する一方、remote client は one-time sink を
使うこともできる。transport を組み合わせても、異なる ownership semantics を
生んではならない。

### Credential verification at rest

Issuance transport は、server が credential をどう verify するかを決めない。
その decision にも明示的な compatibility 対応が必要である。

| Option | What breaks / who is affected | Existing-data effect |
|---|---|---|
| Plaintext な bearer value を保持し続ける | database の read が即座に impersonation を許す。indexed された secret value は accidental な露出も広げる | rollback の互換性は最大。既存の値はすべてそのまま残る |
| versioned な one-way verifier と non-secret な fingerprint を保存する | plaintext の recovery は design 上不可能になる。diagnostics と client は retrieve ではなく rotate する必要がある | 既存の plaintext value には dual-read migration が必要。plaintext の削除は不可逆であり、rollback window の後にのみ行う |
| bearer material を external な secret service に置き、row には reference/verifier のみ保持する | availability、deployment、backup、authority の依存が増える | 既存の row には明示的な import が必要、または legacy verifier に留まる。rollback は legacy representation を保持しているかに依存する |

random な bearer token は、equality verification のために decryptable で
ある必要はない。digest を選ぶ場合は versioned かつ domain separated で
なければならない。server-side の keyed verifier は offline な table copy の
価値を下げる。厳密な construction、key custody、schema は decision 後の
作業である。

### Leak-prevention contract

Secret の安全性は response field の省略ではなく、end-to-end の property で
ある。将来の design では、owner credential と one-time enrollment handle を
次の surface すべてで tainted value として扱うべきである。

| Surface | Required invariant | Current or residual risk to test |
|---|---|---|
| Tool request と model transcript | model-facing な call は credential reference か bound proxy を使う。secret-valued な compatibility parameter が残る場合は、instrumentation の前に redact する | Core は Rich logging の前に top-level の credential-suffixed field を redact する。frozen live、alias、nested payload、将来の instrumentation は依然 canary-test の対象である |
| Tool result と error | structured content、text content、formatting variant、exception data、retry hint のいずれにも再利用可能な secret を含まない | `_agent_to_dict` は安全だが、将来の enrollment と macro の result は regression し得る |
| Logs and telemetry | serialization の前に schema/taint で redact する。値を interpolate しない。traceback local と request dump を避ける | field-name のみの redaction は alias、nested payload、formatted な JSON 文字列を見落とす |
| Messages, profiles, archives, Git, dashboard, and notifications | state、receipt、version、correlation に安全な fingerprint のみを保存する | mail に送られた credential は、後で削除しても既に disclose されている。Git history は durable である |
| Process argv and process listings | literal な credential を argv に置かない。保護された file descriptor、stdin、local proxy を通してバイトを渡す | path や identity name は可視のままかもしれないが、authority として受け付けてはならない |
| Shell history and terminal transcript | `export TOKEN=...`、literal な `curl` の argument、secret を print する command substitution、echo された prompt を要求しない | wrapper が tracing を有効にしていると stdin もキャプチャされ得る。script は boundary 周りで secret の echo/xtrace を無効にする必要がある |
| Environment | inherited または tmux-global な environment に再利用可能な credential を置かない。互換性のために environment variable が必要なら、1 process に scope し、child launch 前に scrub する | crash report と environment dump が値を複製し得る |
| Secret files and handoff files | 親 directory は `0700`、file は `0600`。no-follow と regular-file の check。exclusive create。fsync と atomic install。one-shot な source は durable な成功の後にのみ削除する。commit や sync は絶対にしない | 現在の child state JSON は token を複製しており、copy と cleanup の義務を増やしている |
| Database and backups | 選定した verifier を意図的に保存する。export、fixture、query telemetry、diagnostics から raw credential を省く | 現在の plaintext indexed column と backup は、別途 migration するまで sensitive なままである |

result/log/message/argv/history の scan は release gate であり、best-effort
な documentation ではない。secret-canary test は、failure path と retry
path を含め、予期しない出現があれば fail すべきである。

## Existing null-token authority establishment: proof is the central decision

### What the existing row can and cannot prove

null-token の `Agent` row は identity label と mutable な operational
metadata を含むが、owner secret は含まない。したがって row 単独では、どの
caller がそれを所有しているかを証明できない。project key、name、task
description、program/model、timestamp、inbox の内容、tmux session name、
pane title、hostname、`AGENT_NAME` は identifier または ambient な事実で
あって、ownership の proof ではない。

`WindowIdentity` 単独ではこの gap を埋めない。その UUID/display-name の
association は、現時点で secret-backed な claimant verifier ではない。
claimant が過去の message や filesystem path を引用できることも同様に、
cryptographic あるいは administratively delegated な authority ではない。
first-come の name-only claim は、既知の D7 の弱点を永続的な account
takeover に変えてしまう。

したがって、現在の null-row data のみからの自動的な authority establishment
は不可能である。claim という umbrella の下にある flow は、row の外にある
authority に依存しなければならない。それは事前に確立されたか、operation の
時点で trusted な administrator によって明示的に delegate されたものである
必要がある。

### Candidate proof authorities

| Option | What the proof actually establishes | What breaks / who is affected | Existing-data effect |
|---|---|---|---|
| private channel を通じて、claim より前に server が記録した cryptographic/runtime binding に challenge する | claimant が、claim 以前に作成され、この正確な project/name に既に紐付けられている binding を制御していること | そのような binding を持たない null row は self-serve できない。private channel の喪失には recovery が必要 | 検証可能に既存の bridge/binding record のみが該当する。claimant が claim の最中に作成した binding は何も証明しない |
| project administrator が、別途 authenticate された local control plane を通じて approve する。OS peer credential で強化してもよい | 指定された administrator が ownership を delegate する。元の process が戻ってきたことは証明しない | 定義された project-admin authority、人間の UX、headless policy、audit が必要になる。共有 OS account は peer-credential の意味を弱める | row が proof を提供したふりをせずに、審査済みの legacy row を回復できる。現在の project-owner record は前提とされていない |
| project-admin credential が remotely claim を authorize する | 確立された project-level authority の保持者が ownership を delegate する | impact の大きい credential と project ownership の lifecycle を導入する。compromise は多くの identity に影響する | 既存 project には、これが役立つ前に意図的な admin enrollment が必要 |
| すでに authenticate された project peer の quorum が claim を witness する | 選定された peer set が continuity に合意する | peer が unavailable だったり結託したりし得る。quorum policy と conflict は複雑 | authenticated peer が少なすぎる project では使えない。過去の message だけでは witness にならない |
| pre-issued な recovery code、または hardware-bound な recovery key が continuity を証明する | authority が健全だったときに確立された recovery material を claimant が保持していること | recovery material が発行されなかった row には使えない。custody と紛失は user の責任になる | 将来に向けてのみ適用され、今日の unauthenticated な null-token row には適用されない |
| 新しい enrolled identity を作成し、administrator-approved な continuity transfer/alias を行う | サポートされない ownership claim を行わない。history の continuity は明示的な administrative action である | stable な name、inbox、reservation、audit semantics が変わり得る。transfer tooling は相当な量になる | 古い row を evidence として保持し、history を silently に書き換えずに新しい principal に link できる |
| project と name を知っている最初の caller が row を claim する | public/ambient な identifier を知っているだけ | takeover と race を許す。すべての null-token owner に影響する | すべての null row を attacker-wins-on-first-use に変えてしまい、意味のある authority proof にならない |

product は、endpoint や schema を design する前に、どの external authority が
存在するかを決めなければならない。「local machine」では精度が足りない。
verified peer credential を持つ private socket、user confirmation、既に
bound された bridge は、それぞれ異なる trust scope を持つ。

### Authority-establishment invariants independent of the mechanism

viable な authority-establishment flow は、mechanism によらず次の性質を
すべて満たすべきである。

- proof を、正確な immutable project ID、agent row ID、canonical name、
  credential version、claimant、nonce、expiry に紐付ける。
- row を 1 つの transaction 内で re-read し compare-and-swap して、ちょうど
  1 人の concurrent claimant だけが `unclaimed -> enrolled` へ遷移できる
  ようにする。
- non-null な credential の silent な置き換えを拒否する。それは claim では
  なく rotation または recovery である。
- retired な row、active な reservation を持つ row、未読の work を持つ row を
  enrollment の side effect ではなく明示的な case として扱う。
- authority proof と選定済みの secret-delivery acknowledgement の両方が
  成功するまで、新しい owner credential を persist しない。あるいは owner
  operation を一切露出しない retryable な pending state を定義する。
- wrong、expired、replayed、cancelled、losing-race な proof は、DB
  metadata、profile bytes、archive Git state、reservation、inbox read
  state、signal のいずれにも変化を生じさせない。これは D1 と同じ
  failure-atomic な形をした prospective な claim invariant であり、D1 の
  selected かつ implemented な scope の拡張ではない。
- authority type、approver/binding identifier、old/new の enrollment
  state、timestamp、non-secret な credential fingerprint を audit する。
  secret を audit してはならない。
- 再利用可能な credential を返さずに、receipt によって retry を idempotent
  にする。

state label（`unclaimed`、`pending`、`enrolled`、およびその他の legacy な
variant）は例示であり、この文書は schema を選定しない。

## Rotation, loss, compromise, and recovery

これらは異なる operation であり、弱い fallback を共有してはならない。

- Rotation: caller が現在の credential をまだ持っている。
- Loss recovery: caller が現在の credential をもう持っていない。
- Compromise recovery: 古い credential が attacker に制御されている可能性が
  ある。
- Null-token claim: row が一度も使用可能な owner proof を持ったことがない。

### Options and breakage

| Option | Suitable case and guarantee | What breaks / who is affected | Existing-data effect |
|---|---|---|---|
| Current-credential-authenticated な atomic rotation | 通常の rotation。古い credential が replacement を authorize する。任意で短い明示的な grace window を設けてもよい | private store を atomic に更新できない client は continuity を失い得る。grace は一時的な dual authority を許す | 既存の token を保持している owner には有効。利用不能な生成済み token には使えない |
| verifier として保存された pre-issued な single-use recovery code/key | recovery material を事前に用意していた場合の loss または compromise | user が recovery material を保存する必要がある。code の盗難は takeover になる。code には revocation、replenishment、replay 保護が必要 | 既存の ownership が証明された後に securely に発行されない限り、将来に向けてのみ |
| 後で選定される project-admin/local authority による recovery | 古い credential なしでの審査済み recovery | administrator の availability と trust が必要。unattended な job は止まり得る | 明示的な審査の後、生成済みで利用不能な cohort と null cohort をカバーできる。history を silently に書き換えるべきではない |
| pre-existing で non-exportable な bridge binding による recovery | bearer file を失っても、bound された runtime の制御を証明する | binding migration や bridge の喪失には route がない。新しく捏造された binding に耐性を持つ必要がある | 該当する事前の binding を持つ row のみが恩恵を受ける |
| 新しい identity と audited な continuity transfer | 有効な authority が残っていない場合の最も安全な fallback | stable な name と history の consumer に alias/transfer のサポートが必要かもしれない | 古い row を破棄しない。既存の mail と audit は attribution 可能なまま残せる |
| Server が保存された plaintext credential を開示する | plaintext が存在すれば access を回復する | database/server の operator が credential delivery authority になる。disclosure は owner と requester を区別できず、one-way storage を無効化する | verifier migration の後は不可能。現在の plaintext row と backup を露出する |

古い token の rotation は compare-and-swap にすべきである。古い credential を
verify し、新しい verifier/sink receipt を install し、credential version を
increment し、それから古い version を revoke する。この decision では、
grace window が存在するか、compromise が疑われた場合に誰が早期に終了できる
か、sink と DB の commit の間で crash した場合にどう reconcile するかを
明示しなければならない。Loss recovery は null-claim の節で拒否された
ambient な事実に fallback してはならない。plaintext の credential は決して
「回復」されるのではなく、ownership が再確立され、新しい credential が
rotate される。

## `macro_start_session`

現在の macro は、credential parameter を持たずに identity の作成/再利用、
任意の reservation、inbox access を組み合わせている。strict な enrollment
design の下では、macro は identity ownership が確立されるまで reservation
や inbox の作業を行ってはならない。単に plaintext の token parameter を
追加するだけでは、secret を model-facing な tool と Rich-log の path に
乗せてしまうことになる。

| Option | What breaks / who is affected | Existing-data effect |
|---|---|---|
| Trusted な launcher/proxy が先に enroll し、その後 session-bound な identity を通じて macro を呼び出す。credential の注入は model から隠す | direct な caller は one-call の利便性の前に bootstrap する必要がある。proxy の outage は fail closed になる | 既存の有効な private token file/binding は継続できる。null または利用不能な token の row には claim/recovery が必要 |
| Macro が non-secret な credential reference を受け付け、trusted な server/proxy boundary 内でのみ resolve する | reference の lifecycle、authorization、remote semantics が必要になる。任意の file path は拒否しなければならない | 既存の store には reference adapter が必要。row 自体は変わらない |
| Macro が two-phase の enrollment を行い、secret sink が acknowledge するまで `enrollment_required`/pending な receipt を返す | 最初の呼び出しで即座の reservation や inbox は得られない。client には retry/resume の logic が必要 | 新しい null row を防ぐ。既存の null row は claim の問題として残る |
| Macro が literal な owner token を受け付け、authenticated な registration に delegate する | 単純だが、macro が model-facing でなく logging が修正されない限り、credential を transcript と instrumentation に露出する | 既知 token の caller は動作する。生成済みで利用不能な token と null row は依然失敗する |
| legacy-unclaimed な row を作り続け、その後で authority を確立する | selected された D7 rollout では却下される。name-only の reservation/inbox/retirement の露出を続け、migration debt を増やす | 既存と新規の null row が混在したままになり、enforcement を開始できない |
| Macro が生成した plaintext token を返す | one-call enrollment を維持するが、記録済みの result/archive の漏洩を再現し、広範な macro result を secret container にしてしまう | 既存の null row には依然 claim が必要。新しく返された secret は logging されていれば rotation が必要 |

どの option を選んでも、既存の name を再利用する macro は metadata refresh の
前に authenticate しなければならず、拒否された authentication は side
effect を持たないようにしなければならない。同じ rule が内部の welcome
message、reservation ownership、inbox の read/ack state にも適用される。
inventory は、直接 `_get_or_create_agent` を呼ぶ他の macro と、D3 で確認
された non-macro の cross-project alias path の両方をカバーしなければ
ならない。`macro_start_session` は新しい null-token row の唯一の source では
ない。

## Zero-lockout, rollback-capable migration order

"Zero lockout" とは、検証済みの replacement または明示的に審査された
legacy window を得るまで、既存の cohort が現在の互換 path を失わない
ことを意味する。疑わしい compromised credential を有効にし続けることを
意味しないし、永続的な name-only access を約束するものでもない。

### Cohorts that must remain distinguishable

| Cohort | Present evidence | Migration risk / existing-data treatment |
|---|---|---|
| caller/private-store に一致する copy を持つ non-null token | 現在の credential が continuity を証明できる | risk が最も低い。DB value を export せずに fingerprint/challenge で verify する |
| origin や caller copy が利用不能な non-null token | server は bearer を持つが、見かけ上の owner は持たないかもしれない | null claim ではなく recovery として扱う。保存された値を露出したり silently に置き換えたりしない |
| 該当する external binding を持たない null-token の macro/legacy row | owner proof がない | 一時的に grandfather するか、administrator-approved な claim/新しい identity を要求する。自動 claim は安全ではない |
| cross-project send によって作成された null-token の target-project alias | D3 は alias とその source metadata を証明するが、owner authority や安全な dedup は証明しない | macro/legacy row とは区別する。migration は origin/project の attribution を保持するか、曖昧な場合は隔離しなければならない |
| migration より前に記録された、該当する binding を持つ null-token row | external な continuity evidence が存在するかもしれない | claim を許す前に binding の provenance と正確な project/name の association を validate する |
| Retired、deregistered、あるいは operationally encumbered な row | credential state に加えて lifecycle/work state がある | claim が activity を回復するのか、retirement を保持するのか、別の lifecycle action を要求するのかを決める |

server は non-null な DB value だけから最初の 2 つの cohort を推測できない。
Client 側の possession は、inventory artifact の中で raw value を比較・
export せずに証明されなければならない。

### Ordered rollout

1. **Semantics と code path を凍結する（live data ではなく）。** credential を
   作成・読取・検証・転送・条件付きで bypass するすべての tool、macro、
   launcher、bridge、dashboard route、内部 call を列挙する。telemetry を
   collect する前に secret redaction を追加する。
2. **additive な state と observability のみを追加する。** versioned な
   enrollment/legacy marker、receipt、non-secret な fingerprint を feature
   flag の裏で導入する。null と non-null の値をまだ reinterpret しない。
   古い reader が新しい field を無視できるようにしておく。
3. **credential holder を先にアップグレードする。** launcher と proxy に、
   選定された private transport、atomic な store update、
   rollback-compatible な dual read を教える。direct client には明示的な
   compatibility diagnostics を提供する。strict な server denial はまだ
   有効にしない。
4. **新しい null-token identity の作成を止め、新しい identity には新しい
   path を使う。** secret delivery が acknowledge されるまで使用可能な
   owner identity を作らない。この前提条件はここで selected されているが
   implement されていない。rollback は新しい enrollment を一時停止しても
   よいが、null-token の作成を再開してはならない。既に enrolled された
   identity は credential を消去されずに動作し続ける。
5. **authorized な claim/recovery campaign を実行する。** name や activity
   ではなく proof のみで分類する。proof を持たない null row は visibly
   legacy-unclaimed のままとし、利用不能な token の row は recovery を使う。
   synthetic な owner token を bulk-write してそれを enrollment と呼んでは
   ならない。
6. **提案される strict な D6/D7 の denial すべてを shadow する。** 最初の
   transport-independent な observer は、principal candidate、tool、
   `would_allow` または `would_deny`、reason を正確に記録し、現在の
   behavior を保持する。credential value は記録しない。追加の cohort や
   client field には後の privacy-reviewed な schema revision が必要である。
   enforcement の前に、公開された tool だけでなく macro/内部 caller も
   measure する。
7. **opt-in した identity/project ごとに strict な behavior を有効にする。**
   明示的な readiness check を要求し、operator の kill switch を保持する。
   Rollback は enforcement mode を変えるのであって credential history を
   変えるのではない。enrolled された credential を null に戻してはならない。
8. **審査済みの coverage threshold の後にのみ既定を変更する。** opt-in して
   いない legacy row は、公開された deadline または administrator action
   付きの明示的な compatibility cohort に残る。D6 の selected な Path-A
   parity を strict な behavior に置き換えるには新しい decision が必要
   であり、D7 には依然として別途の implementation/cutover approval が
   必要である。どちらの変更もここでは authorize されない。
9. **legacy な credential representation を最後に削除する。** one-way な
   verifier の cleanup、古い client の削除、plaintext/token の重複の削除は、
   restore drill、backup review、合意された rollback window の expiration
   の後にのみ行う。

### Rollback requirements

- rollback がサポートされている間、schema の変更は additive のままである。
  古い binary は新しい row/field を許容するか、明示的な version gate で
  阻止されなければならない。
- server の flag は、新しく enrollment された credential や claim の audit
  を削除せずに、grandfathered な cohort のために legacy enforcement を
  復元できる。
- Client の store は、rollback window が閉じるまで以前の version と新しい
  version の credential の両方をサポートする。Rotation の commit は、
  どの version が active かを記録する。
- 失敗した rollout は row を自動的に null に戻さない。それは新しく確立
  された authority を破棄し、D7 を再び開いてしまう。Rollback は
  ownership の事実ではなく policy を変える。
- Rollback は新しい null-token identity の作成を再び有効にすることは
  ない。新しい enrollment path が利用不能な場合、新規作成は一時停止する
  か fail closed になる一方、既存の互換な identity は別途選定された
  behavior を保持する。
- テスト済みの recovery path と backup rollback が存在するまで、migration は
  plaintext value、private client copy、binding、recovery verifier を
  削除しない。
- Shadow と enforcement の log には fingerprint/reason のみを含み、基盤の
  identity state を失うことなく無効化できる。

## Option impact summary

product decision が明示的に受け入れなければならない、cross-cutting な
breakage は次の通りである。

| Selected capability | Principal breakage | Existing data that needs handling |
|---|---|---|
| 任意の strict な owner authentication | compatibility window が終わると、tokenless な caller と利用不能な token を持つ caller は mutate/send を停止する | null row、生成済みだが利用不能な token、内部の macro identity |
| secret を model tool の外に置く任意の transport | direct な one-call client には launcher/proxy/bootstrap のサポートが必要 | 既存の private token file と bridge binding には provenance/permission の validation が必要 |
| 任意の one-way な server verifier | cleanup 後、plaintext の retrieval と正確な古い binary の write は動作しなくなる | plaintext の indexed token column、backup、fixture、dual-read の期間 |
| 任意の administrator-backed な claim/recovery | 誰か、あるいは何かが agent row よりも上位の authority になる | project-admin の enrollment、audit、revocation、共有 machine と headless の policy |
| 任意の non-exportable な binding | identity の mobility と disaster recovery が bridge に依存する | binding の provenance、private channel の availability、process/machine 間の transfer |
| claim の代わりの任意の continuity transfer | stable-name の consumer と lifecycle semantics が alias/transfer を理解する必要がある | inbox、thread、reservation、audit、retirement、historical な attribution |

この表は checklist であって ranking ではない。

## Non-binding lean for evaluation

この lean は prototype と test に焦点を当てるためだけに存在する。
enrollment や authority の mechanism を選定するものでも、D6 や D1 の
selected かつ implemented な scope を変更するものでも、ledger-selected な
D7 boundary を弱めるものでも、D6/D7 の enforcement を authorize するもの
でもない。

- trusted な launcher/proxy が保持する caller-generated な high-entropy
  credential、または authenticated な one-time sink に配送される
  server-generated な credential を優先する。model-facing な tool には
  receipt/fingerprint のみを返す。
- dual-read な rollback 期間の後に one-way で versioned な server
  verification を優先する。保存された bearer を開示することを前提に
  通常の recovery を design しない。
- null row については、pre-existing な private binding challenge が
  authority-establishment request より本当に以前から存在する場合はそれを
  評価し、そうでなければ明示的な project-admin/local approval を評価する。
  どちらもなければ、新しい enrolled identity を作成し、first-come の
  name claim ではなく audited な continuity transfer を使う。これらは
  依然として mechanism の候補であって、選定ではない。
- 通常の rotation には current-credential-authenticated な atomic
  rotation を使う。loss には pre-issued な recovery material、または
  後で選定される administrator/binding authority を使う。ambient な
  identity の事実を再利用しない。
- `macro_start_session` の前に trusted な bootstrap が enroll/bind し、
  reservation と inbox の operation は authenticated な boundary の
  内側に置く。
- client の readiness、opt-in の enrollment、claim/recovery、shadow
  denial、per-identity の strict mode を経て additive に migrate し、
  その後にのみ別途承認された既定の変更を行う。

この lean は、project-admin authority、remote な secret sink、verifier の
key custody、continuity-transfer の semantics の定義に依存したままである。

## Post-decision tests

該当する mechanism と implementation が承認された後、これらの test は、
entry の `implementation_state` を変更できるようになる前の、committed で
hermetic な gate になる。D1 の既存の selected-requirement test は、
open な選択ではなく前提条件のままである。

### Enrollment and non-disclosure

- 選定された caller-generated、sink、bound な transport で新しい identity を
  作り、canonical な read-back、credential version、receipt、正確に 1 つの
  active な verifier を assert する。
- DB commit の前、DB commit の後だが sink acknowledgement の前、sink
  acknowledgement の後だが response の前で crash させる。retry は
  deterministic でなければならず、漏洩したり未知の追加の owner
  credential を発行したりしてはならない。
- wrong な sink、expired な handle、replayed な handle、再利用された
  receipt、concurrent な enrollment。すべての拒否された path は durable/
  profile/Git の side effect がゼロである。
- request capture、すべての result format、Rich log、exception、
  telemetry、JSONL、profile、message、archive/Git、dashboard、
  notification、process listing、environment dump、shell history、
  temporary file、client state、diagnostics を canary-scan する。
  意図的に選ばれた verifier/secret store でのみ出現を許す。
- サポートされるすべての secret sink について、permission、symlink、
  non-regular-file、partial-write、fsync、atomic replace、cleanup、
  backup/export の test。

### Claim authority and races

- 受け入れられたすべての proof authority について、正しい
  project/name/row の binding、誤った identity、誤った project、
  expiry、cancellation、replay、forged/newly created な binding、
  revoked な administrator、利用不能な verifier を test する。
- name のみ、project key のみ、`AGENT_NAME` のみ、tmux/window ID のみ、
  task metadata のみ、historical な message のみ、filesystem path のみ、
  inbox knowledge のみで claim を試みる。選定された policy が、事実
  そのものではなく別途 authenticate された control plane を明示的に
  authority にしていない限り、それぞれ失敗しなければならない。
- 2 人の異なる claimant と duplicate な retry を concurrently に実行する。
  ちょうど 1 回の遷移とちょうど 1 つの credential version だけが勝ち、
  負けた側の metadata、archive、reservation、inbox の mutation は
  発生しない。
- active、retired、deregistered、unread-work、active-reservation の
  row をカバーし、selected された lifecycle behavior を ownership とは
  別に assert する。

### Rotation and recovery

- atomic な rotation の前、最中、後で、正しい・欠落した・誤った・古い・
  新しい・grace-window の credential を test する。
- すべての verifier/sink boundary で crash と retry を行う。意図しない
  dual authority を残さずに、少なくとも 1 つの意図された owner path が
  使用可能なままであることを assert する。
- recovery code の single use、replenishment、revocation、theft/replay、
  concurrent な compromise recovery。
- bridge/admin/recovery path を失った場合、そして最終的な新規 identity へ
  の continuity transfer（historical な mail と reservation の
  attribution を含む）。

### D1, D6, D7, and macros

- D1: row、timestamp、profile bytes、archive HEAD/log、Git status を
  snapshot する。明示的に競合する token は拒否された call の mutation が
  ゼロでなければならず、同じ token による update は 1 回だけ commit
  される。omitted-token の compatibility を D1 authority として扱わずに
  固定し、異なる explicit token を null-token row に対して race させ、
  1 人の DB writer と 1 回の profile commit だけが勝つようにする。
- D6: selected された omitted-token の parity case を保持しつつ、
  caller-known token、生成済みで利用不能な token、null legacy、
  enrolled、pending、grandfathered の各 cohort について、将来の
  strict な behavior を別途評価する。正しい/誤った/欠落した sender
  authority を test し、`verified_sender`、DB recipients、archive、
  inbox、拒否された call の mutation がゼロであることを assert する。
- D7: active-null と既に retired 済みの null legacy cohort を分ける。
  name-only の re-retire が既に retired 済みの null-token cohort に
  対してのみ成功し、`retired_at` を保持し、DB/profile/archive/signal の
  state を変更しないことを証明する。active な null-token row が
  auto-retire されないこと、unretire・hard delete・transfer・
  project-wide な owner operation が将来の principal/admin authority を
  要求することを証明する。また、enforcement を有効にする前に、すべての
  new-identity path が null-token row の作成を停止していることを証明
  する。
- `macro_start_session`: fresh な enrolled identity、既存の valid な
  identity、既存の競合する identity、null legacy row、利用不能な
  生成済み token、pending な sink、crash 後の retry。選定された
  authenticated/enrolled な state に達する前に、reservation や inbox
  access が発生してはならない。
- identity を作成する、あるいは identity として振る舞う他の内部 macro/
  welcome path を inventory し test する。strict な enforcement が
  隠れた caller を strand しないようにするためである。

### Migration and rollback

- 現在と以前の client version を、legacy、dual-read、shadow、opt-in
  strict、default-strict の server mode に対して test する。
- aggregate count のみを用いた cohort ごとの migration。inventory や
  shadow telemetry から raw token が一切出力されないことを verify する。
- enrollment、claim、send、rotation、macro start、service restart の
  最中に kill switch を flip する。新しく確立された ownership は
  維持され、明示的に保持された legacy な compatibility behavior は
  戻ってくる。新しい null-token の作成は無効のままでなければならない。
- 宣言されたすべての rollback milestone で古い binary と backup を
  restore する。古い binary が additive な state を安全に読めない場合、
  または stop-null の milestone の後で新しい null-token identity を
  作成できてしまう場合は、明示的な version gate で fail closed にする。
- fresh-install の coverage は引き続き必須である。clean な installation
  は、developer machine の state に依存せずに、enroll・restart・
  rotate・send・retire・recover できなければならない。

## Unselected matters requiring a product decision

- canonical な project-administrator authority とは何か、それはどう
  enrollment・revoke され、共有 machine と headless CI ではどう機能
  するか。
- どの pre-existing な bridge/window/session の binding が continuity を
  証明するのに十分強く、「claim より前から存在する」ことをどう
  tamper-evident にするか。
- local な path や file descriptor が意味を持たない remote MCP client に
  対して、どの secure sink が機能するか。
- server の verifier は plaintext か、keyed one-way か、externally
  managed か。誰が verifier key を保持するか。dual-read の rollback は
  どのくらいの期間保持されるか。
- 現在 tokenize されている row を、その token を export せずにどう分類
  するか。特に一度も返されたことのない server-generated な値について。
- recovery material の custody、expiry、replenishment の rule は何か。
- continuity transfer は canonical な name を保持するのか、alias を
  作るのか、inbox/reservation を移動するのか、それとも audit history を
  link するだけなのか。
- prospective な 24-tool permission inventory を selected な policy に
  変えるには、どの正確な principal/admin credential、delegation、audit
  mechanism が必要か。
- 各 authority-establishment mechanism は、active な reservation、
  未読の mail、signal、concurrent な metadata refresh を、identity を
  暗黙に unretire したり transfer したりせずにどう扱うべきか。
- fingerprint、OS identity、approver metadata を新しい privacy/security
  の liability に変えることなく、どの程度の audit retention が十分か。
- D6 の parity を strict な behavior に置き換える、あるいは D7 の
  implementation/cutover を変更する前に、どの readiness threshold と
  compatibility deadline が必要か。
