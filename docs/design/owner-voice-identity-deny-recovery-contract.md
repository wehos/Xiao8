# Owner 声纹拒绝后的 ASR 恢复合同

> **状态：Current contract。** 本文记录独立 ASR 在正式声纹拒绝并完成安全清理后的恢复边界。代码与测试具有最终权威；本文不改变声纹录入、匹配阈值、ASR 路由或 Provider 策略。

可信 Provider exact boundary 下的子回合局部 DROP/FORWARD 由 [Owner 声纹 exact 子回合局部裁决合同](./owner-voice-identity-exact-interval-contract) 约束。局部裁决没有启动 transport-wide cleanup 时，不进入本文的拒绝恢复状态机。

## 1. 目标与非目标

正式声纹拒绝必须继续 fail-closed：被拒绝语音、迟到 transcript 和旧 Provider 回调都不得重新进入 Core。安全清理成功后，运行时使用拒绝后的连续 Silero 静音重新建立边界，使 Owner 的下一次真实起声可以直接恢复 ASR，不需要先说一句用于“唤醒”链路的牺牲语音。

本文明确不处理：

- 声纹误拒绝率、CAM++ embedding 或相似度阈值；
- Profile 录制、保存和热加载；
- 保存 Profile 后强制重启 ASR；
- ASR 路由、Provider 选择、重试、backoff、超时或麦克风关闭时机；
- `QUARANTINED` 的自动恢复。

## 2. 状态机

恢复沿用现有内部状态，不增加用户可见状态：

```text
OPEN
  → DENY_FENCED
  → RETIRING
  → WAIT_SILENCE
  → ARMED
  → OPEN
```

- `DENY_FENCED`：停止被拒绝候选继续进入 Provider/Core。
- `RETIRING`：等待旧 session、dispatcher、callback、transcript reservation、namespace、boundary proof 和 speaker lease 完成退休。
- `WAIT_SILENCE`：只接受当前 cleanup 身份下的连续 Silero 静音证明。
- `ARMED`：边界已经成立，等待下一次 `SPEECH_STARTED` 或 `SPEECH_RESUMED`。
- `OPEN`：起声所有权仍有效，当前起声帧继续进入原有 Provider 提交路径。

清理链任一安全证明失败时仍进入 `QUARANTINED`。该状态不能通过静音自动恢复，只能使用既有显式麦克风重启路径。

## 3. 拒绝后静音证明

`WAIT_SILENCE` 只能使用拒绝完成后的本地 Silero 事实：

- Silero 可用且推理未降级；
- cleanup generation、session epoch、Detector 对象及 detector epoch 均未变化；
- lifecycle 对象和 ingress token 仍属于当前链路；
- PCM sequence 连续递增，`captured_at` 严格递增；
- 连续静音达到当前 `candidate_silence_ms`，默认 300 ms；
- 中间没有 speech、ambiguous window 或序列缺口。

Detector 在显式 `prepare_deny_rearm()` 后重置 Silero 的递归流状态和窗口计数。新 Detector 因而可以从初始静音产生既有 `CANDIDATE_PAUSE`。如果准备后立即出现语音，这段语音不会构成放行证明；必须等它结束并重新形成完整静音。

用于证明的 PCM 不发送给 Provider，不进入 SmartTurn coordinator，不创建 Speaker Shadow 候选，也不触发 Provider prewarm。`CANDIDATE_PAUSE` 成立后，Detector 立即消费本次 prepare token，运行时才可把状态推进到 `ARMED`。

## 4. 有序准备与并发所有权

Detector prepare token 由 `(cleanup_generation, cutoff_sequence, detector_epoch)` 组成：

- 相同 token 重复准备是幂等操作；
- 新 cleanup、新 cutoff 或新 detector 必须重新准备；
- direct detector 在 detector lock 内准备；
- semantic adapter 通过 FIFO control item 排在已经接纳的音频之后；
- closed、epoch 不匹配、Silero unavailable 或 gate 异常返回失败；
- caller 取消不会留下 gate 与 token 不一致的半准备状态。

Runtime 在 prepare 和 feed 的每个 `await` 后重新核对 cleanup、session、Detector/epoch、lifecycle、ingress、cutoff、状态以及 sequence/time 所有权。旧任务只允许返回，不能回滚或打开新状态。

若 sequence 不连续、`captured_at` 不递增、ingress identity 改变或 detector epoch 改变，当前帧不参与静音证明，状态保持或回到 `WAIT_SILENCE`，并从更新后的 cutoff 重新准备。旧 socket 的相同或更旧 route/lease 身份在更新全局 sequence 基线前返回 `STALE`，因此高 sequence 迟到帧不能污染新链路。

## 5. ARMED 放行

`ARMED` 只等待 `SPEECH_STARTED` 或 `SPEECH_RESUMED`。起声返回后，当前操作在无 `await` 的线性化点执行 `ARMED → OPEN`，再调用既有 activity 处理；同一个起声帧继续进入正常 Provider 音频路径。

若 feed 期间发生新 cleanup、session epoch 变化、Detector 替换、lifecycle 替换或 ingress 失效，迟到结果不能推进状态，也不能把当前帧发送给 Provider。Detector 在 `ARMED` 中被替换时，新 Detector 仍须产生真实 onset，不能因为重建本身直接进入 `OPEN`。

## 6. Transcript DROP 的单一所有者

正式拒绝进入 cleanup 后，`_SpeakerDenyCleanupOperation` 是 transcript DROP 的唯一写入者。已经发布的 Provider callback 只完成以下移交：

- 校验 cleanup generation、speaker lease、session epoch、Runtime identity 与 dispatcher identity；
- 将 `FinalKey → TranscriptDispatcher` 幂等登记到 `provisional_reservations`；
- 不调用 `resolve_reserved(DROP)`，不删除 dispatcher 映射，不退休 Provider turn ownership，也不等待 cleanup settlement。

cleanup 在 transport、session 和旧 callback 按既有顺序停止后封闭 reservation 成员集合，再统一写入 DROP。安全确认只接受：

- `APPLIED`；
- `ALREADY_SAME`，且既存 disposition 明确为 `DROP`。

`NOT_RESERVED`、既存 `FORWARD`、不同 dispatcher、ownership 漂移或 Runtime 被替换均不是安全成功。DROP tombstone 被确认后，cleanup 才能删除映射、退休 ownership，并继续完成 namespace、boundary proof、speaker evidence lease 与 lifecycle 清理。任何一步无法证明时保持 fail-closed，并进入 `QUARANTINED`。

该单写者合同同时适用于迟到 callback、callback 取消以及 cleanup settlement 前后的交错，避免 callback 与 cleanup 对同一 reservation 双写后把已经安全删除的内容误判成清理失败。

## 7. 保持不变的安全合同

- `_finish_speaker_deny_cleanup()` 继续按 transport、session、callback、transcript、namespace/proof、speaker lease 与 lifecycle 的既有安全顺序收口；
- 被拒绝 partial/final、旧 transcript reservation 和旧 callback 不得复活；
- Provider close 或其他清理证明失败继续进入 `QUARANTINED`；
- 不重放拒绝期间丢弃的 PCM；
- 不改变声纹 enforce/fail-open 判定、`unsupported_asr_route` 或用户提示；
- 不新增日志、数据库、配置、指标或原始音频持久化。

## 8. 自动化与实际验收

自动化测试至少覆盖：

1. 正式拒绝、清理、session/Detector 重建、连续静音、`ARMED`、Owner onset、`OPEN` 和同帧 Provider 提交的完整链路；
2. 不足 300 ms、speech、ambiguous window 和 sequence gap 不能建立证明；
3. prepare 幂等、generation/epoch 失效、semantic FIFO 和取消安全；
4. 旧 socket 高 sequence、迟到 prepare/feed 和新 cleanup 接管不能打开链路；
5. Provider close 失败继续 `QUARANTINED`，拒绝内容永不进入 transcript/Core；
6. callback 只移交 reservation，cleanup 是唯一 DROP 写入者；`NOT_RESERVED`、冲突 `FORWARD` 和 dispatcher 替换均不能伪装成安全成功；
7. 普通非 rearm Detector 的事件与 throttle 行为保持不变。

自动化不能替代真实麦克风验收。实际流程必须用非 Owner 触发一次正式拒绝，确认被拒绝内容未进入 Core，安静至少 500 ms，再由 Owner 只说一句正常长句；该句必须立即进入 ASR。还需在 `WAIT_SILENCE` 中制造一次 session/Detector 重建，并确认角色切换或 socket 重连后的旧帧不能打开新链路。
