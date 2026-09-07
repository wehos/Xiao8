# Owner 声纹安装生命周期合同

> 状态：本工作包的实施与审查合同；不是全量测试、真实语料或 Electron 实机验收证明。修改前基线为 `faf4ded97`。实现和验收差距必须单独报告，不得把本文的要求当作已通过的结果。

本合同只约束期望配置、manager 实际安装、证据权限、健康事件和 READY 的一致性。模型、评分算法、Profile schema、录入质量门、音频处理、ASR Provider 选择策略和 local/SmartTurn 断句均不在范围内。

## 1. 不变量

- 阈值保持 `0.40`；运行时检查点保持 `1.5 / 3.0` 秒。
- Provider exact 前 LOW/HIGH 仍为 provisional；本次安装事务不提前释放隔离文本。
- 正式 DENY 永不撤销；退休权限只使尚无正式拒绝的证据进入既有 `UNAVAILABLE` fail-open 路径。
- 失败或过期安装不能用错误 Profile 为新句子提供证据。
- 禁用、删除后不再过滤；切路由不得复活已撤销的 Profile。
- 安装生命周期竞态的修复不等于已经证明最初丢字的全部原因。

## 2. 四个权威与五种身份

| 状态 | 权威 | 含义 |
| --- | --- | --- |
| 期望配置 | Service / Registry | 用户是否启用、期望 Profile |
| 当前目标 | 每个 manager 的 Core 协调器 | 当前应安装什么、是否等待路由 |
| 实际安装 | Runtime / Detector | 哪个实例已挂载、是否仍有权限 |
| 实例健康 | 具体安装操作 / 实例 | 当前完整未恢复故障集合 |

中立合同位于 `main_logic/asr_client/speaker_verifier_contracts.py`，不向上导入 Service 或 app。

`profile_generation` 标识资料，`activation_revision` 标识配置操作，`installation_id` 标识 manager 上的具体安装；runtime/session/route generation 标识语音生命周期，`health_revision` 标识实例内事件顺序。重新启用、回滚至相同 Profile 都必须生成新操作和安装身份。

Detector 的候选 epoch 不是安装寿命：安装进行中跨 epoch 必须 STALE；提交后正常句间 reset 保留同一 Shadow，实际挂载以 Detector 身份、捕获的 Shadow 对象和安装 owner generation 校验。正常 reset 不要求重建模型；直接替换或关闭 Detector/Shadow 后，不得继续报告 READY。

`SpeakerVerifierSpec` 保存期望资料、激活 revision、启用/enforce、共享可撤销 authority 和惰性 factory builder。`SpeakerVerifierInstallReceipt` 携带完整安装身份、明确 outcome、ownership、health revision 和 cleanup pending。新的安装回执禁止隐式 bool；公开兼容枚举的 truthiness 不是安装成功证明。

| 安装结果 | 合同 |
| --- | --- |
| `INSTALLED` | 仅证明回执对应实例安装完成；仍须校验当前身份、权限及健康才能 READY |
| `DEFERRED_ROUTE` | 无可安装活动语音路由，保留期望；不能称已安装 |
| `UNSUPPORTED_ROUTE` | 当前路由不支持，保留期望和恢复义务 |
| `STALE` | 操作被接管，不得清理新实例 |
| `FAILED` | 明确失败，不代表已恢复旧配置 |
| `REVOKED` | 权限已撤销；物理资源是否关闭由 ownership / cleanup pending 表示 |

## 3. 每个 manager 的单一协调入口

注册、开始语音、stop/start、路由 reconcile、替换/重新启用 Profile、回滚和重试均汇入 Core 的同一安装协调入口。同一 manager 最多一个安装操作；后续请求只保留最新目标，重复触发合并。

空闲注册只绑定期望并返回 `DEFERRED_ROUTE`，不分配 Shadow/模型，不为等待开麦持续轮询。新 manager 对外发布前完成配置绑定。语音路由启动后本地触发 reconcile；Runtime 不访问 app Registry。

每次启动重新验证有效路由 capability：Provider authority 需要 canonical exact capability，local/SmartTurn 保持原支持规则，native 不属于独立 ASR 保护范围。不支持时不能静默切 Provider，也不能沿用旧 factory 假装 READY。路由/session 变化先同步退休旧安装权限，再异步释放旧资源。

## 4. 安装、资源移交与取消

顺序是：捕获 spec / route / session / Detector / operation 身份 → 验证 capability → 初始化操作健康记录 → 创建 factory/Shadow → 撤销旧非终态权限 → 显式移交 Detector → 取得安装回执 → 跨 await 重新校验 → 无 await 发布实际 snapshot → 退休 owner 清理旧资源。

调用过 async 替换函数不是移交证明。`SpeakerVerifierReplacementOperation` 明确记录接纳、所有权和结果：

- 尚未创建：不承担 Shadow 关闭义务。
- 创建后未移交：安装操作负责关闭 Shadow 及对应 factory。
- Detector 已接纳：Detector / 替换操作负责关闭。
- 已挂载后退休：捕获该实例的退休 owner 负责关闭。

Detector 替换/消失、route/session 漂移、新目标接管、authority 撤销或回执 ownership 不符都不能返回安装成功。旧任务只能清理自己捕获的实例，不能按“当前字段”关闭后继实例。

取消、超时和异常使用同一对偶清理：未移交则关闭；已移交但结果待定则先撤销权限、查询/结算真实结果；物理关闭不确定则保留 cleanup owner 和诊断。每个 Runtime 最多容纳两个未完成退休安装，满载时停止新增 Shadow 并报告 degraded。

本合同不承诺 Python timeout 能终止失控 native 调用；不能关闭时必须如实保留未结清义务，不能丢引用后报告成功。

## 5. 跨 manager 激活与持久化

Registry 区分已提交 A、暂存 B、各 manager 实际安装和恢复目标。一次激活共享 `STAGED → COMMITTED → REVOKED` authority；REVOKED 不可逆。共享权限有效仍不等于某个旧 installation identity 有效。

录入提交顺序为：

```text
stage Profile
→ prepare activation（B 可准备/挂载，但不能提供正式证据）
→ 保存 preference
→ commit Profile
→ swap Service Profile
→ commit activation authority
```

继续复用现有 enrollment suppression，不增加第二套录音抑制。普通初始化/启用可通过便捷 activate 封装 prepare/commit；不要求拆 HTTP API。

未提交 B 失败时先同步 revoke B，再以新 activation revision 恢复 A，逐 manager 恢复/撤下错误实例；失败义务必须保留。路由 unsupported 不能结清回滚：B 无权限、A 仍为目标、恢复待定；回到支持路由时重装 A。

磁盘 commit 已完成后取消必须先结算 Service / authority，再传播取消，不能盲目恢复磁盘 A。commit 结果不确定时先取得确定结果。shutdown 或更新已接管时，旧事务不能重新激活 B。

这不是多个 manager 的物理替换原子事务。保证的是权限切换可线性化、实际状态可核对、未完成的恢复义务不会消失。

## 6. 健康与公开状态

健康事件携带 installation identity、单调 health revision 和完整原因集合。操作健康记录先于回调建立；安装完成读取最新记录，不能无条件清除 degraded。只接受当前/正在安装实例；同 revision 相同内容幂等、倒退丢弃、同 revision 冲突需诊断。旧实例 degraded/healthy 都不能修改新实例，恢复只能清除本实例原因；旧实例 cleanup 故障单列。

公开状态不暴露内部安装身份：

| 情况 | effective reason / enabled |
| --- | --- |
| 用户关闭 | `disabled` / false |
| 无 Profile、音频合同不匹配等 | 保持既有原因 / false |
| 已保存配置，无活动语音路由 | `activation_pending` / false |
| 当前活动语音路由不支持 | `unsupported_asr_route` / false |
| 安装/回滚未完成 | `activation_pending` / false；明确失败为 `runtime_degraded` |
| 活动语音 manager 均匹配当前目标且健康 | `ready` / true |

空闲 manager 不拖累正常活动语音 manager，但保留安装义务。文本会话按真实 input mode 排除，不能只看 `is_active`。某个 unsupported manager 不撤销另一正常 manager 的有效实例。Service 汇总必须固定同一配置 revision，旧 READY 不得覆盖新事务。

READY 只表示当前路由与实例已正确安装且没有已知故障，不是识别准确率或下一次推理成功保证。Web/Electron 共用 `static/js/voice_identity.js`，八种 locale 共用 `reasonActivationPending`：“设置已保存，等待语音链路就绪”。

## 7. 诊断、验证与回滚

诊断区分 requested/installed/stale/deferred/unsupported、rollback pending/settled、unadopted cleanup、retired timeout/capacity、stale health/revision conflict、READY mismatch。仅低基数原因码和不可逆短身份，不记录 PCM、embedding、分数或完整文本。

必须用 Event/Barrier 控制路由切换、取消、超时、接管、部分成功和持久化边界；关键失败场景重复至少 50 次，并以恢复旧错误顺序的变异确认测试能失败。自动化至少覆盖两种注册顺序、同 Profile 重启、A→B→C、混合 manager、unsupported 回滚后重装、移交前后取消、晚到健康、cleanup 满载、禁用/删除及所有既有声纹/录入不变量。

所有 Python 验证使用 `uv run`。定向全绿不是全仓全绿；历史 242/256 等结果不算本次验收。Electron 真实麦克风和私有真实语料必须另行验证。逐符号 upstream impact、HIGH/CRITICAL 预告、完整非 partial/truncated 的 detect-changes 和核心人工 review 都是上线门禁。

紧急降级先撤销声纹权限，保留期望配置并显示不可用，ASR 按既有 fail-open 继续；不恢复旧的 unsupported 当成功、盲目复用 factory 行为，也不撤销正式 DENY。

## 8. 本工作树验证记录（2026-09-05）

本轮按用户要求不运行全仓测试；后续只执行本次改动的定向选择。以下是分批结果，不可相加成一次完整测试通过率：

- Registry/Service 安装事务组合：422 passed；包括持久化取消、失败补偿、unsupported 恢复、同步身份退休和变异反证。
- Runtime 资源/健康：移交前后取消、旧实例 close 失败、未接纳 Shadow 清理、健康覆盖、容量边界均有 Event/锁控制的 50 次重复场景；删除清理或恢复无条件 healthy 的变异会失败。
- Admission 写锁撤权：199 passed 的定向组合；已入队但尚未裁决的旧事实会被拦住，正式 DENY 保持。
- Runtime exact FIFO 新增选择：51 passed（50 次撤权后排队 final 恰好 FORWARD 一次、1 次移除 final 保留逻辑的变异）。final 已 pending 时不会重放旧 partial，隔离缓存仍必须清空。
- Core 正常 reset、安装中 reset、直接替换/关闭实际挂载：新增选择 200 passed；不把句间候选 epoch 当安装 epoch。
- 共享前端状态：Node 57 passed；8 个 locale 已同步。自动化不代替 Electron 真麦克风验收。
- 用户收窄测试前已完成的 Core 基线对照：`faf4ded97` 与当时工作树均为 390 passed / 34 failed，失败名称集合一致。后续未重跑该完整文件，不宣称全仓全绿。

索引以 `GITNEXUS_MAX_FILE_SIZE=1024` 纳入超过默认 512 KB 的 Runtime；动态调用和执行流预算仍存在缺口，图谱低风险计数不能替代人工 HIGH 风险判断。`detect-changes --scope all` 使用完整结构化结果核对，新增文件也必须进入差异范围。

最终结构化检查覆盖 39 个差异文件、305 个符号，返回列表长度与总数一致，未出现 partial/truncated 标记；其中包括独立保留的 17 行既有测试修改。affected processes 为 0 不是无影响证明，执行流预算和动态调用限制仍须在 review 中保留。

未完成的发布门禁：主开发者核心 review、Electron/真实麦克风和真实 Profile 验收。本节记录提交前的验证状态，不代表已经满足发布条件；原有 `test_admission_runtime_reset.py` 17 行修改独立保留，不纳入本次安装生命周期提交。诊断用 detached 基线 worktree 保留在 `E:/Work/CODE/neko-speaker-baseline-faf4ded97`，不属于产品修改。
