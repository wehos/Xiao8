# Owner 声纹 exact 子回合局部裁决合同

> **状态：Current contract。** 本文记录独立 ASR 在 Provider 给出可信 exact boundary 时，对单个 Provider 子回合执行局部声纹 DROP/FORWARD 的当前行为。代码与测试具有最终权威。

声纹录入与 Profile 提交由 [Owner 声纹录入与独立验证合同](./owner-voice-identity-enrollment-contract) 约束；transport-wide 正式拒绝完成后的恢复由 [Owner 声纹拒绝后的 ASR 恢复合同](./owner-voice-identity-deny-recovery-contract) 约束。

## 1. 目标与语义边界

当两段顺序语音已经被 Provider 识别为两个独立、精确且互不重叠的音频区间时，运行时可以让它们分别接受声纹裁决：

```text
exact A：正式拒绝 → 只 DROP A 的 partial/final
exact B：验证通过 → 只 FORWARD B 的 transcript
```

“局部裁决”只控制 transcript 是否进入 Core。音频在边界到达前已经发送给 ASR Provider，本合同不撤回 Provider 已接收的音频，也不承诺 Provider 不处理该音频。

本文明确不提供：

- 基于标点、字符串、final 文本或词级时间戳的切割；
- 同一个 Provider item 内部的文本二次切割；
- 重叠说话的声源分离；
- 对 unknown、gap、overlap 或歧义边界的局部授权；
- 声纹阈值、模型、1.5/3 秒检查点长度或录入流程的改变。检查点的时间原点必须是 Provider 给出的 canonical speech start，不能是 transport PCM 起点。

### Provider 能力与 canonical anchor

Provider exact 声纹能力通过中立 capability 声明。只有能够提供 canonical 16 kHz speech start 和 exact end 的 Provider 才能启用该路径；不支持的 Provider 在声纹 enforce 模式下返回 `unsupported_asr_route`，不得显示声纹保护已就绪。首期只有 Qwen 声明 `CANONICAL_16K_EXACT_INTERVAL`，其他 Provider 默认 `UNSUPPORTED`。

wire PCM 字节数只属于 transport 统计，不能作为声纹 ownership 坐标。Runtime、Detector 与 Speaker Shadow 统一使用 session-local canonical 16 kHz sample cursor；因此 16 kHz 与 24 kHz wire Provider 的同一区间必须得到相同 canonical start/end。

exact-capable Provider 的首批 PCM 先进入有界 `UNANCHORED_DEFERRED` candidate：

- started 到达前只缓冲，不创建 Admission parent，不启动 Speaker Shadow checkpoint；
- started 的 canonical start 被验证后，原子丢弃 start 前 PCM，仅以连续后缀启动 1.5/3 秒评分；
- future start 保持 pending，重复相同 start 幂等；缺失、冲突、已淘汰、gap、overflow 或身份漂移都把该句声纹标记为 `UNAVAILABLE`；
- speaker anchor unavailable 不等于 Provider identity failure，partial/final 仍属于已绑定的文本 key。

状态顺序固定为：

```text
UNANCHORED_DEFERRED
  → ANCHORED_SCORING
  → EXACT_PREPARING
  → EXACT_DRAINING
  → FORMAL_ALLOW / FORMAL_DENY / UNAVAILABLE
```

前四个状态不存在正式 LOW/DENY，也不允许 transport-wide speaker cleanup。

## 2. 父 Speaker Lease 的证据合同

exact-capable Provider 只有在 canonical anchor 成功或明确 unavailable 后才创建 Admission parent/child。exact promotion 前，父 lease 始终保持 `COLLECTING` 且 `last_sequence_no == 0`；Speaker LOW/HIGH/close 只写入 Runtime 的有界 provisional ledger，不进入 reducer。ledger 必须绑定 runtime/session/transport、detector epoch、timeline/evidence generation、candidate、Provider key、anchor revision和 PCM/Speaker sequence fence。

ledger 中重复相同事实幂等；重复冲突、gap、乱序、容量溢出或任一 identity/revision 变化都会 poison 该 ledger。poison 后不得沿用旧 LOW/HIGH，最终只能提交 `SpeakerUnavailable`。

父 `SpeakerCaptureLease` 使用以下内部状态：

```text
COLLECTING + HIGH
  → HIGH_SEEN

HIGH_SEEN + CaptureClosed
  → ALLOW

FIRST_LOW + CaptureClosed
COLLECTING + CaptureClosed
SpeakerUnavailable
  → UNAVAILABLE → FORWARD

FIRST_LOW + SECOND/COMPLETION LOW
  → DENY_LATCHED

FIRST_LOW + HIGH
HIGH_SEEN + LOW
  → MIXED_DENY_LATCHED
```

上述 reducer 只在 exact promotion 成功、ledger 进入 `EXACT_DRAINING` 后消费事实。`HIGH_SEEN` 不是立即终态，因此一个子回合的 HIGH 不能担保后续子回合。`DENY_LATCHED` 与 `MIXED_DENY_LATCHED` 都永久粘滞，后续 HIGH 不能复活。单次 LOW 后 capture close、声纹后端 unavailable 与普通 Owner HIGH 的既有 fail-open/allow 策略保持不变。

可能产生 DROP 的父 lease 转换使用两阶段所有权：

1. Admission 在同一 FIFO 和锁内 prepare transition，返回携带 logical revision 的 terminal claim；
2. Runtime 在无 `await` 的线性化点设置 `DENY_FENCED`；
3. Admission 以 CAS 校验 revision 后 commit，并由 coordinator 单写 terminal fan-out。

claim 只能消费一次。prepare 后父状态、generation 或 owner 发生变化时，commit 必须返回 stale/conflict，旧 claim 不得关闭新 session。

## 3. exact 资格证明

### 安装身份的前置边界

声纹事实除下列 utterance / PCM 身份外，还必须属于当前安装实例及已提交、未撤销的激活权限。资料 generation 相同不代表安装相同；路由重启、重新启用和回滚必须隔离旧实例回调。安装退休或权限撤销只使尚无正式 DENY 的句子沿既有 `UNAVAILABLE` fail-open 收口，不能恢复已拒绝文本，也不绕过 exact 前的 provisional 隔离。实例生命周期、健康和 READY 由 [安装生命周期合同](./owner-voice-identity-installation-lifecycle-contract) 约束；本节不修改后续 exact promotion、reducer 或 DROP/FORWARD 语义。

### 区间证明

局部能力只有在下列事实同时成立时才能启动：

- Provider boundary 含合法 start/end，且属于当前 Provider timeline generation 与 key；
- Detector、detector epoch、session epoch、Runtime identity 和 ingress token 当前有效；
- PCM sequence 连续，区间无 gap、overlap 或未知 ownership；
- Speaker Shadow buffer、evidence lease 与 candidate generation 一致；
- 目标 Provider child 是父 lease 唯一且最后一个 child；
- 尚无 successor child 开始或绑定；
- Admission child 尚未 terminal，partial/final 仍受 Admission 控制；
- active exact transaction 与 pending transaction 的总量未超过既有容量上限。

如果 exact target 与父 candidate 不同，父 lease 必须仍为无历史证据的 `COLLECTING`。provisional LOW/HIGH 只能随经过 anchor、sequence 和 coverage 校验的 ledger 投影到 exact target，禁止把父 reducer 中既有证据迁移到另一个 candidate。

相同 key、相同 boundary 的重复通知幂等合并。相同 key 的不同 boundary 是安全冲突，不得把先到的 exact proof 降级成 unknown 后继续局部放行。

## 4. 可撤销的跨层事务

exact 建立严格按以下顺序执行：

```text
Runtime 校验 canonical anchor/key/identity
  → Detector prepare
  → Admission promote tail child
  → Admission activate exact hold
  → Detector commit
  → Runtime 无 await 发布全部 alias 并进入 EXACT_DRAINING
  → 单一 FIFO owner 按 sequence 排空 provisional facts/close/final
  → 记录 Provider exact proof
```

### Detector prepare

Detector 在自身有序锁/队列内验证 anchor revision、PCM sequence、连续 coverage 与 evidence ownership，预留 target candidate 和可选 suffix candidate，冻结相关 Speaker Shadow buffer，并返回不可伪造、可撤销的 reservation。prepare 不发布 completion callback、不消费 finalized continuation PCM，也不改变 Admission ownership。

prepare 期间继续到达的 PCM 只能进入有界 provisional suffix scratch。commit 后 suffix 承接这些 PCM；abort 必须按原顺序恢复到仍合法的 continuation。worker 不可用、队列满、身份变化或恢复失败时 abort 返回失败并撤销 evidence authority，上层显式提交 `UNAVAILABLE`，不能擦除 PCM 后声称安全回滚。

### Admission promote 与 activate

coordinator 在同一把锁内校验父 lease token/revision、child generation/revision、Provider key、boundary proof、唯一尾部 child 和 candidate ownership。promotion 原子地：

- 从父 `child_bindings` 移除 target child；
- 将 child 放入 exact HOLD；
- 把 suffix 安装为父 lease 的新滚动 candidate；
- 返回一次性 typed receipt。

activation 只把已迁移的 evidence 投影到 exact child，仍不触发 transcript resolution。父 terminal fan-out 若先完成，promotion 必须 stale；promotion 若先完成，父 fan-out 不得再包含该 exact child。

### Detector commit 与 Runtime alias

Detector commit 后，target/suffix 的 Speaker Shadow ownership 与 PCM 分割生效。Runtime 在下一次 `await` 前一次性发布 provider key、target candidate、turn、parent lease、suffix evidence 和 proof 的全部 alias，避免紧随其后的 completion callback 观察到半切换状态。

suffix 不绑定到旧 target turn。后续 Provider started 必须为它建立新的 child/turn identity。

## 5. pending FIFO、取消与接管

exact prepare/promote/activate 期间到达的 Speaker facts、capture close、ordered endpoint 和 final 放入该 transaction 的有界 FIFO，并由唯一 drain owner 按 sequence 回放。禁止为每个事件独立 `create_task`。final 先于 ordered 到达时使用已经建立的 exact boundary/proof 封口，不制造 unknown boundary；gap、乱序、冲突或 FIFO overflow 只能 poison 并转 `UNAVAILABLE`。

所有跨层 `await` 后重新校验 Runtime、session、transport、Detector/epoch、ingress、Provider key、parent/child revision、dispatcher 与 receipt ownership。取消遵循以下规则：

- Detector commit 前：shield 已接纳的 Admission 命令，取得确定结果后执行对偶 abort；
- Detector commit 后：不能退回未定义状态；尚无正式 DENY 时必须以 `SpeakerUnavailable` 推进既有 fail-open 终态，只有 sticky DENY 已形成但局部 DROP 无法安全落地时才升级父组 cleanup；
- FIFO replay 被取消时仍须有界排空到安全终态，再向调用者重新抛出取消；
- 旧任务只能退出，不能回滚新状态或修改 successor。

只要 pending 或 active exact transaction 仍在 Runtime map 中，Provider namespace reset 就拒绝 reconnect takeover。即使 disposition 已经算出，只要 partial、tombstone、lifecycle 或 correlator settlement 尚未结束，也不能接纳新 session。transaction 完全退休后才允许重连。

## 6. 局部终态与 transcript 安全

exact child 复用父 lease 的 evidence reducer，但使用 `EXACT_INTERVAL` 内部作用域：

- exact ALLOW/UNAVAILABLE 只 FORWARD 对应 child；
- exact `DENY_LATCHED` 或 `MIXED_DENY_LATCHED` 只 DROP 对应 child；
- effects 只包含对应 child 的 `SettlePartial` 与 `ResolveReserved`；
- 不产生 `AbortProviderTransport`，不改变 Provider session generation；
- `AdmissionDisposition` 仍只有 `FORWARD`、`DROP`、`ABANDON`，不增加公开枚举值。

DROP 只有在 transcript dispatcher 返回 `APPLIED`，或 `ALREADY_SAME` 且既存 disposition 明确为 `DROP` 时才安全。`NOT_RESERVED`、既存 `FORWARD`、dispatcher 替换或 ownership 漂移必须升级父组 cleanup；不得把“请求过 DROP”误写成“DROP 已生效”。

exact terminal settlement 完成后才退休 Runtime alias、Provider proof、candidate binding 与 ownership。被 DROP 的 partial/final 永不进入 Core，也不能由迟到 callback 复活；successor 的首帧、partial 和 final 继续使用正常链路。

## 7. 正式拒绝与显式降级

exact-capable Provider 的正式拒绝只能发生在 canonical exact authority 建立之后：

- endpoint 前的 LOW/HIGH/close 全部是 provisional fact，不能触发 Admission DENY 或 transport-wide cleanup；
- exact promotion 后，按 sequence 排空得到的第二次 LOW、completion-confirmation LOW 或 mixed evidence 才能形成 sticky `DENY_LATCHED` / `MIXED_DENY_LATCHED`；
- 正式 DENY 一旦产生永久粘滞，后续 HIGH、迟到 proof 或 unavailable 不能撤销；
- start/exact/coverage/identity/sequence 缺失，successor ownership 歧义，receipt stale，rollback 状态不确定或 effect 失败时，该句显式进入 `UNAVAILABLE`；
- `UNAVAILABLE` 是 fail-open 声纹终态：已隔离 partial 必须有序 settle 为 FORWARD，final 继续正常交付且 exactly-once；不得把 speaker proof 缺失解释成 Provider key 失败；
- 只有 Provider namespace、key 或 turn ownership 的真实冲突才返回 `FAILED_IDENTITY` 并阻断该文本 identity。

因此典型可局部裁决的序列是：

```text
A provisional FIRST LOW
  → Provider exact endpoint A
  → promotion/drain 后 completion-confirmation LOW
  → 只 DROP A
  → B 使用 suffix/new child 收集证据
  → B HIGH + capture close
  → 只 FORWARD B
```

即使 A 在 endpoint 前已经得到两次 LOW，也必须等待 exact promotion 后再形成正式 DENY。这样避免用前导静音或上一句 suffix 的评分永久拒绝真实用户，同时保持阈值和 sticky DENY 语义不变。

## 8. 容量、资源与验证合同

- parent child、exact proof、Admission ingress、transaction FIFO、pre-anchor rolling buffer、continuation 与 Speaker Shadow PCM 都必须有显式有界容量；
- 容量耗尽不淘汰 HOLD child、不抛出未处理异常、不沿用旧证据；exact 尝试撤销，无法证明恢复时显式提交 `UNAVAILABLE` 并释放 transcript；
- prepare、commit、abort、reset 和 close 后，临时 target/suffix PCM、receipt 与 candidate binding 必须对偶释放；
- Detector identity reset 同步清空 exact reservation registry，旧 receipt 在 reset/close 返回前失效；
- 不新增数据库、配置、原始音频日志、字符串切割规则或 Provider 专用策略分支。

诊断只记录 transaction/key/revision/sample range 与有界 reason code，不记录原始音频或文本。至少区分 anchor missing/conflict/evicted、ledger gap/reorder/overflow、exact prepare/commit/abort、unsupported route 与 per-turn unavailable。

自动化至少覆盖：非零前导 LOW + Owner 后缀 FORWARD、非零前导 HIGH + non-owner 后缀 DROP、局部 A DROP/B FORWARD、single LOW fail-open、mixed evidence 粘滞拒绝、future/重复/冲突 start、16 kHz canonical 与 24 kHz wire、12 秒 finalized coverage、back-to-back suffix、final-first、pending FIFO、重复/冲突 boundary、容量、gap/overlap、旧 socket、取消、rollback、reset/reconnect takeover、dispatcher tombstone 冲突，以及 local/smart 路径不变。

真实验收必须同时验证：非 Owner 独立短句不进入 Core、随后 Owner 独立句进入 Core、local DROP 前后 Provider session generation 不变；没有足够静音或两人重叠时不得声称完成局部裁剪。
