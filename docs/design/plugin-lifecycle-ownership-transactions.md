# Issue #2994：插件文件所有权与事务边界设计

- 状态：已完成并合入；本文保留为 Issue #2994 的设计与交付记录
- 日期：2026-08-29
- 完成复核：2026-09-01，`upstream/main` @ `3e7da108`
- 权威 Issue：<https://github.com/Project-N-E-K-O/N.E.K.O/issues/2994>
- 最新范围 review：<https://github.com/Project-N-E-K-O/N.E.K.O/issues/2994#issuecomment-5461019156>

## 1. 决策摘要

本轮不是插件系统大重构，也不是存储层重写。目标只有四项：

1. 用现有 `LockEntry.root_id + channel` 集中判断 N.E.K.O 是否拥有插件文件；
2. manual 插件只有经过绑定确认才可由本地包或 Market 接管；
3. 把卸载的代码目录与 package profile 事务迁出 `lifecycle_service`；
4. 收窄现有 `replace_plugin` 接口，删除恒定回调装配和 Market 的
   `dict[str, Any] + **kwargs` 路径。

本轮明确不引入：统一 Registry、CAS、JSONL、schema migration、持久化
`managed` 字段、任意多个 user 候选、通用业务数据回滚或新的巨型
`PluginManagement` facade。

设计原则是：先修正危险行为，再迁移职责，最后收窄接口；每个阶段都保持
现有 API 可用，并在同一阶段删除被替代的知识，不长期保留两套路径。

## 2. 历史事实基线与证据优先级

Issue 的代码链接以 `6b6812b3` 为历史事实基线，设计阶段又核对了
`upstream/main` @ `56f068d2`。相关切片现已完成；当前行为应以最新代码和测试
为准，本文中的分支、阶段和“当前缺口”措辞仅记录当时的实施上下文。

发生冲突时按以下顺序判断：

1. 当前 `upstream/main` 的代码与测试；
2. Issue 当前正文；
3. wislap 的最新范围 review；
4. 本设计文档；
5. 历史评论和恢复文档仅作调查线索。

### 2.1 当前已经存在的能力

- 每个 `plugin_id` 最多有一个 builtin 槽位和一个 user 槽位；
- user 槽位的 `channel` 为 `manual`、`imported` 或 `market`；
- `replace_plugin` 已是 Plugin CLI 与 Market 共用的文件替换实现；
- `serialized_plugin_operation` 已提供进程内与跨进程串行化；
- `plugins.lock.json` 与运行偏好文件有不同生命周期，并各自原子写入；
- 磁盘扫描仍是候选是否存在的事实来源；
- 插件代码、package profile 与 `config/data/cache` 已有分离边界。

### 2.2 已解决的三个缺口

| 缺口 | 实施前表现 | 已交付结果 |
| --- | --- | --- |
| 卸载所有权守护缺失 | user root 下的 manual 目录可能被直接删除 | 所有卸载入口统一 fail-closed |
| 文件事务接口过浅 | 两个调用方重复装配恒定回调；Market 丢失类型 | `replace_plugin` 只接收变化项 |
| 卸载职责放错位置 | lifecycle 直接管理代码、profile 与来源记录 | lifecycle 只调用结构化卸载边界 |

## 3. 候选与文件所有权合同

### 3.1 当前候选上限

```text
plugin_id
├── builtin slot: 0..1
└── user slot:    0..1 (manual | imported | market)
```

Market、本地包和 manual 不在本轮变成三个可同时保留的 user 副本。新包替换
当前 user 槽位；只有出现已确认的多副本用户场景后，才重新讨论目录布局和
候选选择持久化。

### 3.2 所有权是派生规则，不是新状态

唯一规则函数应是纯函数，并由所有卸载入口复用：

```python
def can_neko_uninstall(entry: LockEntry) -> bool:
    return entry.root_id == "user" and entry.channel in {"imported", "market"}
```

| `root_id` | `channel` | 默认卸载 | 原因 |
| --- | --- | --- | --- |
| builtin | builtin | 拒绝 | 随应用分发，不属于用户安装事务 |
| user | manual | 拒绝 | 目录由用户或开发者维护 |
| user | imported | 允许 | 由本地包安装器管理 |
| user | market | 允许 | 由 Market 安装器管理 |
| 任意未知组合 | 任意 | 拒绝 | 无法证明所有权时 fail-closed |

路径白名单继续保留，但它只回答“路径是否允许操作”，不能代替所有权判断。
卸载必须同时通过：

1. 精确 install-source entry 可读取且未 removed；
2. entry 的 `plugin_id`、`root_id`、`directory_name` 与待删目录一致；
3. `can_neko_uninstall(entry)` 为真；
4. 现有路径与 symlink/reparse-point 安全检查为真。

若 manager 未初始化、处于 degraded/read-only 状态、entry 缺失或字段不一致，
不得猜测为 managed。操作返回可解释的冲突或服务不可用错误，不删除目录。

建议的稳定错误语义：

| 条件 | 错误码 | HTTP |
| --- | --- | --- |
| builtin | `PLUGIN_UNINSTALL_BUILTIN_FORBIDDEN` | 403 |
| manual | `PLUGIN_MANUAL_NOT_MANAGED` | 409 |
| ownership 无法确认 | `PLUGIN_UNINSTALL_OWNERSHIP_UNKNOWN` | 409 |
| install-source 只读/损坏 | 沿用 `INSTALL_SOURCE_READ_ONLY` | 503 |
| 路径不安全 | 沿用 `PLUGIN_DELETE_FORBIDDEN_PATH` | 403 |

最终错误码可沿用项目现有命名规范，但上述条件不得合并成模糊的 500。

## 4. Manual 接管合同

接管不是新的持久状态。它是一次经过明确授权的 user 槽位替换：

```text
manual --确认并替换成功--> imported | market
manual --任意阶段失败----> manual（原目录和原 entry 恢复）
```

### 4.1 计划与确认

复用现有 `PluginInstallPlan` 与 replacement action，不新增 `adopt` action：

- `action` 仍为 `upgrade`、`reinstall` 或 `downgrade`；
- `reason = "manual_takeover"`；
- `current_source = "manual"`；
- `target_source = "imported"` 或 `"market"`；
- 使用现有显式确认交互，文案必须说明目录所有权会转移。

用户文案的语义必须包含：

> 目标插件目录由用户手动维护。继续后将替换该目录；成功后该 user 候选由
> N.E.K.O 管理。

### 4.2 确认证据

确认不能只是一个未绑定计划的布尔值。令牌或等价证据至少绑定：

- 待安装包的内容摘要；
- `plugin_id` 与目标目录；
- 当前目录 manifest/内容指纹；
- 当前 install-source entry 的 ownership 指纹（至少包含
  `root_id/channel/directory_name/plugin_id/updated_at`）；
- Market 路径还要绑定权威 release 摘要。

执行替换前必须在 `serialized_plugin_operation` 内重新读取目录与 entry。
任何变化都返回 plan changed，不继续替换。

### 4.3 失败与成功

- 接管成功后，现有来源记录更新为 `imported` 或 `market`；
- 接管失败时恢复原 manual 目录和原 `LockEntry`；
- rollback 失败必须报告为 incomplete，不得把 ownership 写成已接管；
- 第一阶段不支持 `force=true` 删除 manual，也不增加 adoption ledger。

## 5. 目标事务边界

最终的逻辑边界为 `installation_transactions`，只暴露两个领域操作：

```text
installation_transactions
├── replace_plugin(...)
└── uninstall_plugin(plugin_id)
```

它不是包办扫描、查询、路由和 UI 的 facade。它只拥有安装器文件事务及其所需
的固定运行态协调。

建议最终代码布局：

```text
plugin/server/application/plugins/installation_transactions/
├── __init__.py       # 仅导出公开操作、结果和错误
├── common.py         # 目标校验、备份/恢复、运行态协调、公共结果语义
├── replace.py        # 从 upgrade_support 收口后的替换事务
└── uninstall.py      # 所有权门禁、代码/profile/source 卸载事务
```

`install_source` 继续拥有来源记录的模型与持久化；`registry_service` 继续拥有
扫描和有效来源计算；`lifecycle_service` 继续拥有生命周期 API、事件和领域错误
映射，但不再实现包文件事务。

迁移期间允许短暂移动代码，但一个阶段完成时不得同时保留两个生产实现。
`upgrade_support.py` 只有在两个调用点都切换后才删除；不建立长期 re-export
兼容层。

## 6. 卸载事务设计

### 6.1 外部接口

调用方只提供逻辑身份：

```python
async def uninstall_plugin(plugin_id: str) -> UninstallPluginResult:
    ...
```

调用方不传删除函数、来源 manager、profile 路径、stop/start 回调或 rollback
顺序。事务内部从当前权威服务解析并复核这些依赖。

结果至少区分：

```python
@dataclass(frozen=True, slots=True)
class UninstallPluginResult:
    plugin_id: str
    deleted_from_disk: bool
    deleted_profile_dir: Path | None
    restored_builtin: bool
    preference_action: Literal["preserved", "cleared"]
    filesystem_rollback: Literal["not_needed", "completed", "incomplete"]
    runtime_restart: Literal["not_needed", "succeeded", "failed"]
    cleanup_pending: bool
```

对外响应可保留现有字段，并以新增字段表达更准确的结果，避免一次性破坏前端。

### 6.2 阶段与提交点

```text
preflight
  -> ownership
  -> stop
  -> stage_profile
  -> stage_code
  -> update_source
  -> refresh_and_preferences
  -> COMMIT
  -> restore_builtin_runtime
  -> cleanup_staging
```

阶段合同：

1. `preflight`：确认插件、目录、执行根与持久数据根分离；
2. `ownership`：精确读取 entry，同时执行所有权与路径双重门禁；
3. `stop`：仅在原候选运行时停止；
4. `stage_profile`：只暂存当前包独占且安装器拥有的 profile；
5. `stage_code`：通过同文件系统 rename 暂存代码，不立即 `rmtree`；
6. `update_source`：软删除来源记录；失败视为事务失败，不再仅记录 warning；
7. `refresh_and_preferences`：刷新磁盘事实；恢复 builtin 时保留偏好，无候选时
   清理运行偏好；
8. `COMMIT`：来源记录、扫描结果与偏好合同已经一致，原 user 路径不再有效；
9. `restore_builtin_runtime`：原候选运行且 builtin 恢复时尝试启动；失败单独报告；
10. `cleanup_staging`：永久清理暂存代码/profile；失败不反转已经提交的卸载，
    但必须返回 `cleanup_pending`。profile 可复用现有受限路径的延迟清理；代码
    备份只能留在事务自己的受控 backup root，不能写入可删除任意路径的通用队列。

提交点之前任何失败都必须尽力按逆序补偿：

```text
restore source entry
-> restore code directory
-> restore package profile
-> refresh registry
-> restart original runtime when it was running
```

`filesystem_rollback=completed` 只表示代码目录、安装器拥有的 profile 和来源
记录恢复；`runtime_restart` 必须单独报告。`config/data/cache` 从未进入事务，
因此不属于 rollback。

### 6.3 lifecycle 的最终职责

`PluginLifecycleService.delete_plugin()` 最终只做：

1. 调用 `uninstall_plugin(plugin_id)`；
2. 把结构化错误映射为现有领域/HTTP 错误；
3. 发出 lifecycle event；
4. 返回兼容响应。

以下知识必须从 `lifecycle_service.py` 移出：

- 代码目录删除；
- profile ownership 推断、暂存、恢复与最终清理；
- deferred profile cleanup 记录；
- install-source removed 更新；
- 卸载文件事务的补偿顺序。

## 7. 替换事务接口收窄

当前 `replace_plugin` 的以下依赖在两个调用点中恒定或等价，应由事务内部拥有：

- `plugin_is_running`；
- `stop_plugin_for_replace`；
- strict restart；
- 默认 backup cleanup；
- `plugin_id` 与目标目录的公共身份校验。

目标接口只保留每次操作真正变化的内容：

```python
async def replace_plugin(
    *,
    layout: PluginLayout,
    install_new: Callable[[], Awaitable[dict[str, object]]],
    additional_targets: tuple[Path, ...] = (),
    preserve_targets: tuple[Path, ...] = (),
    initialize_runtime_config: bool = True,
    validate_backup: Callable[[Path], Awaitable[None]] | None = None,
    validate_channel_specific: Callable[[], Awaitable[None]] | None = None,
    on_rollback_start: Callable[[], None] | None = None,
) -> ReplacePluginResult:
    ...
```

这是边界示意，不要求机械采用函数名；硬要求是：

- CLI 不再传五个恒定生命周期/cleanup 回调；
- 公共 identity 校验只实现一次；
- Market 只保留 builtin override 等渠道特有校验；
- `_replace_market_plugin_transaction` 使用显式类型参数直接调用，不接收
  `dict[str, Any]`；
- 不创建拥有原 12 个字段的 request 来伪装接口收窄。

## 8. API、UI 与兼容合同

### 8.1 后端兼容

- 保留现有安装 action 集合和主要响应字段；
- manual takeover 使用现有 replacement action，只增加可解释 reason/source；
- 现有 `rollback_status` 在过渡期继续返回，并明确它对应 filesystem rollback；
- 新增 `runtime_restart`/`cleanup_pending` 时采用 additive 字段；
- 既有错误码能准确表达时沿用，ownership 错误不得映射成通用 500；
- cached Market 客户端传入的 legacy `rename` 仍按现状归一化为 `fail`。

### 8.2 前端确认

manual takeover 必须在 Package Manager 与 Market 两条入口都得到明确确认，不能
只在某个后端分支加保护。若需要新增用户可见文案，必须同步当前完整的 8 个
locale，并验证 key、占位符和格式一致。

旧客户端没有能力提供绑定确认时，manual takeover 应拒绝并返回
confirmation-required，而不是静默沿用旧的自动替换行为。

## 9. 分阶段 PR 计划

原计划为四个顺序 PR；用户随后明确授权把前两个安全切片合并到当前 PR。
PR 3 与 PR 4 仍不采用 stacked PR：当前安全 PR 合并后，再从更新后的目标分支
依次创建。运行时 ID 调查仍不混入这些实现 PR。

### PR 1：卸载所有权门禁

- 建议分支：`codex/issue-2994-uninstall-ownership`
- 建议标题：`fix(plugin): enforce uninstall ownership`

范围：

- 集中 `root_id + channel` 所有权规则；
- 所有卸载入口在任何文件操作前 fail-closed；
- 精确校验 manager 状态、active entry、插件 ID、root、目录和 channel；
- 保留现有路径与链接安全检查作为独立第二道门；
- 覆盖 builtin、manual、imported、market 和所有未知 ownership 测试。

完成条件：manual/builtin/unknown 不能删除，imported/market 的成功卸载行为不
回归。此 PR 不移动卸载实现，也不加入接管确认。

### PR 2：Manual 接管确认

- 建议分支：`codex/issue-2994-manual-takeover`
- 建议标题：`fix(plugin): require confirmation for manual takeover`

范围：

- install plan 使用既有 replacement action 和 `manual_takeover` reason；
- 确认证据绑定包、目标、当前目录内容与精确 ownership 快照；
- 在操作锁内重验确认对象；
- 本地包与 Market 两个入口使用同一规则；
- 同步相关界面当前实际支持的全部 locale；
- 替换失败恢复原 manual 目录和原 `LockEntry`。

完成条件：未确认、旧确认或对象已变化时均不能替换 manual；成功后 ownership
变为 imported/market；任一提交前失败仍保持 manual。此 PR 不新增持久化
`managed` 状态，也不借机收窄整个 replacement 接口。

### PR 3：卸载事务迁移

- 建议分支：`codex/issue-2994-uninstall-transaction`
- 建议标题：`refactor(plugin): move uninstall into an installation transaction`

范围：

- 引入窄 `uninstall_plugin(plugin_id)`；
- 原样迁移当前权威版本的 profile ownership、安全检查、旧记录兼容和延迟清理；
- 代码与 profile 都先 staging，代码使用同文件系统 rename；
- 明确提交点、结构化结果和补偿顺序；
- 代码延迟清理只接受事务生成且带精确提交标记的受控 backup；
- 来源记录、registry refresh、偏好语义和运行态恢复均有明确阶段；
- `lifecycle_service` 在同一 PR 删除被迁出的包文件辅助函数及事务知识。

完成条件：生命周期层只保留调用、错误映射、事件和兼容响应；所有故障注入
能证明提交前补偿或提交后告警语义；`config/data/cache` 从不进入事务。此 PR
不增加独立删除保留 profile 的产品功能，也不修改 replacement 接口。

### PR 4：替换接口收窄

- 建议分支：`codex/issue-2994-replace-boundary`
- 建议标题：`refactor(plugin): narrow plugin replacement boundary`

范围：

- 行为等价迁移当前已验证的 replacement 实现；
- 恒定运行态依赖和公共 identity 校验内聚；
- Plugin CLI 删除重复闭包；
- Market 删除 `dict[str, Any] + **kwargs`；
- 在调用点迁移完成后删除旧 `upgrade_support` 生产路径。

完成条件：两个调用方只传变化项；共享事务测试继续覆盖代码/profile 回滚与
持久数据隔离；不存在长期兼容转发层或巨型 package-management facade。

### PR 4 之后：兼容行为核查闸门

运行时 ID 自动改名不是预先承诺的 PR 5。先调查它是否仍有真实消费者并把证据
写回 Issue；没有证据前不新增“允许改名”测试。只有调查能够证明应当改变行为
时，才从当时最新目标分支另开独立 Issue 或最小 PR，拒绝同 ID 冲突并逐步删除
残留改名逻辑。

每个 PR 的描述必须重复自己的硬范围、必须执行的测试和明确排除项。无关的基线 CI
失败记录 base SHA 证据，不混入本 Issue 修复。

## 10. 测试矩阵

### 10.1 所有权

- builtin 拒绝；
- manual 拒绝；
- imported 允许；
- market 允许；
- manager 缺失、degraded、entry 缺失、removed、未知 channel、目录或 ID 不符
  均 fail-closed；
- 路径、symlink 与持久数据根防护继续生效。

### 10.2 Manual 接管

- plan 使用 replacement action + `manual_takeover` reason；
- 无确认、错误确认或旧确认均拒绝；
- entry、manifest、目录或包变化使确认失效；
- imported 与 market 成功后记录各自 channel；
- install、validate、source write、restart 任一失败均验证原 manual ownership；
- 两个入口的用户可见提示一致。

### 10.3 卸载事务

对 `stop`、profile staging、code staging、source update、refresh、pre-commit
preference update 逐阶段故障注入，验证：

- 代码/profile/source 的恢复结果；
- 原先运行时才尝试恢复运行；
- rollback 与 runtime restart 分别报告；
- commit 后 builtin 启动失败不伪装成文件 rollback 失败；
- cleanup 失败留下安全的 deferred 工作，不恢复已提交卸载；
- 删除 user 并恢复 builtin 时保留偏好；删除最后候选时清理偏好；
- `config/data/cache` 始终不被读取、移动或删除。

### 10.4 替换接口

- 现有 replacement 测试迁移后语义不变；
- CLI 与 Market 都经过同一公共 identity 校验；
- Market 的渠道特有校验仍在操作锁内；
- 静态检查能看到完整调用签名；
- 并发测试使用 barrier/event/可控 future，不使用任意 sleep；
- 单元测试只使用临时目录和伪造 artifact，不访问真实网络或用户插件目录。

## 11. 验收与停止条件

本 Issue 完成必须同时满足：

- 所有卸载入口使用同一所有权规则；
- manual、builtin、未知 ownership 均不能默认删除；
- manual 接管在两个安装入口都需要绑定确认；
- 卸载有明确提交点、阶段结果和可验证补偿；
- lifecycle 不再包含包文件/profile 事务知识；
- replace 调用方不再装配恒定回调；
- Market 不再通过 untyped kwargs 调用替换；
- 现有 API 兼容字段与 8 locale 要求得到验证；
- 没有引入本轮排除的存储、候选或数据回滚能力；
- 每个迁移阶段都实际删除旧知识，而非增加长期 facade。

如果实现需要新增 Registry/schema、任意多候选、生产依赖、持续后台任务或
业务数据快照，应停止当前 Issue，提交独立用户场景和设计审查，不得顺手扩大。

## 12. 恢复文档处置

切换中转站前恢复的六份文档保留为取证材料，但不是实现依据：

| 恢复文档 | 处置 |
| --- | --- |
| `plugin-registry-unification-design.md` | 拒绝：以统一 Registry/CAS 为核心 |
| `architecture-review-unified-registry.md` | 拒绝：错误优先统一存储 |
| `v3_migration.md` | 拒绝：本轮无 schema migration |
| `installation-coordinator-design.md` | 仅保留“调用方不掌握事务顺序”的原则；拒绝 Registry port、任意候选与大 Coordinator |
| `phase-2-coordinator-implementation-notes.md` | 仅作污染分支审计，不作为待办清单 |
| `phase-0-1-implementation-summary.md` | 逐项以当前代码重验，不能据其声称已完成 |

任何内容若要重新采用，必须能直接映射到本设计的当前缺口、测试或兼容合同。

## 13. 本轮之后的长期方向

本轮完成后，架构会具备可递进演化的边界，但不会预先实现未来能力：

- 有真实多 user 副本需求时，再扩展候选模型和目录布局；
- 有环境复现用户场景时，另设无机器状态的声明格式；
- 有插件数据迁移协议时，再讨论 schema 兼容或插件自管快照；
- 有可复现端到端性能问题时，再按分阶段 benchmark 优化；
- 有无法收口的多写者证据时，再评估额外并发控制。

这些都是边界上的后续能力，不是本 Issue 的隐含 Phase 4。
