/**
 * i18n.js — 文案字典 + 轻量读取 API.
 *
 * 当前只定义 `zh-CN` 字典. Settings → UI 预留语言切换位 (P04 实装),
 * 目前 `setLocale` 只支持 `zh-CN`.
 *
 * 约定:
 *   - key 采用点号命名空间:  `topbar.session.new` / `workspace.chat.title`
 *   - 未命中的 key 返回 key 本身并在 console 发 warn, 便于开发期发现遗漏
 *   - hydrate(root) 扫描 `[data-i18n]` / `[data-i18n-title]` / `[data-i18n-placeholder]`
 *     属性并回填文案, 无需在每个模块手写 textContent 赋值
 */

export const I18N = {
  'zh-CN': {
    app: {
      name: 'N.E.K.O. Testbench',
      tagline: '独立测试生态',
    },
    topbar: {
      session: {
        label: '会话',
        none: '(未建会话)',
        new: '新建会话',
        delete: '销毁当前会话',
        load: '加载存档 / 导入 JSON…',
        save: '保存到存档',
        save_as: '另存为…',
        restore_autosave: '恢复自动保存…',
        restore_autosave_hint: '崩溃恢复 / 自动保存入口',
        not_implemented: '该功能将在后续版本实装',
      },
      stage: {
        label: '阶段',
        chip_placeholder: '未启用',
        no_session: '未建会话',
        chip_title: stageName => `当前阶段: ${stageName} · 点击展开 Stage Coach`,
        chip_title_expanded: '点击折叠 Stage Coach',
      },
      timeline: {
        label: '时间轴',
        chip_placeholder: '无快照',
        chip_summary: (total, backup) => backup > 0
          ? `${total} 条 · ${backup} 备份`
          : `${total} 条`,
        chip_no_session: '(未建会话)',
        chip_expand_hint: '点击展开快照时间线',
        chip_collapse_hint: '点击折叠快照时间线',
        panel_title: '快照时间线',
        panel_hint: '最近 N 条快照按时间倒序; 点 [回退] 恢复到该快照.',
        panel_mechanism_fmt: (max_hot, debounce_seconds) =>
          `机制速记: 最新 ${max_hot} 条在内存, 溢出压到磁盘 · 同类触发 ${debounce_seconds}s 内合并 · 回退自动留 pre_rewind_backup 备份 · 完整说明见 [打开完整时间线].`,
        panel_empty: '尚无快照; 发消息 / 编辑 / 记忆操作会自动建快照.',
        panel_loading: '加载中…',
        panel_load_failed: fmt => `加载快照失败: ${fmt}`,
        panel_open_full: '打开完整时间线',
        panel_manual_btn: '+ 手动建快照',
        panel_summary: (hot, cold, max_hot) =>
          `内存 ${hot}/${max_hot} · 磁盘 ${cold}`,
        panel_row_rewind: '回退',
        panel_row_view: '查看',
        panel_backup_badge: '备份',
        panel_compressed_badge: '磁盘',
        show_more_fmt: n => `查看更多 (+${n}) ›`,
      },
      error_badge: {
        title_none: '最近错误 (0)',
        title_some: count => `最近错误 (${count})`,
        empty: '暂无错误',
        view_all: '查看全部',
      },
      menu: {
        label: '菜单',
        export: '导出…',
        reset: '重置…',
        about: '关于',
        diagnostics: '打开诊断',
        settings: '打开设置',
      },
    },
    tabs: {
      setup: 'Setup 准备',
      chat: 'Chat 对话',
      evaluation: 'Evaluation 评分',
      memory_trace: 'Memory Analysis 记忆系统分析',
      diagnostics: 'Diagnostics 诊断',
      settings: 'Settings 设置',
    },
    workspace: {
      setup: {
        title: 'Setup 准备',
        placeholder_heading: '测试环境准备',
        placeholder_body:
          '此 workspace 用于配置角色 / 记忆 / 虚拟时钟 / 从真实角色导入. '
          + '具体子页在后续阶段注入.',
        todo_list: [
          { tag: 'P05', text: 'Persona + Import 子页 (人设表单 + 一键从真实角色拷贝)' },
          { tag: 'P06', text: 'Virtual Clock 子页 (bootstrap / live cursor / per-turn default)' },
          { tag: 'P07', text: 'Memory 四子页 (Recent / Facts / Reflections / Persona) 读写' },
          { tag: 'P10', text: 'Memory 操作触发 + 预览 drawer' },
        ],
      },
      chat: {
        title: 'Chat 对话',
      },
      evaluation: {
        title: 'Evaluation 评分',
        placeholder_heading: '评分中心',
        placeholder_body:
          '四个子页: Run / Results / Aggregate / Schemas. ScoringSchema 作为一等公民.',
        todo_list: [
          { tag: 'P15', text: 'Schemas 子页 + 内置三套 schema' },
          { tag: 'P16', text: 'Run 子页 + 四类 Judger' },
          { tag: 'P17', text: 'Results + Aggregate 子页 + 导出报告' },
        ],
      },
      diagnostics: {
        title: 'Diagnostics 诊断',
        placeholder_heading: '运维诊断',
        placeholder_body:
          '出问题时才来. 包含 Logs / Errors / Snapshots / Paths / Reset 五个子页.',
        todo_list: [
          { tag: 'P19', text: '全局异常中间件 + Errors/Logs 子页' },
          { tag: 'P20', text: 'Paths / Snapshots / Reset 子页' },
        ],
      },
      settings: {
        title: 'Settings 设置',
        placeholder_heading: '集中配置',
        placeholder_body:
          '四组模型配置 (chat/simuser/judge/memory) + API Keys 状态 + '
          + 'Providers 只读 + UI 偏好 + About.',
        todo_list: [
          { tag: 'P04', text: 'Models / API Keys / Providers / UI / About 五个子页' },
        ],
      },
    },
    memory_trace: {
      title: 'Memory Trace 记忆溯源',
      // 记忆系统分析 workspace 的子页菜单 (subnav). 记忆溯源是首个子页;
      // 后续记忆分析子页在此追加.
      nav: {
        overview: '系统概况',
        lineage: '记忆溯源',
        embedding_space: '向量空间',
        code_leads: '代码线索 (开发者)',
      },
      // 向量空间 (Embedding) 子页 (P28). 只读分析当前角色磁盘上已有的向量嵌入.
      embedding: {
        title: 'Embedding 向量空间',
        intro:
          '把当前角色每条记忆 (事实 / 反思 / 人设) 的向量嵌入 (embedding) 拿来做只读分析: '
          + 'PCA 降到 2D 看聚类 (散点), 点选一条看它在向量空间里的最近邻, '
          + '或对照"反思声明的来源事实"与"它语义上最像的事实" (语义源 vs 结构源). '
          + '向量由主程序后台异步生成; 测试台自建数据通常没有向量, 需从已跑过主程序的角色导入.',
        loading: '加载中…',
        select_hint: '在散点图里点选一个点, 这里显示它的内容与最近邻.',
        neighbors: '最近邻 (cosine)',
        neighbors_none: '没有可比较的最近邻 (同一向量空间里条目太少).',
        legend: '类型 (可勾选筛选)',
        jump_to_lineage: '在记忆溯源中查看',
        mode: {
          scatter: '散点',
          duplicates: '近重复',
          matrix: '相似度矩阵',
          bridges: '语义源 vs 结构源',
        },
        dup: {
          threshold: '相似度阈值 (cosine)',
          empty: '当前阈值下没有近重复对。调低阈值滑块可放宽。',
          count_fmt: (n) => `近重复对: ${n}`,
          capped_fmt: (n) => `过多, 仅显示分数最高的 ${n} 对。`,
        },
        matrix: {
          empty_heading: '没有可比的向量子集',
          empty_body: '需要主向量空间里至少有若干条已嵌入记忆。可在图例里调整类型筛选, 或先导入已嵌入的角色。',
          subset_fmt: (n) => `子集: ${n} 条 (已按相似度聚类重排)`,
          truncated_fmt: (total, shown) => `子集过大: 共 ${total} 条, 仅取前 ${shown} 条画矩阵。可用类型筛选缩小范围。`,
          scale_low: '低',
          scale_high: '高',
          hint: '颜色越深 = cosine 越高。对角线恒为 1。悬停看两条记忆与分数。',
        },
        reducer: {
          head: '降维算法',
          pca: 'PCA',
          umap: 'UMAP',
          ready: 'UMAP 已就绪, 可切换.',
          umap_hint: 'UMAP 按拓扑结构降维, 聚类更分明, 但需联网安装 (umap-learn '
            + '及其二进制依赖 numba/llvmlite). 点 UMAP 即开始按需安装, 装好后自动切换; '
            + '装不上会保持 PCA.',
          installing: '正在安装 UMAP (联网 pip install, 可能需要几分钟, 含编译 '
            + 'numba/llvmlite)…',
          fallback_small: '本向量空间条目过少, UMAP 暂回落到 PCA (需 ≥4 条).',
        },
        cluster: {
          toggle: '自动聚类',
          head: '聚类',
          loading: '聚类计算中…',
          empty: '没有识别出明显的簇 (条目太少或彼此都不够相似)。',
          summary_fmt: (k, noise) => `识别出 ${k} 个簇 · ${noise} 个离群点`,
          cc_note: 'sklearn 不可用, 用 numpy 连通分量近似聚类; 启用 UMAP 会带入 HDBSCAN, 更准。',
          proj_note: '簇在原始高维空间上划分; 投到 PCA 2D 可能看着有交叠 (UMAP 会分得更开), 这是降维固有现象。',
          llm_btn: '用 LLM 概括聚类',
          labeling: 'LLM 概括中…',
          llm_fallback: 'LLM 概括未成功, 暂用每簇最具代表性的记忆作标签。',
        },
        type: {
          fact: '事实',
          reflection: '反思',
          persona: '人设',
        },
        cov: {
          embedded_fmt: (n, total) => `已嵌入 ${n}/${total} 条`,
          missing_fmt: (n) => `缺失 ${n} 条 (无向量)`,
          stale_fmt: (n) => `过期 ${n} 条 (改过文)`,
          dim_fmt: (dim, count) => `主向量空间 ${dim} 维 · ${count} 条`,
          other_space_fmt: (n) => `另有 ${n} 条属于其它维度的向量空间 (不参与本图)`,
        },
        empty: {
          heading: '该角色没有可分析的向量',
          body: '当前角色的记忆里没有任何已嵌入的向量. 向量由主程序后台异步生成 — '
            + '请在主程序开启向量功能、让它跑出向量后, 再从 Setup → Import 导入该角色.',
        },
        bridges: {
          intro:
            '对每条反思, 比较它"声明的来源事实" (source_fact_ids, 结构源) 与"向量上最像的事实" (语义源). '
            + '一致说明归因可信; 偏离 (蓝色=语义相近却没被列为来源 / 灰色=列为来源却语义不近) 值得复查.',
          empty: '没有可对照的反思与事实 (需两者都已嵌入向量).',
          semantic: '语义最近事实:',
          extra: '声明却不相近:',
          fact_unembedded: '(未嵌入, 无法比对语义)',
          fact_missing: '(已删除/缺失)',
          jump: '在溯源中查看',
          jump_hint: '跳到记忆溯源子页并聚焦这条反思, 看它的结构化来源链.',
        },
      },
      // 系统概况 (Overview) 子页 (P29). 记忆系统分析的入口页: 一屏图景 + 自动发现 +
      // 下钻到溯源/向量空间. 只读, 不改记忆.
      overview: {
        title: '记忆系统概况',
        intro:
          '一屏看清当前角色记忆系统的运行状况: 记忆构成、嵌入覆盖、向量空间与聚类、流水线吞吐, '
          + '并自动排查冗余重复、矛盾记忆、归因偏离、结构孤儿、嵌入健康等问题。'
          + '每条发现可一键下钻到"记忆溯源"或"向量空间"看详情。本页只读, 不会修改任何记忆。'
          + '可选用 LLM 生成体检报告、对"待核对候选"做矛盾判定。',
        loading: '加载中…',
        reload: '刷新',
        load_failed: '加载失败',
        export: {
          button: '导出记忆分析',
          button_hint: '一键导出脱敏后的记忆数据与分析结论 (ZIP), 便于离线分析与共享',
          modal_title: '导出记忆分析 (脱敏 ZIP)',
          tier_heading: '脱敏档位',
          tier: {
            minimal: '最小 (仅去凭据)',
            standard: '标准 (默认, 假名化身份)',
            strict: '严格 (额外撤除原始转录)',
          },
          tier_desc: {
            minimal: '仅移除 api_key/token/cookie 等凭据; 保留真实姓名与全部正文。',
            standard: '在最小基础上, 对主人名/角色名做一致假名化 (全包同一映射), 其余正文保留。',
            strict: '在标准基础上, 整层撤除最原始的逐轮对话正文 (替换为结构占位); 事实/反思等抽象记忆正文仍保留。',
          },
          include_corpus: '包含对话语料 (conversation_corpus)',
          include_corpus_hint: '关闭后不导出原始对话 turns, 只导出记忆四文件与分析结论。',
          notice_head: '记忆脱敏说明 (务必阅读)',
          notice_body:
            '· 三档行为: minimal 只去凭据; standard 一致假名化身份并保留正文; strict 额外整层撤除原始逐轮转录、保留抽象记忆。\n'
            + '· 跨层一致性: 身份标识在对话与事实/反思中使用同一映射, 绝不会出现"对话里是 A、记忆里是 B"。\n'
            + '· 诚实限制: 只有身份标识被一致假名化; 自由正文里的其它个人披露不做自动清洗 (不可靠), 对外分享前请自行复核。\n'
            + '· standard 档仍包含对话与记忆正文; 若要对外分享建议用 strict。凭据在所有档位均被移除。',
          export_btn: '导出',
          exporting: '导出中…',
          ok_toast_fmt: (name) => `已导出: ${name}`,
          err: {
            no_session: '尚未创建会话或未选择角色, 无法导出。',
            busy: '会话正忙 (有其它操作占用), 稍后再试。',
            network: '网络错误, 导出请求未送达。',
            backend_fmt: (msg) => `导出失败: ${msg}`,
            download_fmt: (msg) => `下载失败: ${msg}`,
          },
        },
        no_session: {
          heading: '尚未创建会话',
          body: '先在顶栏新建会话并选择角色, 再来看记忆系统概况。',
        },
        no_character: {
          heading: '尚未选择角色',
          body: '当前会话还没有绑定角色; 在 Setup 选定角色后即可分析其记忆系统。',
        },
        empty: {
          heading: '该角色暂无可分析的记忆',
          body: '当前角色没有事实 / 反思 / 人设记忆。先在主程序或 Setup 让它产生记忆后再来概览。',
        },
        attention: {
          none: '未发现需要关注的问题 👍',
          some_fmt: (n) => `发现 ${n} 项需要关注`,
        },
        cards: {
          composition: {
            head: '记忆构成',
            fmt: (c) => `事实 ${c.facts}`
              + (c.facts_archived ? ` (+${c.facts_archived} 归档)` : '')
              + ` · 反思 ${c.reflections} · 人设 ${c.persona} · `
              + `纠正 ${c.corrections} · 对话 ${c.convo_turns} 回合`,
          },
          coverage: {
            head: '嵌入覆盖',
            fmt: (c) => `已嵌入 ${c.embedded}/${c.total} (${Math.round((c.embedded_ratio || 0) * 100)}%)`,
            detail_fmt: (c) => `缺失 ${c.missing} · 过期 ${c.stale} · 损坏 ${c.corrupt}`,
          },
          space: {
            head: '向量空间',
            fmt: (c) => (c.primary_dim
              ? `主空间 ${c.primary_dim} 维 · ${c.primary_count} 条`
              : '无可用向量空间'),
            other_fmt: (n) => `另有 ${n} 条属其它维度 (不可比)`,
          },
          clusters: {
            head: '聚类',
            fmt: (c) => (c.n_clusters
              ? `${c.n_clusters} 个簇 · ${c.noise_count} 离群`
              : '未识别出明显簇'),
          },
          pipeline: {
            head: '流水线漏斗',
            fmt: (c) => {
              const pct = (x) => (x == null ? '—' : `${Math.round(x * 100)}%`);
              return `晋升 ${pct(c.promote_rate)} · 否决 ${pct(c.reject_rate)} · `
                + `产出 ${pct(c.extract_yield)} · 待处理 ${c.pending}`;
            },
          },
          credibility: {
            head: '结论可信度',
            fmt: (conf) => `可信度: ${conf._levelLabel} (嵌入 ${Math.round((conf.embedded_ratio || 0) * 100)}%)`,
          },
        },
        severity: { bad: '严重', warn: '注意', info: '提示' },
        stage: {
          extract: '抽取', dedup: '去重', reflect: '反思', promote: '晋升',
          correct: '纠正', embed: '嵌入', structure: '结构',
        },
        category: {
          redundancy: '冗余重复', contradiction: '矛盾记忆', attribution: '归因',
          structure: '结构', embedding: '嵌入健康', pipeline: '流水线',
          fidelity: '晋升保真', retention: '留存质量',
        },
        findings_head: '自动发现',
        no_findings: '规则排查未发现明显问题。',
        info_show_fmt: (n) => `展开 ${n} 项提示`,
        info_hide: '收起提示项',
        drill: {
          lineage: '去记忆溯源查看 →',
          embedding: '去向量空间查看 →',
        },
        examples_more_fmt: (n) => `…等共 ${n} 项`,
        ai: {
          btn: 'LLM 体检报告',
          running: '生成中…',
          head: 'LLM 体检报告',
          unavailable: 'LLM 报告暂不可用 (见下方原因)。',
          hint: '基于上面的只读统计, 让记忆模型给出总体判断与优先建议 (仅建议, 不改记忆)。',
        },
        contra: {
          btn: '矛盾 NLI 裁决',
          running: '裁决中…',
          head: '矛盾裁决 (LLM NLI)',
          hint: '对"同主题待核对候选"逐对做自然语言推理, 判断是否真矛盾 / 重复 / 互补 / 无关。相似不等于矛盾。',
          none: '没有需要裁决的候选对。',
          empty_verdicts: '模型未给出有效裁决 (见原因); 下面是候选对原文。',
          relation: {
            contradiction: '互相矛盾', duplicate: '重复同义',
            complementary: '互补', unrelated: '其实无关',
          },
          verdict_fmt: (v) => `${v._relLabel}: ${v.reason || ''}`,
        },
        confidence: {
          level: { high: '高', medium: '中', low: '低' },
          note: {
            NO_EMBEDDINGS: '当前角色无向量 → 冗余/归因/晋升保真/矛盾候选等向量类排查不可用。',
            LOW_EMBED_COVERAGE: '嵌入覆盖偏低 → 向量类结论只覆盖部分记忆。',
            SPLIT_SPACES: '存在多个维度的向量空间 → 只对最大空间作比较, 其余未参与。',
            NO_TIME_DB: '缺少对话时间库 → 抽取产出率等对话相关指标可能不准。',
            NO_REFLECTIONS: '尚无反思 → 晋升/否决/归因相关排查为空。',
            FIDELITY_PARTIAL: '部分晋升缺少向量 → 晋升保真度仅部分可核验。',
          },
        },
        // 每条发现的标题 + 详情. detail(f) 接收整条 finding (用 f.count / f.data).
        finding: {
          A1: { title: '近重复记忆对',
            detail: (f) => `有 ${f.count} 对记忆相似度 ≥ ${f.data.threshold}, 疑似重复。`
              + (f.data.capped ? ' (过多, 仅取分数最高的若干对)' : '') },
          A2: { title: '高度重复的记忆簇',
            detail: (f) => `有 ${f.count} 个簇内部高度雷同, 像是同一件事被反复记录。` },
          A3: { title: '冗余代价',
            detail: (f) => `约 ${f.data.redundant} 条记忆可被合并 (占已嵌入的 `
              + `${Math.round((f.data.ratio || 0) * 100)}%, 分 ${f.data.groups} 组)。` },
          B1: { title: '已记录的矛盾信号',
            detail: (f) => `磁盘上有 ${f.count} 条已记录的冲突信号 (纠正 ${f.data.corrections} · `
              + `被否决反思 ${f.data.denied} · 被抑制人设 ${f.data.suppressed})。` },
          B2: { title: '同主题待核对候选',
            detail: (f) => `检索到 ${f.count} 对"同对象、语义相近但不完全相同"的记忆, 值得人工或 LLM 核对是否冲突 (相似≠矛盾)。` },
          N1: { title: '未解决的矛盾',
            detail: (f) => `有 ${f.count} 条纠正所针对的旧人设文本仍然在册 (纠正未生效)。` },
          C1: { title: '可能漏标的来源',
            detail: (f) => `有 ${f.count} 条反思存在"语义上很像、却没被列为来源"的事实, 归因可能有遗漏。` },
          C2: { title: '存疑的来源声明',
            detail: (f) => `有 ${f.count} 条反思列出的来源事实在语义上并不接近, 归因可能偏弱。` },
          D1: { title: '无来源的反思',
            detail: (f) => `有 ${f.count} 条反思没有任何有效的来源事实, 无法溯源。` },
          D2: { title: '无来源的人设',
            detail: (f) => `有 ${f.count} 条人设声称来自反思, 但其来源反思已不存在。` },
          D3: { title: '未被使用的事实',
            detail: (f) => `有 ${f.count} 条事实既未被任何反思引用、也未被吸收 (共 ${f.data.total_facts} 条事实)。` },
          D4: { title: '引用了已删除的事实',
            detail: (f) => `有 ${f.count} 条反思的来源声明里, 共引用了 ${f.data.dangling_refs} 处已不存在 (被删除) 的事实。`
              + '已删除的事实不应再被后续节点引用——这通常是删除事实时未同步清理引用它的反思所致, 会让这些反思的溯源链出现断点。' },
          E1: { title: '缺失嵌入',
            detail: (f) => `有 ${f.count}/${f.data.total} 条记忆没有向量, 无法参与向量分析。` },
          E2: { title: '过期嵌入',
            detail: (f) => `有 ${f.count} 条记忆改过文本但向量未更新 (stale)。` },
          E3: { title: '损坏嵌入',
            detail: (f) => `有 ${f.count} 条记忆的向量无法解码 (corrupt)。` },
          E4: { title: '向量空间分裂',
            detail: (f) => `有 ${f.count} 条向量属于非主维度空间, 与主空间不可比较。` },
          F2: { title: '晋升停滞',
            detail: (f) => `${f.data.reflections} 条反思里只有 ${f.data.promoted} 条晋升为人设 `
              + `(${Math.round((f.data.rate || 0) * 100)}%), 偏低。` },
          F3: { title: '反思积压',
            detail: (f) => `有 ${f.count} 条反思超过 ${f.data.age_days} 天仍停留在待处理 (共 ${f.data.pending} 条待处理)。` },
          F4: { title: '否决率偏高',
            detail: (f) => `${f.data.reflections} 条反思里有 ${f.data.denied} 条被否决 `
              + `(${Math.round((f.data.rate || 0) * 100)}%), 抽取质量可能有问题。` },
          G1: { title: '晋升语义漂移',
            detail: (f) => `有 ${f.count} 条人设与其来源反思的相似度 < ${f.data.threshold}, 晋升中语义发生了漂移。`
              + (f.data.unverifiable ? ` (另有 ${f.data.unverifiable} 条因缺向量无法核验)` : '') },
          H1: { title: '高价值事实被冷落',
            detail: (f) => `有 ${f.count} 条重要度 ≥ ${f.data.importance} 的事实从未被任何反思使用。` },
          H2: { title: '低质量事实',
            detail: (f) => `有 ${f.count} 条事实文本过短或缺少实体, 质量偏低。` },
          H3: { title: '僵尸事实',
            detail: (f) => `有 ${f.count} 条事实超过 ${f.data.age_days} 天既未吸收也未被引用。` },
        },
      },
      intro:
        '把当前角色的记忆按"对话 → recent 摘要 → 事实 → 反思 → 人设"分层画成只读节点流水线图, '
        + '看清一条记忆从哪来、到哪去. 实线 = 已落盘的真因果; 虚线 = 启发式推断 (Tier C 反向归因). '
        + '默认只画"有连线"的记忆 (即真正参与溯源的节点), 按来源对齐、留足间距; 大量"无连线"的事实/对话默认折叠, '
        + '点工具栏"显示未连线"可在下方网格区查看. 若图里没有连线, 点"推测全部源头"用文本相似度补出对话级来源虚线.',
      reload: '刷新',
      loading: '加载中…',
      focus_tip: '点选任意节点即聚焦: 自动把该记忆的整条溯源链 (上游来源 + 下游影响) 重排并缩放到最小画面完整呈现; 点击空白处取消聚焦, 恢复整图.',
      zoom: {
        in: '放大',
        out: '缩小',
        fit: '适配窗口 (缩放到整图可见)',
        fit_label: '适配',
        reset: '重置为 1:1',
      },
      attribute_all_btn: '推测全部源头',
      attribute_all_running: '推测中…',
      attribute_all_hint:
        '一次性对所有 事实 / 反思 / 人设 跑文本相似度反向归因, 画出全部虚线 (Tier C 启发式推断, 非已落盘真因果).',
      attribute_all_done_fmt: (n, nodes, total) =>
        `已为 ${nodes}/${total} 条记忆推测出 ${n} 条来源虚线 (文本相似度). 点选任一节点即可只看它这一条链路.`,
      attribute_all_none: '未找到任何文本相似的来源对话 (该角色可能无对话语料, 或相似度过低).',
      heuristic_hidden_fmt: (n) =>
        `总览已隐藏 ${n} 条启发式来源虚线 (数量过多会看不清且拖慢画面). 点选任一记忆节点, 即可只看它这一条链路的来源.`,
      isolated_show_fmt: (n) => `显示未连线 (${n})`,
      isolated_hide: '隐藏未连线',
      isolated_toggle_hint:
        '默认折叠所有"无任何溯源连线"的节点 (多为孤立事实/对话). 展开后它们会以紧凑网格出现在主图下方, 仅供浏览, 不参与连线.',
      isolated_zone_fmt: (n) =>
        `未连线节点 · ${n} 条 (与任何记忆/对话都无溯源关系, 仅供浏览)`,
      no_session: {
        heading: '先建一个会话',
        body: '左上角"新建会话"并选定角色后, 这里会画出该角色的记忆溯源图.',
      },
      no_character: {
        heading: '先选一个角色',
        body: '在 Setup → Persona 填角色名, 或在 Setup → Import 从真实角色导入, 才能分析其记忆.',
      },
      empty: {
        heading: '该角色暂无记忆',
        body: '当前角色还没有任何事实 / 反思 / 人设 / 对话记录. 跑几次记忆操作或导入一个真实角色后再来.',
      },
      load_failed: '记忆溯源图加载失败',
      lanes: {
        message: '对话',
        recent_memo: 'recent 摘要',
        fact: '事实',
        reflection: '反思',
        persona_entry: '人设',
      },
      node_type: {
        message: '对话',
        recent_memo: 'recent 摘要',
        fact: '事实',
        fact_archived: '事实(归档)',
        reflection: '反思',
        persona_entry: '人设',
        correction: '矛盾待裁决',
      },
      relation: {
        source_fact: '由事实合成',
        promoted_from: '由反思晋升',
        merged_from: '由反思合并',
        compressed_from: '由对话压缩',
        extracted_from: '由对话抽取',
        attributed_from: '反向归因',
        corrects: '修正',
      },
      confidence: {
        persisted: '已落盘 (真因果)',
        captured: '生成时捕获',
        heuristic: '启发式推断',
      },
      legend: {
        heading: '图例',
        solid: '实线 · 已落盘真因果',
        dashed: '虚线 · 启发式推断',
      },
      counts_fmt: (c) =>
        `对话 ${c.messages} · 摘要 ${c.recent_memos} · 事实 ${c.facts}`
        + (c.facts_archived ? ` (+${c.facts_archived} 归档)` : '')
        + ` · 反思 ${c.reflections} · 人设 ${c.persona} · 矛盾 ${c.corrections}`,
      budget_fmt: (shown, total) =>
        `已显示 ${shown} / ${total} 个节点 (其余按节点预算省略)`,
      sources: {
        heading: '数据源',
        time_indexed_db_present: '对话归档 (time_indexed.db): 已加载',
        time_indexed_db_absent: '对话归档 (time_indexed.db): 无 — 预设角色或从未对话; 导入真实角色后才有对话级溯源.',
        time_indexed_db_hint: '提示: 重度角色的对话归档可能超过存档大小上限而不随存档/读档保留, 如需溯源请重新从真实角色导入.',
        events_present: '事件流 (events.ndjson): 已加载',
        events_absent: '事件流 (events.ndjson): 无 — testbench 原生记忆无 evidence 时间线, 变迁史由 status 字段粗粒度重建.',
        trace_present: 'Tier B 侧车 (trace_provenance.json): 已加载',
      },
      warnings_heading: '读取告警',
      detail: {
        heading: '节点详情',
        empty: '点击左侧任意节点查看其内容、来源与影响.',
        field_type: '类型',
        field_status: '状态',
        field_entity: '归属',
        field_created: '创建时间',
        field_content: '内容',
        field_source: '来源',
        field_origin: '对话来源',
        upstream: '上游来源',
        downstream: '下游影响',
        none: '无',
        attribute_btn: '分析来源 (文本相似度)',
        attribute_llm_btn: '分析来源 (LLM 精判)',
        attributing: '分析中…',
        attribute_hint: '反向归因为启发式重建, 画虚线 (Tier C), 非已落盘真因果.',
        attribute_done_fmt: (n, method) =>
          `${method === 'llm' ? 'LLM' : '文本相似度'}归因到 ${n} 条对话 (虚线)`,
        attribute_fallback_fmt: (n, reason) =>
          `LLM 精判失败, 已回退文本相似度归因到 ${n} 条对话 (虚线)。`
          + `这些虚线来自文本相似度而非 LLM 精判。原因: ${reason || '未知'}`,
        attribute_none: '未找到可归因的对话片段.',
        attribute_failed: '反向归因失败',
      },
      origin: {
        time_indexed_db: '对话归档',
        recent_json: 'recent 窗口',
      },
      // 代码线索 (开发者) 子页 (P32). 把机械不变量类记忆分析发现「反推」成主程序
      // 记忆代码的排查方向. 重点: 全文案压低置信预期 —— 这些是弱线索、不是 bug 报告.
      // 措辞纪律 (蓝图 §3 UI-2): 线索卡文本禁用 "bug/缺陷/确认存在" 等确定性断言,
      // 必用 "疑似/线索/值得排查/需人工确认".
      code_leads: {
        title: '代码线索 (开发者)',
        intro: '本页把「机械不变量类」记忆分析发现反推成主程序记忆代码的排查方向, 仅供代码相关人员参考. 普通测试员可忽略本页.',
        reload: '重新分析',
        loading: '分析中…',
        load_failed: '分析失败',
        // UI-1: 页首不可跳过、不可关闭的红色警告 (6 要素).
        notice: {
          head: '务必先读: 这些是排查方向, 不是 bug 报告',
          p1: '① 以下每一条都是「值得排查的方向 / 线索」, 不是已确认的代码缺陷.',
          p2: '② 由记忆数据反推代码问题并不完全可靠, 可能是假阳性 (数据/模型/配置原因, 未必是代码).',
          p3: '③ 每条线索都需要你到主程序代码里人工确认后才能下结论.',
          p4: '④ 本页只读, 绝不会自动修改任何代码; testbench 全程不写主程序.',
          p5: '⑤ 内容质量类问题 (矛盾 / 冗余 / 归因 / 漂移 / 留存) 刻意不在此反推 —— 只看机械不变量.',
          p6: '⑥ 反推方法与局限详见说明文档:',
          link: '代码线索使用说明',
        },
        leads_head: '排查线索 (需人工确认)',
        strength: {
          high: '较强',
          medium: '中等',
          low: '较弱',
          hint: '较强 = 违反几乎必是写入/迁移代码问题; 较弱 = 也可能只是还没跑某道工序, 未必是代码信号. 无论强弱都需人工确认.',
        },
        needs_confirm: '(仍需人工确认)',
        suspect_head: '疑似方向 (主程序模块, 非精确定位)',
        missing_head: '还需哪些运行期证据才能坐实',
        count_fmt: (n) => `命中 ${n} 处`,
        examples_head: '样例 (仅本地查看, 勿截图外传)',
        // 每条不变量的「违反了什么」+「建议排查动作」(祈使句式, 无确定性断言).
        invariant: {
          D1: '有反思没有任何有效来源事实 (游离反思).',
          D2: '有晋升人设标记来自反思, 却没有对应的晋升/合并溯源边.',
          D4: '有反思声明的来源事实已被硬删除 (悬空引用).',
          E2: '有向量与其当前文本的 sha256 不一致 (向量过期未重算).',
          E3: '有向量维度或数值非法 (向量损坏).',
          E4: '混入了不同维度/模型的向量 (多向量空间).',
          'ID-DUP': '同一记忆文件里出现了重复主键 id.',
          'EVT-DUP': 'events.ndjson 里出现了重复 event_id.',
        },
        action: {
          D1: '建议排查: 反思合成时来源事实的绑定逻辑, 或确认该反思本就无源 (非代码原因).',
          D2: '建议排查: 反思晋升/合并写入时 source 与 merged_from 的赋值是否遗漏.',
          D4: '建议排查: 删除事实时是否级联清理了引用它的反思.',
          E2: '建议排查: 记忆文本更新后是否触发了向量重算.',
          E3: '建议排查: 向量写入前的维度/数值校验点.',
          E4: '建议排查: 是否在同一角色里混用了不同的嵌入模型/维度.',
          'ID-DUP': '建议排查: 该记忆容器的主键分配与写入去重逻辑.',
          'EVT-DUP': '建议排查: 事件 append/reconcile 路径是否重复写入了同一 event_id.',
        },
        // UI-5: 排除区 (明示故意不反推).
        excluded: {
          head: '以下类别故意不做代码反推',
          intro: '这些属数据 / 模型 / 配置层问题, 反推到代码不可靠. 请走记忆运营 (在系统概况里查看), 而非改代码.',
          item_fmt: (code, count) => `${code}: ${count} 项`,
        },
        // UI-5: 诚实状态行.
        status: {
          head: '检查状态',
          embedding_ran: '向量检查: 已运行 (E2/E3/E4 已核对).',
          embedding_unavailable: '向量检查: 未检查 (该角色无向量) —— 不代表向量一定没问题.',
          evt_ran: '事件流检查: 已运行 (event_id 唯一性已核对).',
          evt_unavailable: '事件流检查: 未检查 (无 events.ndjson) —— 不代表事件流一定没问题.',
          evt_truncated: '事件流检查: 已部分运行 (文件过大, 仅扫描了前若干行).',
        },
        warnings_head: '数据读取告警',
        // 诚实空态: 无线索 ≠ 代码没问题.
        empty_leads: {
          head: '未发现机械不变量违反',
          body: '当前没有可反推的代码排查线索。注意: 这不代表主程序代码一定没问题, 只代表这几项机械检查没有命中。',
        },
        no_session: {
          heading: '没有活动会话',
          body: '请先在 Setup 新建或选择一个会话。',
        },
        no_character: {
          heading: '未选择角色',
          body: '请先在 Setup → Persona 填写角色名, 或从真实角色导入。',
        },
        empty: {
          heading: '该角色暂无记忆数据',
          body: '先在 Setup 里为角色导入 / 生成记忆, 再回来分析。',
        },
      },
    },
    setup: {
      nav: {
        persona: 'Persona 人设',
        import: 'Import 导入',
        virtual_clock: 'Virtual Clock 虚拟时钟',
        scripts: 'Scripts 对话剧本',
        // 记忆分组 (P07); `memory_group` 是导航侧分组标题, 不对应子页.
        memory_group: '记忆 (Memory)',
        memory_recent: '最近对话',
        memory_facts: '事实',
        memory_reflections: '反思',
        memory_persona: '人设记忆',
      },
      no_session: {
        heading: '先建一个会话',
        body: '左上角"新建会话"后, 本页将允许编辑人设或从真实角色导入.',
      },
      persona: {
        heading: '人设元数据',
        intro: '定义本次会话的身份: 主人名 / 角色名 / 语言 / system_prompt. 修改只落在当前沙盒, 不影响主 App 的真实 characters.json.',
        fields: {
          master_name: '主人名',
          character_name: '角色名',
          language: '语言',
          system_prompt: 'System Prompt',
        },
        placeholder: {
          master_name: '例: Master / 主人',
          character_name: '例: N.E.K.O. / 兰兰',
          system_prompt: '留空可通过 Import 从真实角色拷贝默认 prompt.',
        },
        hint: {
          master_name: '出现在 human 消息前的说话人标签.',
          character_name: '角色专属 memory 子目录名; 变更后 Import 会写到新目录.',
          language: 'zh-CN / en / ja 等 ISO 代码. 作用于后续 Prompt 本地化.',
          system_prompt: '支持 {LANLAN_NAME} / {MASTER_NAME} 占位符, 由 Prompt 合成器在运行期替换. 留空或保留默认文本时, 运行期会自动替换为当前 language 的默认模板 — 下方可预览实际效果.',
        },
        buttons: {
          save: '保存',
          revert: '撤销',
        },
        status: {
          saved: '已保存',
          save_failed: '保存失败',
          loaded: '已载入当前会话 persona',
          no_change: '没有未保存的改动',
        },
        // 实际 system_prompt 预览 (P05 补强, 对照后端 /api/persona/effective_system_prompt).
        preview: {
          heading: '预览实际 system_prompt',
          intro: '按当前表单内容 (未保存也生效) 还原运行时装配结果: 空/默认文本会走对应 language 默认模板, 然后替换 {LANLAN_NAME}/{MASTER_NAME}. 实际 LLM 请求里的 system_prompt 就是此处的 "最终" 文本.',
          refresh_btn: '刷新预览',
          loading: '加载中...',
          load_failed: '加载失败',
          source_label: '来源',
          // 函数叶子 (§3A i18n-fmt-naming): `_fmt` 后缀表明是函数值, caller
          // 必须用 `i18n(key, arg)` 而非 `i18n(key)` 读. 本字典用 `{0}`
          // 占位符模式是训练语料污染 (i18next 风格) — 项目自定义 i18n 不
          // 支持, 显示的是字面量 `{0}`. 2026-04-22 Day 7 验收期用户反馈
          // 顺手修了 page_ui 和此处共 4 处; 统一改为函数值.
          source_default_fmt: (lang) => `默认模板 (language = ${lang})`,
          source_stored: '自定义 (来自已保存 system_prompt)',
          default_warning_fmt: (lang) =>
            `注意: 当前自定义文本被识别为某语言默认模板 — 运行期会被当作"空"并自动换成 language=${lang} 的版本. 若想固化保存, 需要对默认文本做任意修改.`,
          placeholder_warning: '注意: 主人名 / 角色名为空, 预览里保留了 {LANLAN_NAME} / {MASTER_NAME} 占位符, 运行期会按实际填写值替换.',
          resolved_label: '最终 (名字替换后)',
          template_label: '模板原文 (未替换占位符)',
          copy_btn: '复制',
          copy_done: '已复制',
          copy_fail: '复制失败',
          char_count: n => `${n} 字符`,
        },
      },
      import: {
        heading: '导入角色',
        intro: '把一个可用的角色灌进当前沙盒. 有两种来源: (1) testbench 自带的内置预设, 一键即可用, 也可用来清零本会话; (2) 从主 App 文档目录里的真实角色复制. 两种都**只写沙盒**, 不会回写主 App.',
        // 内置预设 (git 追踪的 seed 数据).
        builtin: {
          heading: '内置预设 (testbench 自带)',
          intro: '仓库里带的最小完整示例角色. 适合: 新会话快速起点; 把乱七八糟的沙盒一键覆盖回已知状态 (characters.json + persona.json + facts.json + recent.json 都会被重写). 多次点击会重复覆盖, 不会累积.',
          empty: '暂无内置预设 (tests/testbench/presets/ 里没有任何合法子目录).',
          button_apply: '一键载入',
          button_applying: '载入中…',
          apply_ok: (name, count) => `已载入预设 ${name} (${count} 个文件)`,
          apply_failed: '载入预设失败',
        },
        // 真实角色导入 (从主 App characters.json).
        real: {
          heading: '从主 App 真实角色导入',
          intro: '列出 ~/Documents/N.E.K.O/config/characters.json 里的所有猫娘. 只读真实目录, 复制到沙盒.',
        },
        // zip 人格档案导入 (用户自带压缩包, 走和 preset / 真实角色同一套写管线).
        archive: {
          heading: '从 zip 人格档案导入',
          intro: '选一个 .zip 压缩包导入到当前会话. 期望布局: 根目录有 characters.json + memory/<角色名>/ (与内置预设 / 主程序数据目录同构). 只解压到沙盒临时目录, 不写真实目录. 若压缩包内含多个角色, 在下方填要导入的角色名.',
          name_placeholder: '角色名 (多角色时必填, 单角色可留空)',
          button_pick: '选择 zip 导入',
          button_reading: '读取文件中…',
          button_importing: '导入中…',
          import_ok: (name, count) => `已从压缩包导入 ${name} (${count} 个文件)`,
          import_failed: '压缩包导入失败',
          // 客户端预校验 (上传前就拦掉明显不合法的选择).
          bad_ext: (name) => `"${name}" 不是 .zip 文件. 请选择 zip 格式的人格档案压缩包.`,
          empty_file: '选择的文件是空的 (0 字节), 无法作为人格档案导入.',
          too_large_client: (mb) => `文件超过 ${mb} MiB 上限, 多半选错了文件 (人格档案通常远小于此). 已取消导入.`,
          read_failed: '读取本地文件失败, 请重试或换一个文件.',
          // 失败分类标题 (内联状态区主标题, 后端 message 作详情副行).
          err_format: '文件格式不合法: 无法解析为角色档案',
          err_toolarge: '压缩包过大',
          err_no_session: '请先在顶栏新建会话, 再导入人格档案.',
          err_ambiguous: '压缩包内含多个角色',
          err_ambiguous_hint: '请在上方输入框填写要导入的角色名后, 重新选择文件导入.',
          err_generic: '导入出错',
        },
        no_session: '需要先建会话才能读取真实角色目录.',
        no_real: '主 App 文档目录下没找到 characters.json, 或暂无角色.',
        // P24 Day 8 §12.4.A: dev_note L17 默认角色数据显示 bug 的诊断入口.
        // Backend 新增 `skipped_entries` 字段列出被过滤掉的 raw 条目 + 原因,
        // 前端展示成独立块让用户直接自查本地 characters.json 的异常.
        skipped_heading_fmt: (n) => `有 ${n} 个 characters.json 里的条目被过滤掉, 没显示在上面的列表里`,
        skipped_hint: '常见原因: 该条目不是对象格式 (手动编辑过 / 误存成数组或字符串) / 主程序最近改了 schema. 请打开上面"数据源"里的 config_dir 路径看实际文件, 检查下面列出的角色.',
        skipped_unknown_reason: '(未知原因)',
        // Windows CFA (Controlled Folder Access) 回退警告. 用户在 Documents/
        // 下改了 characters.json 但主程序用的是 AppData\Local 下的另一份 —
        // 2026-04-22 dev_note L17 根因.
        cfa_fallback: {
          heading: '⚠ 主程序检测到 Documents 目录不可写, 已降级到 AppData\\Local',
          body: 'Windows 受控文件夹访问 (CFA) 或类似安全策略阻止了对 Documents 目录的写入. '
              + '主程序与 testbench 都从下面"实际生效"的路径读写配置和角色数据. '
              + '如果你在"仅可读"的 Documents 那份文件里改了 characters.json 或其它 config, '
              + '主程序根本不会看到那些改动, 测试场景里就会观察到"改了角色但列表没更新"的现象.',
          active_label: '✅ 主程序实际读写的 characters.json',
          readable_label: '🚫 仅可读的 Documents 副本 (改这个无效)',
          hint: '修复方式二选一: (a) 直接编辑上方"实际读写"路径下的 characters.json; '
              + '或 (b) 去 Windows 设置关掉 Documents 的 CFA / 反勒索防护让 Documents 可写, '
              + '然后重启主程序 + testbench 服务.',
        },
        columns: {
          name: '角色名',
          status: '状态',
          files: 'memory 文件',
          action: '操作',
        },
        badge_current: '当前',
        badge_current_hint: '你当前正在使用的角色 (characters.json 的"当前猫娘").',
        badge_has_prompt: 'prompt ✓',
        badge_has_prompt_hint: '该角色在 characters.json 里配置了自定义 system_prompt. 导入会把这段 prompt 带到 testbench 会话里作为 persona.system_prompt.',
        badge_no_prompt: 'prompt ✗',
        badge_no_prompt_hint: '该角色没有自定义 system_prompt — characters.json 里该字段缺失或为空. 导入后人设 prompt 会是空的, 需要你自己在 Setup → Persona 里填写.',
        badge_no_memdir: '无 memory 目录',
        badge_no_memdir_hint: '主程序 memory/store/ 下没有该角色的子目录, 也就是这个角色没有任何历史对话 / facts / reflections / persona 文件可以导入. 只会导入 characters.json 的基本信息 (名字、system_prompt 等).',
        button_import: '导入到当前会话',
        button_importing: '导入中…',
        confirm_overwrite: name => `"${name}" 已经在当前沙盒存在同名 memory 目录. 继续将覆盖文件; 确认?`,
        import_ok: (name, count) => `已导入 ${name} (${count} 个文件)`,
        import_failed: '导入失败',
        // P31 角色一键导出 (与"导入"镜像): 忠实全量打包该本地角色记忆目录.
        button_export: '导出',
        button_exporting: '导出中…',
        export_hint: '把该角色的完整记忆目录 (含隐私原始数据) 打包为 zip, 仅供本地备份 / 迁移 / 可信分享. 与脱敏的记忆分析导出不同, 此处不做任何脱敏.',
        export_ok: (name) => `已导出 ${name}`,
        export_failed: '导出失败',
        export_no_session: '没有活动会话, 或本地 characters.json 里没有该角色, 无法导出',
        export_too_large: '该角色记忆目录过大, 超过导出上限',
        source_paths_label: '数据源',
      },
      // P12.5: Setup → Scripts 子页 (对话剧本模板编辑器).
      scripts: {
        heading: '对话剧本 (Dialog Scripts)',
        intro: '阅览 / 复制 / 编辑 / 新建 `dialog_templates/*.json`. 剧本里的 user turn 是测试输入, assistant turn 的 `expected` 字段会在 Chat 里跑脚本时自动写入消息的"参考回复"供对照评分 (Comparative Judger). 评分 prompt / 评分维度不在这里, 归 Evaluation → Schemas 子页.',
        buttons: {
          refresh_list: '刷新列表',
          new_blank: '+ 新建空白模板',
          save: '保存',
          reload: '撤销',
          export: '导出 JSON',
        },
        list: {
          count_fmt: n => `共 ${n} 条`,
          user_group: n => `用户模板 (${n})`,
          builtin_group: n => `内置模板 (${n}, 只读)`,
          badge_builtin: '内置',
          badge_overriding: '覆盖 builtin',
          duplicate: '复制为可编辑',
          delete: '删除',
          turns_fmt: n => `${n} 轮`,
          empty: '还没有任何模板. 点 [+ 新建空白模板] 或把 JSON 文件放到 testbench_data/dialog_templates/ 后点 [刷新列表].',
        },
        editor: {
          loading: '加载中…',
          empty_title: '从左侧挑一个剧本开始编辑',
          empty_hint: '内置模板只读, 点 [复制为可编辑] 得到 user 副本再改. 也可以直接 [+ 新建空白模板].',
          untitled: '(未命名)',
          readonly_badge: '只读 · 内置',
          dirty_badge: '未保存',
          readonly_hint: '这是内置模板, 直接编辑会被 git 覆盖. 请先点 [复制为可编辑] 创建 user 副本.',
          basic_heading: '基本信息',
          bootstrap_heading: 'Bootstrap (初始虚拟时间)',
          bootstrap_intro: '加载此剧本时, 若会话尚未产生任何消息, 虚拟时钟会被重置到这里. 已有消息时忽略 (不会硬覆盖产生负时间差).',
          turns_heading: n => `对话回合 (${n} 轮)`,
          turns_empty: '还没有 turn. 点下方 [+ 添加 user turn] 开始写.',
          role_user: '用户 (user)',
          role_assistant: 'AI (assistant)',
          errors_heading: '顶层字段错误',
          buttons: {
            add_user: '+ 添加 user turn',
            add_assistant: '+ 添加 assistant 参考回复',
          },
          fields: {
            name: '模板 name (= 文件名)',
            description: '描述',
            user_persona_hint: '假想用户画像提示 (user_persona_hint)',
            virtual_now: '虚拟起始时间 (ISO, 可选)',
            last_gap_minutes: '距上一次对话分钟数 (可选)',
            user_content: 'user 消息内容',
            assistant_expected: '参考回复 (expected)',
            time_advance: '推进时长 (advance)',
            time_at: '绝对时间 (at)',
          },
          hints: {
            name: '字母 / 数字 / 下划线 / 短横线, 首字非符号, ≤64 字. 改名会走 Save As.',
            name_readonly: '内置模板 name 不可改. 复制为 user 副本后可改.',
            user_persona_hint: '生成假想用户消息时会注入这段话, 帮 LLM 进入角色.',
            virtual_now: '例: 2025-01-01T09:00',
            last_gap_minutes: '整数, 例: 10',
            assistant_expected: '这条 expected 会挂到上一条 user turn 对应 AI 回复的 reference_content. 连续多条 assistant 的 expected 会用 `\\n---\\n` 合并.',
            time_advance: '例: 5m / 1h30m / 2d. 与 at 只能二选一, 未填走默认.',
            time_at: '例: 2025-01-01T09:05. 与 advance 只能二选一.',
          },
          placeholders: {
            user_content: '例: 下午好. 我又被组长当众骂了...',
            assistant_expected: '例: ......当着全组的面啊, 那肯定难受得紧. 本喵听着都替你胃疼. 先别急着替他说话...',
          },
        },
        prompt: {
          new_name: '新模板的 name (= 文件名):',
          duplicate_name: src => `把内置模板 "${src}" 复制为 user 副本. 新 name:`,
          confirm_delete: name => `确定要删除 user 模板 "${name}" 吗? 此操作不可恢复 (但内置版本若存在会重新生效).`,
        },
        toast: {
          list_failed: '加载模板列表失败',
          load_failed: '加载模板详情失败',
          name_taken: name => `user 模板 "${name}" 已存在. 请换个 name.`,
          duplicate_failed: '复制失败',
          duplicated: name => `已复制到 "${name}"`,
          delete_failed: '删除失败',
          deleted: name => `已删除 "${name}"`,
          resurfaces_builtin: name => `内置版本的 "${name}" 重新生效`,
          save_failed: '保存失败',
          save_errors: n => `保存被拒: ${n} 条字段错误待修正`,
          saved: name => `已保存 "${name}"`,
          now_overriding_builtin: name => `当前 user 模板 "${name}" 覆盖了同名 builtin — 加载时优先用 user 版本`,
          rename_left_old: old => `改名已保存新版本, 但删除旧 user 模板 "${old}" 时失败, 请手动 [刷新列表] 检查.`,
        },
      },
      memory: {
        // 无会话 / 无角色 时的空状态文案.
        no_session: {
          heading: '先建一个会话',
          body: '左上角"新建会话"后, 这里会打开本会话沙盒里的记忆文件 (recent / facts / reflections / persona).',
        },
        no_character: {
          heading: '先选一个角色',
          body: '请去 Setup → Persona 填写\u300c角色档案名\u300d, 或去 Setup → Import 从真实角色导入, 再回来编辑记忆.',
        },
        // 公共工具条文案, 4 个子页都会用到.
        editor: {
          recent: {
            heading: 'Recent · 最近对话 (recent.json)',
            intro: 'LangChain 风格的消息数组 (type=human/ai/system, data.content=字符串). 这是未压缩的原始对话, Prompt 装配会取末尾若干条.',
          },
          facts: {
            heading: 'Facts · 事实池 (facts.json)',
            intro: '压缩提炼出的事实列表. 每条含 id / text / importance / entity (master|neko|relationship) / tags / hash / created_at / absorbed.',
          },
          reflections: {
            heading: 'Reflections · 反思 (reflections.json)',
            intro: 'pending / confirmed / denied / promoted 四态. status=promoted 表示已晋升为 persona fact. 手动编辑时注意 id 不要重复.',
          },
          persona: {
            heading: 'Persona · 人设记忆 (persona.json)',
            intro: '顶层按 entity 分节 (master / neko / relationship / 自定义), 每节是 { "facts": [...] }. 注意: 真实 PersonaManager 首次加载还会自动合并角色卡片, 这里看到的是磁盘上的原始状态.',
          },
          path_label: '文件路径',
          not_exists_badge: '文件尚未生成 (保存后创建)',
          exists_badge: '已存在',
          count_list: count => `共 ${count} 条`,
          count_dict: count => `共 ${count} 个 entity`,
          valid: 'JSON 合法',
          invalid: prefix => `JSON 解析失败: ${prefix}`,
          dirty_badge: '未保存',
          saving: '保存中...',
          saved: '已保存',
          reloading: '重新加载中...',
          reloaded: '已重新加载',
          format_done: '已重新格式化',
          format_failed: 'JSON 不合法, 无法格式化',
          buttons: {
            save: '保存',
            reload: '从磁盘重新加载',
            format: '重排格式',
            revert: '还原到上次加载',
          },
          confirm_overwrite: '当前有未保存修改, 重新加载会丢弃. 确认吗?',
          // 结构化 / Raw 双视图 tab.
          tabs: {
            structured: '结构化',
            raw: 'Raw JSON',
          },
          tab_switch_blocked: brief => `当前 Raw 文本不是合法 JSON, 无法切到结构化视图: ${brief}`,
          // 结构化视图通用文案.
          advanced_toggle: '高级字段',
          add_entity: '添加实体',
          add_persona_fact: '添加事实',
          add_fact: '添加事实条目',
          add_reflection: '添加反思条目',
          add_message: '添加消息',
          prompt_entity_name: '新实体名 (如 master / neko / 自定义)',
          entity_exists: name => `实体 "${name}" 已存在`,
          delete_item: '删除',
          delete_entity: '删除实体',
          delete_entity_confirm: name => `删除实体 "${name}" 及其下所有条目? 点保存前都可用"还原"回退.`,
          count_items: n => `${n} 条`,
          empty_persona_hint: '还没有任何实体. 人设按实体 (master / neko / 自定义角色) 分组, 每个实体下挂一串事实. 点"＋添加实体"开始.',
          empty_facts_hint: '此实体下暂无事实.',
          empty_list_hint: '暂无条目. 点上方"＋"按钮添加.',
          recent_warn: '此文件是运行期对话日志 (LangChain dump), 一般由 Chat 工作区自动写入; 手动编辑仅用于制造异常输入来测 pipeline 容错.',
          complex_content_hint: '此消息 content 是 multimodal 分段结构但不含文本段 (如仅图片/音频), 不能直接用文本框编辑. 如需修改请切到 Raw JSON 视图.',
          multimodal_extras: count => `另含 ${count} 个非文本分段 (如图片/音频), 编辑不影响它们; 如需改切 Raw`,
          multimodal_multi_text: count => `此消息共有 ${count} 个文本分段, 上方只编辑首段; 改其它段请切 Raw`,
          textarea: {
            expand: '展开全文 ▾',
            collapse: '折叠 ▴',
          },
          // 字段标签.
          field: {
            text: '文本',
            entity: '实体',
            source: '来源',
            protected: 'protected (永久, 不可抑制)',
            suppress: 'suppress (临时抑制)',
            suppressed_at: 'suppress 开始时间',
            source_id: '上游 ID',
            recent_mentions: '最近提及时间戳',
            id: 'ID',
            importance: '重要度',
            tags: '标签 (逗号分隔)',
            hash: 'hash',
            created_at: '创建时间',
            absorbed: 'absorbed (已被反思吸收)',
            status: '状态',
            source_fact_ids: '源事实 IDs (逗号分隔)',
            feedback: '反馈',
            next_eligible_at: '下次触发时间',
            type: '消息类型',
            content: '内容',
            extra_data: '其它 data 字段 (JSON)',
          },
          // recent 消息的 LangChain type 枚举 label. 括号里是给测试人员看的中文注释,
          // 方便一眼知道 human/ai/system 分别对应"用户/助手/系统".
          message_type: {
            human: 'human (用户)',
            ai: 'ai (助手)',
            system: 'system (系统)',
          },
        },
        // P10 触发面板: 在 editor 下方让测试人员手动跑记忆合成操作, 查看
        // dry-run 预览, 确认后再写入磁盘. 全部 LLM 调用走本会话 memory 组.
        trigger: {
          section_title: '手动触发记忆合成',
          reloaded_after_commit: '已应用并重新加载磁盘',
          params_title: op => `参数配置 · ${op}`,
          preview_title: op => `预览 · ${op}`,
          failed_title: op => `触发失败 · ${op}`,
          close: '关闭',
          no_params: '此操作无可配置参数, 点\u300c执行\u300d直接跑.',
          run_button: '执行 (Dry-run)',
          cancel: '取消',
          running: '正在调用模型, 请稍候...',
          accept: '应用到磁盘',
          reject: '丢弃此次预览',
          committing: '写入中...',
          committed: op => `${op} 已应用到磁盘`,
          drop_item: '从本次提交中剔除',
          // P25 r7 (2026-04-23): [预览 prompt] 按钮 — 不调 LLM, 只看
          // "这次触发会发什么 prompt 给记忆 LLM".
          preview_prompt: {
            label: '预览 prompt',
            tooltip: '不调用 LLM, 只查看这次触发会发给记忆合成 LLM 的 prompt',
            params_title: (op) => `预览参数 · ${op}`,
            run_button: '预览 prompt',
            failed: '预览 prompt 失败',
            modal_title: (op) => `预览 prompt · ${op}`,
            intro: '以下是 Dry-run 真正触发时会发给记忆合成 LLM 的 prompt. 本次预览没有真正调用 LLM, 也不会写入 session.memory_previews / session.last_llm_wire.',
            meta: {
              op: '操作',
              note: '备注',
            },
          },
          recent: {
            intro: '把 recent.json **最旧** 的若干条消息压成一条 system 摘要 (保留最近的对话原样), 节省 context. 只影响 recent.json, 不动 facts/reflections.',
            compress: { label: '压缩最旧消息' },
            params: {
              tail_count: '压缩条数',
              tail_count_ph: '默认按历史长度阈值自动算',
              tail_count_help: '要送进 LLM 压缩的 **最旧** 消息数 (从开头算起). 留空=按 max_history_length 自动推导.',
              detailed: '详细摘要',
              detailed_help: '勾选后生成\u300c详细版\u300d摘要 (篇幅更长, 保留更多细节), 否则用简洁版.',
            },
            stats: {
              total_before: '压缩前条数',
              tail_count: '本次压缩',
              kept_count: '保留原样',
              total_after: '压缩后条数',
            },
            preview: {
              memo: '注入 recent 开头的 system 摘要消息',
              memo_help: '写入时会替换为单条 system 消息挂在保留的最近消息之前. 可在此直接修改.',
              raw_summary: 'LLM 原始摘要输出',
              raw_summary_help: '仅参考. 实际写入的是上面的 system 消息文本.',
            },
          },
          facts: {
            intro: '从本会话消息 (或 recent.json) 中抽取可复用事实. 可逐条剔除/微调后再写入 facts.json.',
            extract: { label: '从对话抽事实' },
            params: {
              source: '来源',
              source_session: '本会话 Messages (Chat 页)',
              source_recent: '磁盘上的 recent.json',
              source_help: '默认用 Chat 工作区当前对话. 选 recent.json 则读磁盘.',
              min_importance: '最小重要度',
              min_importance_help: '低于此值的事实会被丢弃 (0-10, 默认 5).',
            },
            stats: {
              message_count: '扫描消息',
              extracted_count: '本次抽出',
              total_existing: '原有事实数',
            },
            fields: {
              text: '事实正文',
              entity: '实体',
              importance: '重要度 (0-10)',
              tags: '标签 (逗号分隔)',
            },
            preview: {
              empty: '模型没有抽到任何新事实 (可能已全部重复或未达最小重要度).',
            },
          },
          reflections: {
            intro: '把多条未吸收的事实合成为一条反思. 合成后相关事实会标记 absorbed, 反思进入 pending 等待裁决.',
          },
          reflect: {
            label: '合成反思',
            params: {
              min_facts: '最少事实数',
              min_facts_help: '未吸收事实少于此值时跳过合成. 默认 5.',
            },
            stats: {
              unabsorbed: '可用未吸收事实',
              source_count: '引用事实数',
            },
            fields: {
              text: '反思正文',
              entity: '归属实体',
              entity_master: 'master (主人)',
              entity_neko: 'neko (自己)',
              entity_relationship: 'relationship (关系)',
            },
            source_facts_title: n => `引用的事实 (${n})`,
          },
          persona: {
            intro: '手动添加一条 persona 事实. 若与现有 persona 或角色卡冲突, 会进入矛盾队列或直接拒绝.',
            add_fact: { label: '添加 persona 事实' },
            resolve_corrections: { label: '裁决矛盾队列' },
            params: {
              text: '事实内容',
              entity: '归属实体',
            },
            code: {
              added: '将直接写入',
              rejected_card: '与角色卡冲突 · 将被永久拒绝',
              queued: '与现有条目冲突 · 进入矛盾队列',
            },
            preview: {
              code_label: '预期结果',
              existing_count: '该实体现有条目',
              conflicting: '冲突条目原文',
              conflicting_help: '仅展示, 裁决将在矛盾队列阶段完成.',
              text: '即将写入的正文',
              text_help: '可在此微调文案 (不会触发重新跑矛盾检测).',
              entity: '归属实体',
              section_preview_title: n => `写入后该 entity 内容 (${n} 条)`,
            },
          },
          resolve: {
            stats: {
              queue_size: '队列规模',
              action_count: 'LLM 建议动作数',
            },
            empty: '矛盾队列为空, 没有需要裁决的条目.',
            fields: {
              old_text: '原条目',
              new_text: '待评估新条目',
              action: '建议动作',
              merged_text: '合并后文本',
              merged_text_help: 'action=replace/keep_new/keep_both 时会用到, 可手动改写.',
            },
            action: {
              replace: 'replace · 新替旧',
              keep_new: 'keep_new · 丢弃旧',
              keep_old: 'keep_old · 丢弃新',
              keep_both: 'keep_both · 两条都留',
            },
          },
        },
      },
      virtual_clock: {
        heading: '虚拟时钟 (滚动游标)',
        intro: '测试用的时间源. Prompt 装配和记忆计算使用的"当前时间"全部取自这个游标, 不读系统时钟. 游标不会自己前进, 需要通过下方"实时游标"或"每轮暂存"手动推进.',
        no_session: {
          heading: '先建一个会话',
          body: '左上角"新建会话"后, 才能配置会话级时钟.',
        },
        live: {
          heading: '实时游标 (当前时间)',
          intro: '本次会话的"虚拟当前时间". 未设定时回退到系统真实时间, 每秒自动刷新; 一旦设定为某个具体时间就会冻结在那里, 只能手动推进.',
          now_label: '当前时间',
          real_time_badge: '跟随系统时间',
          virtual_badge: '虚拟时间',
          absolute_label: '设为指定时间',
          advance_label: '按时长推进',
          set_btn: '设定',
          release_btn: '回到系统时间',
          advance_btn: '推进',
          preset_plus_5m: '+5 分钟',
          preset_plus_1h: '+1 小时',
          preset_plus_1d: '+1 天',
          delta_hint: '支持 "1h30m" / "45s" / "2d 4h" 这类写法, 也接受纯数字 (如 "120" 视作 120 秒); 以负号开头 (如 "-1h") 即回退.',
        },
        bootstrap: {
          heading: '会话起点 (Bootstrap)',
          intro: '会话创建时的"虚拟当前时间", 以及"距离上次对话过去了多久". 这两个值只在**首条消息发送前**被 Prompt 用到; 一旦有了首条消息, 后续 gap 改以最后一条消息的时间戳为准, 本段数据就不再影响新轮次.',
          bootstrap_at_label: '起点时间',
          initial_gap_label: '距上次对话',
          sync_cursor_label: '同时把"实时游标"也设到起点时间 (常见用法)',
          set_btn: '保存起点',
          clear_bootstrap_btn: '清除起点时间',
          clear_gap_btn: '清除距上次对话',
          hint: '"距上次对话"支持 "1h30m" / "3600s" / 纯秒数等写法, 表示"上次聊天发生在起点时间之前多久".',
        },
        per_turn_default: {
          heading: '每轮默认推进',
          intro: '自动对话 / 脚本对话 / 手动 Composer 每发送一轮后, 游标默认往前走的时长. 单轮在下面"每轮暂存"里显式设定的值会覆盖本项.',
          value_label: '默认 +',
          set_btn: '保存',
          clear_btn: '清空',
          hint: '支持 "1h30m" / "45s" / 纯数字 (秒) 等写法; 留空保存 = 清除, 等同"不自动推进".',
          current_label: '当前默认',
          unset_value: '不自动推进 (每轮游标保持不动)',
        },
        pending: {
          heading: '下一轮暂存 (Pending)',
          intro: '临时声明"下一次发送前, 把游标推到某个时间". 在下一次发送消息时会被使用一次, 使用完立即清空, 不影响后续轮次. 如果同时设了"按时长"和"设为指定时间", 以"指定时间"为准.',
          none_label: '当前没有暂存 (下一轮按"每轮默认推进"执行)',
          pending_delta_label: '下一轮按时长推进',
          pending_abs_label: '下一轮设为指定时间',
          delta_input_label: '按时长',
          abs_input_label: '指定时间',
          stage_delta_btn: '暂存时长',
          stage_abs_btn: '暂存时间',
          clear_btn: '清空暂存',
        },
        reset: {
          heading: '重置时钟',
          intro: '一键清空"实时游标 / 会话起点 / 每轮默认推进 / 下一轮暂存", 回到"跟随系统时间 + 无起点"的裸态. 不会删除任何消息或记忆.',
          reset_btn: '重置时钟',
          confirm: '确定要重置虚拟时钟吗? (不影响消息和记忆)',
        },
        status: {
          saved: '已更新',
          save_failed: '更新失败',
          cleared: '已清除',
          invalid_duration: '无法解析时长, 请检查格式',
          invalid_datetime: '无法解析时间, 请检查格式',
        },
      },
    },
    evaluation: {
      nav: {
        schemas: 'Schemas 评分模板',
        run: 'Run 评分运行',
        results: 'Results 评分结果',
        aggregate: 'Aggregate 汇总',
      },
      run: {
        heading: 'Run 评分运行',
        intro: '选一个 Scoring Schema, 挑要评分的目标 (整段对话 / 某几条 AI 回复), 可选择覆盖 judge 模型参数, 然后 [运行评分]. 结果会立即出现在下方并落到 session.eval_results (Results 子页读取).',
        no_session: {
          heading: '当前没有活动 session',
          body: '请先去 Home 或顶栏 [+ 新会话] 创建一个 session, 再来跑评分.',
        },
        fields: {
          schema: 'Scoring Schema',
          schema_hint: '从这里选将用于评分的模板; 决定了评分维度 / 模式 / 是否需要 reference.',
          schema_summary: '模板摘要',
          target: '评分目标',
          reference: '参考回复 B (Comparative 模式必填)',
          reference_hint: 'comparative schema 会把目标 AI 回复 (A) 和这段文本 (B) 做 pairwise 对比. 可以直接粘贴, 也可以挑一条已经带 reference_content 的消息 (通常来自 Script 脚本跑完落盘).',
          override_heading: '高级 · Judge 模型覆盖 (可选)',
          override_hint: '留空则沿用 Settings → Models → judge 组的配置. 只填你想临时改的字段, 其余回落到 session 默认.',
          match_main_chat: '评委看到和主对话模型一样的 system prompt',
          match_main_chat_hint: '默认只把 persona.system_prompt 发给评委. 勾选后会调 build_prompt_bundle 组装出主 chat 模型实际看到的完整 system (含人设模板 / chat gap / recent_history / 节日等), 让评委在同一条"上下文"下打分. 主要用于排查"同一条对话为什么评分和体感不一致": 打开后对比评分变化, 通常能看出是 persona 本身的问题还是 gap/memory 的问题.',
        },
        picker: {
          no_schemas: '(没有 schema, 先去 Schemas 子页新建)',
        },
        badges: {
          dims_fmt: n => `${n} 维度`,
          has_pass_rule: '有 pass_rule',
        },
        target: {
          scope_messages: '按消息挑选',
          scope_conversation: '整段对话',
          scope_conversation_hint: '把 session 里所有消息当一整段输入, 跑一次 judger (即便 schema 是单条粒度, 这条等价于"轮询每条 assistant 消息").',
          conversation_forced: '当前 schema 是整段粒度 (conversation), 只能按整段评分. 挑消息选项在整段 schema 下不可用.',
          select_all: '全选',
          clear_all: '清空',
          refresh: '刷新消息',
          refresh_hint: '重新从服务端拉取当前会话的消息列表 (Chat 清空 / 新对话后这里可能还停在旧数据).',
          refresh_ok: n => `已刷新, 当前 ${n} 条 assistant 回复可评分.`,
          selection_fmt: (sel, total) => `已选 ${sel}/${total} 条 assistant 回复`,
          no_assistant_messages: '当前会话还没有 AI 回复可评分. 先去 Chat 子页发几条消息.',
          empty_preview: '(消息内容为空)',
          has_reference: '附参考',
        },
        reference: {
          mode_inline: '内联文本 (1:N 同一份 B)',
          mode_msg_ref: '每条消息自带参考 (1:1 自动配对)',
          inline_placeholder: '在此粘贴参考回复 B...',
          conversation_unsupported: '当前版本 UI 暂不支持"整段对比" (comparative + 整段 scope / 或 conversation 粒度). 这类 schema 要走 reference_conversation 字段 (结构化轨迹), 请直接调 POST /api/judge/run; 或把 scope 切回 [按消息挑选] 走 1:N pairwise.',
          disabled_placeholder: '此组合下参考输入已禁用 (切换 scope 至 "按消息挑选" 可恢复).',
          pairing_none: '先在上面挑至少一条 assistant 回复, 这里的参考 B 才有对比对象.',
          pairing_single: '将把这份参考 B 与选中的那 1 条 assistant 回复 (A) 做 pairwise 对比.',
          pairing_multi: n => `注意: 选了 ${n} 条消息, 同一份参考 B 会与每条 A 分别对比 (1:N). 如需为每条消息单独指定 B, 请切到 [每条消息自带参考] 模式, 或分 ${n} 次运行. 多条内联示例见下方 [插入模板].`,
          // 手测反馈: pairing_multi 一句话概括了"是什么", 但没告诉新用户"具体
          // 怎么操作". 多条 A + 单份 B (1:N) 是 testbench 最容易误用的组合 —
          // 很多人以为点 [插入模板] 后每个分段的注释会被 judger 分别解析给对
          // 应的 A, 其实不会: judger 只看到一整段 B 字符串, # 开头的行也一并
          // 塞进 prompt. 所以下面这个步骤条摆明四件事: (1) B 要写成"能同时
          // 评所有 A 的通用参考"; (2) 模板按钮纯粹帮你排版, 不切分语义;
          // (3) 如果真需要 per-A 独立参考, 走 msg_ref 或分次运行; (4) 注释
          // 行会原样进 prompt, 不想污染 prompt 的可以删干净.
          inline_multi_howto_heading: n => `怎么写这份 B (当前选了 ${n} 条 A, 1:N 场景)`,
          inline_multi_howto: [
            '把文本框当一份"通用参考"来写 — 它会被原样作为 B, 与每一条 A 分别做 pairwise 对比, judger 并不会按条切分.',
            '点 [插入模板] 只是帮你排版: 生成带"# [i/N] 消息id — 预览"注释的分段骨架, 让你一眼知道自己在为哪几条 A 写. 注释本身会和正文一起塞进 judger prompt.',
            '如果你想让每条 A 用各自不同的 B, 切到 [每条消息自带参考] 模式 (走消息自身的 reference_content 字段, 1:1 配对); 如果连"每条都要临时改 B"也要, 就按消息分多次运行.',
            '写完后按 [运行评分]; 会并发跑 N 次 judger, 每次 (A_i, 同一份 B) 独立产出一条 EvalResult, 在结果区和 Results 子页都按 A_i 分卡片展示.',
          ],
          pairing_msg_ref_single: '每条消息自带参考: 用选中那 1 条 A 自身的 reference_content 字段作为 B 进行 pairwise 对比.',
          pairing_msg_ref_multi: n => `每条消息自带参考: ${n} 条 A 各自用自身 reference_content 作为 B (1:1 自动配对, 一次批处理产出 ${n} 条独立结果).`,
          pairing_conv_scope: '整段 scope 下 judger 会把全部消息作为一整段输入, 这份参考 B 会被当作 "整段的 gold 版本" 配进 prompt.',
          per_target_no_selection: '先在上面挑至少一条 assistant 回复, 才能为它们分别取 reference_content.',
          per_target_intro_fmt: (ok, total) => `共选中 ${total} 条 A, 其中 ${ok} 条带有自身的 reference_content 可作为 B:`,
          per_target_missing_item: '(这条消息没有 reference_content, 运行时会报 MissingReference)',
          per_target_partial_warn_fmt: (missing, total) => `其中 ${missing}/${total} 条没有 reference_content, 会在结果里以 MissingReference 错误卡片出现; 其余带参考的会正常出评分. 批量仍可运行.`,
          tpl_insert: '插入模板 (按选中消息分段)',
          tpl_clear: '清空参考',
          tpl_clear_confirm: '清空当前已填写的参考文本?',
          tpl_header: n => `# 共有 ${n} 条目标消息, 同一份参考 B 会与每条 A 分别对比.\n# 下面分段列出选中消息, 你可以在每段后面写对应的参考要点,\n# 也可以只在最顶上写一段通用参考 (1:N 语义下, LLM 会把整段都当 B).`,
        },
        override: {
          use_session: '(用 session 默认)',
          provider: 'provider',
          base_url: 'base_url',
          model: 'model',
          api_key: 'api_key',
          api_key_hint: '只在你不想用 Settings 里保存的 key 时才填; 一次性覆盖.',
          temperature: 'temperature',
          max_tokens: 'max_tokens',
          timeout: 'timeout (秒)',
          timeout_hint: 'LLM 单次调用超时. 默认 90s.',
        },
        button: {
          run: '运行评分',
          running: '运行中…',
          running_hint: 'LLM 单次调用通常 5-15 秒; batch 可能要一分钟以上, 请耐心等.',
        },
        // P25 r7 (2026-04-23): [预览 prompt] 按钮位于 [运行评分] 旁边, 调
        // /api/judge/run_prompt_preview 不真调 LLM, 只展示本次会发给评
        // 委 LLM 的 prompt. Chat 页 Preview Panel 现在专注对话 AI 的 wire,
        // judge.llm 的 wire 统一通过这里看.
        preview_prompt: {
          label: '预览 prompt',
          tooltip: '不调用 LLM, 展示本次运行会发给评委 LLM 的 prompt',
          failed: '预览 prompt 失败',
          no_previews: '没有可预览的 prompt (可能所有 target 都被 skip)',
          modal_title: (schema_id) => `预览 judge prompt · ${schema_id}`,
          intro: (count) =>
            `以下为 ${count} 次 judge 调用会发给评委 LLM 的 prompt 合并视图. 各 target 之间用 system 行标签 "── preview #k ──" 分隔. 本次预览没有真正调用 LLM, 也未写 session.eval_results / last_llm_wire.`,
          meta: {
            schema: 'schema',
            mode: 'mode',
            granularity: 'granularity',
            target_count: 'target 数',
          },
          skipped_fmt: (target, err) =>
            `skip target=${target}: ${err}`,
        },
        disabled: {
          no_schema: '先选一个 Scoring Schema.',
          no_message: '先挑至少一条 assistant 回复.',
          no_ref_inline: '比较模式下需要填 [参考回复 B] (内联文本).',
          no_ref_per_target: '选中的消息都没有 reference_content 字段; 要么切回 [内联文本] 手写 B, 要么去 Script 子页跑一遍让脚本落盘参考.',
          conv_comparative_unsupported: '当前 schema 是 comparative + conversation, 当前版本 UI 暂不支持, 请直接调 API.',
        },
        results: {
          heading: '本次运行结果',
          empty: '还没有运行过. 按 [运行评分] 开始.',
          empty_after_nav: '切走再回来结果会清空, 持久化的历史记录请看 Results 子页.',
          batch_error: '整批运行失败',
          verdict_unknown: '(无 verdict)',
          passed: '通过',
          failed: '未通过',
          overall_fmt: s => `overall ${Number(s).toFixed(1)}/100`,
          gap_fmt: g => `gap ${(Number(g) >= 0 ? '+' : '')}${Number(g).toFixed(1)}`,
          target_conversation: '整段对话',
          preview_label: '目标: ',
          strengths: '亮点',
          weaknesses: '问题',
          details: '完整 scores JSON',
          retry: '重试这条',
          retry_hint: '只重跑这一条, 不动其他结果.',
          retry_unavailable: '整段对话的评分不支持按条重试.',
        },
        toast: {
          schema_load_failed: '加载 schema 详情失败',
          run_ok: n => `评分完成 (${n} 条)`,
          partial_error: (errN, total) => `评分完成但有 ${errN}/${total} 条失败, 请看结果卡片里的错误信息.`,
          run_failed: '评分失败',
          match_main_chat_fallback: reason => {
            const reasonCn = {
              preview_not_ready: '当前 session 没有填 character_name, build_prompt_bundle 无法装配, 已回落到只发 persona.system_prompt 的默认路径',
              bundle_error: 'build_prompt_bundle 装配时异常, 已回落到只发 persona.system_prompt 的默认路径',
            }[reason] || `回落原因: ${reason}`;
            return `已勾选"评委看到主对话 system prompt", 但 ${reasonCn}. 本轮评分未对齐主对话 system.`;
          },
        },
      },
      results: {
        heading: 'Results 评分结果',
        intro: '查看本 session 内所有已完成的评分记录 (来自 Run 子页或 API 直接触发). 支持多维过滤 / 详情 drawer / 导出 Markdown + JSON 报告. 结果随 session 保留, 销毁或新建 session 后清空.',
        loading: '加载评分结果中…',
        no_session: {
          heading: '当前没有活动 session',
          body: '评分结果依附于 session. 请先去 Home 或顶栏 [+ 新会话] 建一个 session, 跑几次评分后回来查看.',
        },
        filter: {
          any: '(全部)',
          schema: 'Schema',
          mode: '模式',
          verdict: 'Verdict',
          passed: '是否通过',
          errored: '是否报错',
          errored_yes: '仅报错',
          errored_no: '排除报错',
          overall_range: 'Overall 分数 (0-100)',
          gap_range: 'Gap (A - B)',
          gap_hint: '仅对 comparative 生效; 填了会自动排除 absolute.',
          min: '最小',
          max: '最大',
          query: '全文搜索',
          query_placeholder: '搜 analysis / diff / target / strengths / weaknesses …',
          query_hint: '不区分大小写, 对分析文本 / schema_id / target_preview 等字段做子串匹配.',
          reset: '重置过滤',
          remove_one: '移除此条件',
        },
        toolbar: {
          count_fmt: (total, selected) => selected > 0
            ? `共 ${total} 条结果, 已选 ${selected}`
            : `共 ${total} 条结果`,
          refresh: '刷新',
          refresh_hint: '重新从 session.eval_results 拉取 (Run 子页跑完评分后, 回这里要手动刷新一次).',
          export_md: '导出 Markdown',
          export_json: '导出 JSON',
          exporting: '导出中…',
          clear_selection: n => `清空选中 (${n})`,
        },
        table: {
          empty: '当前过滤条件下没有匹配的评分结果. 尝试重置过滤或去 Run 子页多跑几次.',
          errored: '报错',
          open: '详情',
          col: {
            time: '时间',
            schema: 'Schema',
            mode: '模式',
            verdict: 'Verdict / 通过',
            score: '分数',
            duration: '耗时',
            target: '目标消息',
          },
        },
        pager: {
          fmt: (from, to, total) => `${from}-${to} / 共 ${total} 条`,
          prev: '上一页',
          next: '下一页',
        },
        drawer: {
          close: '关闭详情',
          overall: '总分 (overall_score)',
          raw_score_fmt: s => `raw_score: ${s}`,
          penalty_fmt: p => `ai_ness_penalty: ${p}`,
          judge_model_fmt: (provider, model) => `judge: ${provider} / ${model}`,
          analysis: '整体分析 (analysis)',
          diff_analysis: '差异分析 (diff_analysis)',
          problem_patterns: '问题模式',
          side_a: 'A (目标 AI)',
          side_b: 'B (参考)',
          gap: 'Gap (A - B)',
          cmp_col_dim: '维度',
          cmp_col_gap: 'Gap',
          relative_advantage_fmt: v => `相对优势: ${v}`,
          target_ai: '目标 AI 回复 (A)',
          target_reference: '参考回复 (B)',
          target_user: '触发该轮的用户输入',
          target_system: 'system_prompt 快照 (截断)',
          raw_response: 'LLM 原始回复',
          raw_json: '完整 EvalResult JSON',
        },
        toast: {
          export_ok: filename => `已导出 ${filename}`,
          export_failed: '导出报告失败',
        },
      },
      aggregate: {
        heading: 'Aggregate 汇总',
        intro: '基于 Results 的过滤条件, 给出总览卡片 / 维度雷达 / comparative gap 轨迹 / 问题模式词频. 失败记录 (error 非空) 会在 "总览" 里单列, 不进入任何均值.',
        // P23: 会话级导出快捷入口. 按钮预选 conversation_evaluations + markdown.
        export_btn: '导出会话报告…',
        export_btn_hint: '把当前会话的对话 + 评分 + aggregate 汇总导出为 Markdown / JSON (可通过模态调整范围).',
        export_modal_subtitle: '预选为 "对话 + 评分" Markdown, 适合给评审 / 自己复盘 — 可在下方调整.',
        loading: '计算汇总中…',
        no_session: {
          heading: '当前没有活动 session',
          body: '汇总依附于 session. 请先去 Home 或顶栏 [+ 新会话] 建一个 session 并跑过几次评分.',
        },
        empty: '当前过滤条件下没有可汇总的有效结果. 去 Results 子页放宽过滤条件, 或者先跑几次评分.',
        section: {
          overview: '总览',
          radar: '维度雷达 (A 侧 / absolute 维度均值)',
          gap_line: 'Comparative Gap 轨迹',
          problem_patterns: '问题模式词频',
          schema_breakdown: '按 Schema 分组',
        },
        section_hint: {
          radar: '每个评分维度的平均得分. 顶点越靠外 = 该维度越强; 用于一眼看出哪些维度拖后腿.',
          gap_line: 'Comparative 模式下 A−B raw 差值随时间的变化. 纵轴 0 为基准, 正值 = A 更好. 看趋势是否在逼近目标.',
          problem_patterns: 'Judger 在 problem_patterns 字段里标注的共性问题, 按出现频次统计 (字号越大 = 越频繁).',
          schema_breakdown: '按评分模板 (schema) 分组后再汇总, 因为不同 schema 的维度 / 分数尺度不可直接混合求均值.',
        },
        cards: {
          total_runs: '总次数',
          successful: '成功次数',
          errored: '失败次数',
          pass_rate: '通过率',
          avg_overall_abs: '均值 · absolute overall',
          avg_overall_cmp_a: '均值 · comparative A',
          avg_gap: '均值 · gap',
          avg_duration: '平均耗时',
        },
        cards_hint: {
          total_runs: '含失败 · 所有 Judger 调用的总次数',
          successful: '成功产出评分的次数 (参与均值)',
          errored: 'Judger 报错或解析失败 · 不计入任何均值',
          pass_rate: 'verdict 为 pass / excellent 的占比 (分母=成功次数)',
          avg_overall_abs: 'absolute 模式 overall 分数 (0-100) 的均值',
          avg_overall_cmp_a: 'comparative 模式下 A 侧 overall 分数 (0-100) 的均值',
          avg_gap: 'comparative A raw − B raw 的均值 · 正数=A 更好 · 0≈持平',
          avg_duration: 'Judger LLM 单次调用耗时均值 (包含网络)',
        },
        verdict_heading: 'Verdict 分布',
        verdict_hint: 'Judger 给出的定性判决档位 (excellent / pass / borderline / fail 等), 比原始分数更直观.',
        radar_empty: '当前过滤下没有足够的维度数据 (需要至少一个 schema + 一条有效结果).',
        radar_source_fmt: sid => `Schema: ${sid}`,
        gap_line_empty: '当前过滤下没有 comparative 结果.',
        problem_empty: '暂无问题模式记录.',
        go_results: '去 Results 子页查看原始记录',
        dim_table: {
          dimension: '维度 (key)',
          avg: '均分',
          samples: '样本数',
        },
        schema_meta: {
          errored_fmt: n => `+${n} 失败`,
          pass_fmt: p => `通过率 ${p}`,
          avg_overall_fmt: v => `overall 均分 ${v}`,
          avg_gap_fmt: v => `gap 均值 ${v}`,
        },
      },
      schemas: {
        heading: 'Scoring Schemas (评分模板)',
        intro: '定义评分的维度 / 权重 / 锚点 / 公式 / 判决规则 / prompt 模板. ScoringSchema 是一等公民, 四类 Judger (Absolute / Comparative / Prompt Test / Analysis) 均由同一份 schema 驱动. 内置三套模板: `builtin_human_like` (整段对话 · Absolute) / `builtin_prompt_test` (单条回复 · Absolute) / `builtin_comparative_basic` (单条 A/B 对比 · Comparative). 内置只读, 需要定制时 [复制为可编辑] 生成 user 副本再改, 或 [+ 新建空白] 从头写.',
        buttons: {
          refresh_list: '刷新列表',
          new_blank: '+ 新建空白 schema',
          save: '保存',
          reload: '撤销修改',
          export: '导出 JSON',
          import: '从文件导入',
          preview_prompt: '预览 prompt',
        },
        list: {
          count_fmt: n => `共 ${n} 条`,
          user_group: n => `用户 schema (${n})`,
          builtin_group: n => `内置 schema (${n}, 只读)`,
          badge_builtin: '内置',
          badge_overriding: '覆盖 builtin',
          badge_absolute: 'Absolute',
          badge_comparative: 'Comparative',
          badge_single: '单条',
          badge_conversation: '整段',
          badge_penalty: '+penalty',
          duplicate: '复制为可编辑',
          delete: '删除',
          dims_fmt: n => `${n} 维度`,
          empty: '没有任何 schema. 点 [+ 新建空白 schema] 或把 JSON 文件放到 testbench_data/scoring_schemas/ 后点 [刷新列表].',
        },
        editor: {
          loading: '加载中…',
          empty_title: '从左侧挑一个 schema 开始编辑',
          empty_hint: '内置模板只读, 点 [复制为可编辑] 得到 user 副本再改. 也可以直接 [+ 新建空白 schema].',
          untitled: '(未命名)',
          readonly_badge: '只读 · 内置',
          dirty_badge: '未保存',
          readonly_hint: '这是内置 schema, 直接编辑会被 git 覆盖. 请先点 [复制为可编辑] 创建 user 副本.',
          basic_heading: '基本信息',
          mode_heading: '模式与粒度',
          dimensions_heading: n => `评分维度 (${n} 个)`,
          dim_untitled: '(未命名维度)',
          dim_anchor_badge_fmt: n => `${n} 锚点`,
          preview_dirty_new: '尚未保存, preview 基于磁盘上的当前 schema. 建议先 [保存] 再预览.',
          preview_dirty_existing: '有未保存改动, preview 读取的是磁盘版本 (非当前草稿). 先 [保存] 可得到最新结果.',
          preview_copy_fail: '复制失败, 请手动选中',
          anchors_heading: '锚点 (1-10 分档位描述)',
          penalty_heading: 'AI-ness Penalty (机器感惩罚分, 可选)',
          formula_heading: '公式与规则 (可选, 留空走默认)',
          prompt_heading: 'Prompt 模板',
          prompt_hint: '可用占位符: {system_prompt} / {history} / {user_input} / {ai_response} / {reference_response} / {character_name} / {master_name} / {scenario_block} / {dimensions_block} / {anchors_block} / {formula_block} / {verdict_rule} / {max_raw_score}. 后几个 block 由 schema 自己生成, 不需要 ctx 传值.',
          prompt_extra_context_warn: '⚠ 安全提示 (F4): 通过 API 调用 POST /api/judge/run 时, 请求体 extra_context 里传入 system_prompt / history / conversation / user_input / ai_response / reference_response / character_name / master_name 任一 key 都会 **覆盖** testbench 管理的评委上下文, 相当于对评委 prompt 的完全控制权. 当前 UI 不暴露此字段, 只对脚本化 API 调用者生效. 一旦检测到上述覆盖, 后端会在 Diagnostics → Errors 写一条 warning 级审计 (不阻断运行). 导入他人 schema / 共享 auto-dialog 脚本前请审阅对应代码, 避免无意中传入敏感 key.',
          tags_heading: '标签',
          errors_heading: '字段错误',
          buttons: {
            add_dim: '+ 添加维度',
            add_anchor: '+ 添加锚档',
            remove: '删除',
            move_up: '↑',
            move_down: '↓',
            toggle_penalty: '启用 AI-ness Penalty',
          },
          fields: {
            id: 'id (= 文件名)',
            name: '显示名',
            description: '描述',
            mode: '评分模式 (mode)',
            granularity: '评分粒度 (granularity)',
            version: '版本 (version)',
            tags: '标签 (逗号分隔)',
            dim_key: 'key',
            dim_label: '显示名 (label)',
            dim_weight: '权重 (weight)',
            dim_description: '描述',
            anchor_range: '分数区间',
            anchor_text: '描述',
            penalty_max: 'max 上限',
            penalty_max_passable: 'max_passable 可通过阈值',
            penalty_description: '简要描述',
            raw_score_formula: 'raw_score 公式 (留空 → 默认 Σ dim*weight - penalty)',
            normalize_formula: 'overall_score 归一化公式 (留空 → 默认按 max_raw_score × 100)',
            verdict_rule: 'verdict 判决规则 (自然语言, 交给 LLM 照做)',
            pass_rule: 'pass_rule (机器判 pass/fail 用, 可选)',
            prompt_template: 'prompt_template (发给 LLM 的原始 prompt)',
          },
          hints: {
            id: '字母 / 数字 / 下划线 / 短横线, 首字非符号, ≤64 字. 内置 schema 以 `builtin_` 开头, 同名 user schema 会覆盖同 id 的 builtin.',
            id_readonly: '内置 schema 的 id 不可改. 复制为 user 副本后可改.',
            mode: 'absolute = 给 A 打绝对分; comparative = 比 A/B 两条回复谁更好.',
            granularity: 'single = 单条回复; conversation = 整段对话.',
            dim_key: '英文小写 + 数字 + 下划线, 首字小写字母, ≤64 字. JSON 输出字段名会直接用它.',
            dim_weight: '≥ 0 的数字. 所有维度 weight × 10 的和等于 max_raw_score.',
            anchor_range: '形如 9-10 / 7-8 / 5-6 / 1-4. 按这个区间给出对应评分档位描述.',
            prompt_template: '支持 Python str.format_map 占位符. `{{` / `}}` 转义字面量花括号.',
          },
          placeholders: {
            dim_description: '例: 回复是否自然, 像真实的人在聊天.',
            anchor_text: '例: 几乎没有明显机器感, 语气流畅自然, 像真实的人在即时聊天.',
            prompt_template: '{system_prompt}\n=== 对话 ===\n{history}\n=== 用户 ===\n{user_input}\n=== AI ===\n{ai_response}\n...',
          },
          meta: {
            max_raw_score: v => `max_raw_score = ${v}`,
            dims_count: n => `dim 数: ${n}`,
            penalty_enabled: '已启用 AI-ness Penalty',
            penalty_disabled: '未启用 AI-ness Penalty',
          },
          preview_dialog: {
            title: 'Prompt 预览',
            char_count: n => `共 ${n} 字`,
            used_label: '已用占位符',
            missing_label: '未填的已知占位符 (会被替换成空)',
            close: '关闭',
            copy: '复制到剪贴板',
            copy_done: '已复制',
          },
        },
        prompt: {
          new_id: '新 schema 的 id (= 文件名):',
          duplicate_id: src => `把 "${src}" 复制为 user 副本. 新 id:`,
          confirm_delete: id => `确定要删除 user schema "${id}" 吗? 此操作不可恢复 (但内置版本若存在会重新生效).`,
        },
        toast: {
          list_failed: '加载 schema 列表失败',
          load_failed: '加载 schema 详情失败',
          id_taken: id => `user schema "${id}" 已存在. 请换个 id.`,
          duplicate_failed: '复制失败',
          duplicated: id => `已复制到 "${id}"`,
          delete_failed: '删除失败',
          deleted: id => `已删除 "${id}"`,
          resurfaces_builtin: id => `内置版本的 "${id}" 重新生效`,
          save_failed: '保存失败',
          save_errors: n => `保存被拒: ${n} 条字段错误待修正`,
          saved: id => `已保存 "${id}"`,
          now_overriding_builtin: id => `当前 user schema "${id}" 覆盖了同名 builtin — 评分时优先用 user 版本`,
          rename_left_old: old => `改 id 已保存新版本, 但删除旧 user schema "${old}" 时失败, 请手动 [刷新列表] 检查.`,
          preview_failed: '生成 prompt 预览失败',
          import_failed: '导入失败',
          imported: id => `已导入 "${id}"`,
        },
      },
    },
    diagnostics: {
      nav: {
        errors: 'Errors 错误',
        logs: 'Logs 日志',
        snapshots: 'Snapshots 快照',
        paths: 'Paths 路径',
        reset: 'Reset 复位',
      },
      errors: {
        heading: 'Errors · 错误面板',
        intro: '进程级错误 ring buffer (容量 200, 重启清空). 数据源 = 后端中间件捕获的未处理异常 + 前端 errors_bus 同步的 HTTP/SSE/JS/Promise 事件. 与 Logs 子页互补: Logs 是"所有结构化事件"的权威回放, Errors 是"最近出了什么问题"的工作队列.',
        loading: '加载中...',
        empty: '暂无错误. 继续测试; 有问题会自动出现在这里.',
        empty_filtered: total => `当前过滤条件下没有匹配项 (总计 ${total} 条). 调整过滤或 [清除过滤].`,
        load_failed: reason => `加载失败: ${reason}`,
        count_fmt: (matched, total) => matched === total
          ? `共 ${total} 条`
          : `匹配 ${matched} / 共 ${total} 条`,
        all_sources: '全部来源',
        all_levels:  '全部级别',
        source_labels: {
          middleware: '后端中间件',
          http: 'HTTP',
          sse: 'SSE',
          js: 'JS',
          promise: 'Promise',
          resource: '资源',
          pipeline: 'Pipeline',
          synthetic: '合成',
          unknown: '其他',
        },
        level_labels: {
          info: 'info',
          warning: 'warn',
          error: 'error',
          fatal: 'fatal',
        },
        search_placeholder: '搜索 type / message / url...',
        auto_refresh: '自动刷新 (5s)',
        include_info_label: '包含 info 级',
        include_info_tooltip: '默认隐藏 info 级条目 (Errors 页语义 = "最近出了什么问题"; info 级一般是审计回放, 例如外部事件仿真成功时会往 ring 里写一条 avatar_interaction_simulated 给"事后可溯源"留痕, 但它不是"问题"). 勾上后会把 info 级也一起显示. 如果在 "级别" 下拉里已经选了具体的 level, 这个开关会被忽略 (尊重用户显式筛选意图).',
        refresh: '刷新',
        trigger_test: '制造测试错误',
        synth_msg: '人工触发的测试错误 (用于验证诊断面板全链路).',
        trigger_test_done: '已追加一条合成错误, 几秒后回到面板核对',
        clear: '清空',
        clear_confirm: '确认清空进程级错误 ring buffer? (Logs 子页的 JSONL 日志不受影响)',
        cleared: count => `已清空 ${count} 条`,
        clear_failed: '清空失败',
        clear_filter: '清除过滤',
        trace_digest_label: '异常栈 (trace_digest)',
        detail_label: '附加信息 (detail)',
        pager_prev: '← 上一页',
        pager_next: '下一页 →',
        pager_fmt: (page, total) => `第 ${page} / ${total} 页`,
        // P24 F7 Option B — Security-focused quick filters.
        security_filter_label: '安全筛选:',
        security_filter: {
          integrity_check: '存档体检',
          judge_override: '评委 override',
          prompt_injection: '注入命中',
          timestamp_coerced: '时间戳纠正',
          all: '以上任一',
        },
        security_filter_hint: {
          integrity_check: '只看"存档完整性校验"相关事件 (session 加载 / 导入时 hash 不符、字段缺失等). 这是数据损坏类异常的第一窗口.',
          judge_override: '只看"评委 extra_context 覆盖内置键"的记录. 如果有人通过 API 传 extra_context 偷偷改 persona_system / dimensions_block 等内置字段, 会在这里出现, 是评分框架被 prompt injection 的早期信号.',
          prompt_injection: '只看"Chat 发送 / persona 编辑 / memory 字段命中注入模式"的记录. 按"检测不改, 不阻断"原则只是审计留痕, 框架不会过滤或拒绝. 若目标是测试 AI 对抗性鲁棒性, 这些记录是预期的 (可忽略); 若并非刻意输入注入 payload, 建议改回常规文本.',
          timestamp_coerced: '只看"消息时间戳被自动纠正"的记录. 说明 append_message 遇到了非单调时间戳 (通常是手动 set_cursor 回到过去), 强行上推到上一条. 多发说明测试流程存在问题, 但偶发是正常容错.',
          all: '勾选则同时筛这四类 (存档体检 + 评委 override + 注入命中 + 时间戳纠正), 做一次"安全相关条目总体扫描".',
        },
      },
      logs: {
        heading: 'Logs · 结构化日志',
        intro: '读取 tests/testbench_data/logs/<session_id>-YYYYMMDD.jsonl. 每条一个 op (chat.send / judge.run / memory.* 等) 含 payload / error. 切换 session + date 看对应文件.',
        intro_help: 'Session = 一次测试会话的唯一 id (每次左上角 [新建会话] 生成), dropdown 里 ★ 标记的是当前活跃会话. Op = 具体动作 (chat.send.begin = "开始发送聊天" 等), 鼠标悬停任意 op 可查看中文说明. DEBUG 日志默认关闭 (节省 30%+ 体量), 排查问题时点右上 [Debug 日志] 勾上再复现.',
        loading: '加载中...',
        no_sessions: '还没有日志文件. 和后端跑起对话/评分/记忆操作后会自动生成.',
        session_load_failed: reason => `加载日志 session 列表失败: ${reason}`,
        pick_session: '请选择一个 session + 日期.',
        empty_file: '此日志文件目前为空.',
        empty_filtered: total => `当前过滤条件下没有匹配项 (总计 ${total} 条). 调整过滤条件再看.`,
        load_failed: reason => `加载日志失败: ${reason}`,
        count_fmt: (matched, total) => matched === total
          ? `共 ${total} 条`
          : `匹配 ${matched} / 共 ${total} 条`,
        session_label: '会话',
        session_help_tooltip: 'Session = 一次测试会话的唯一 id. 每次左上角点 [新建会话] 会生成新 id, 此会话产生的所有日志都记到 <sid>-YYYYMMDD.jsonl. ★ 标记的是当前活跃会话. _anon 是无活跃会话时 (启动期/健康检查) 的 fallback.',
        session_opt_current_prefix: '★ 当前 · ',
        session_opt_anon_prefix: '_anon · ',
        session_opt_days_suffix: '天',
        session_opt_all_fmt: n => `☆ 全部会话 (合并 ${n} 个)`,
        session_badge_tooltip_fmt: sid => `来自 session ${sid}`,
        export_disabled_all_mode: '合并视图不提供下载 (没有单一源文件), 先选定某一个 session 再导出.',
        date_label: '日期',
        date_help_tooltip: '日期 = 该 session 的日志文件按天滚动 (YYYYMMDD). 一次会话跨天会产生多个文件, 分日期切换查看.',
        all_levels: '全部级别',
        level_tooltip: 'DEBUG = 高频 echo (默认不落盘); INFO = 正常动作; WARNING = 可恢复异常; ERROR = 未捕获异常或业务失败. DEBUG 选项要等 [Debug 日志] 开关打开并复现操作之后才看得到数据.',
        level_labels: {
          INFO: 'info',
          WARNING: 'warn',
          ERROR: 'error',
          DEBUG: 'debug',
        },
        search_placeholder: '关键字搜索 op / payload / error...',
        op_facet_label: 'Op 筛选:',
        op_facet_help: 'Op 按"模块.动作[.子动作][.阶段]" 命名, 例如 chat.send.begin = Chat 模块 · 发送 · 开始阶段. 悬停每个 chip 看中文说明; 点击可把列表过滤到该 op.',
        op_clear: name => `× ${name}`,
        auto_refresh: '自动刷新 (5s)',
        refresh: '刷新',
        refresh_sessions: '扫描目录',
        export: '下载 JSONL',
        error_label: 'Error',
        payload_label: 'Payload',
        raw_label: '原始 JSON',
        pager_prev: '← 上一页',
        pager_next: '下一页 →',
        pager_fmt: (page, total) => `第 ${page} / ${total} 页`,
        // 保留/清理 (P19 hotfix 2). 日志每天滚动, 默认保留最近 14 天,
        // 超期自动在 server 启动 + 每 12h 定时 + 这里手动按钮触发.
        retention_fmt: (days, files, sizeStr) =>
          `保留最近 ${days} 天 · 当前 ${files} 个文件 · 占用 ${sizeStr}`,
        cleanup_btn: '清理旧日志',
        cleanup_running: '清理中...',
        cleanup_confirm_fmt: days =>
          `即将删除 ${days} 天以前的日志文件 (今天的文件不会被删). 该操作不可撤销, 确认继续吗?`,
        cleanup_done_fmt: (count, sizeStr) =>
          `已清理 ${count} 个旧日志文件, 释放 ${sizeStr}`,
        cleanup_nothing: '没有可清理的旧日志 (全部文件都在保留期内)',
        cleanup_failed_fmt: reason => `清理失败: ${reason}`,
        // Debug 日志开关 (P19 hotfix 3, 2026-04-20)
        debug_toggle_label: 'Debug 日志',
        debug_toggle_tooltip: '默认关. 开启后, 后端会把 DEBUG 级的高频 echo (例如 chat.prompt_preview 每次 UI 刷新预览都会打) 也写进 JSONL 供调试. 关闭期间 SessionLogger 会直接 skip, 不占磁盘也不卡 IO. 切换不需要重启 server, 立刻生效.',
        debug_turned_on: 'Debug 日志已开启, 从现在起 DEBUG 级条目会落盘',
        debug_turned_off: 'Debug 日志已关闭, 后续 DEBUG 条目不再落盘 (历史数据保留)',
        debug_toggle_failed_fmt: reason => `切换 Debug 日志失败: ${reason}`,
      },
      paths: {
        title: 'Paths · 本地路径一览',
        intro: '集中展示 testbench 所有运行时读 / 写的本地目录位置. 数据目录 (sandbox / logs / saved_sessions / autosave / exports / 自定义 schemas / dialog templates) 位于 tests/testbench_data/ 下, 代码侧目录 (builtin 资源 / docs) 只读展示, 不可打开.',
        refresh_btn: '刷新',
        platform_fmt: p => `操作系统: ${p}`,
        loading: '加载中…',
        load_failed_fmt: r => `加载失败: ${r}`,
        empty: '后端没有返回数据, 请点击 [刷新] 重试.',
        data_root_label: '数据目录 (整体 gitignored)',
        data_root_meta_fmt: (size, files) => `${size} · ${files} 个文件`,
        gitignore_note: '提示: tests/testbench_data/ 整个目录已加入 .gitignore, 不会被提交到 git. 若要清理本地测试数据, 直接删除这个目录即可 (testbench 下次启动时会自动重建子目录骨架).',
        group: {
          session: {
            title: '当前会话',
            intro: '仅在活跃会话下出现. 会话销毁后这些路径会失效 (沙盒目录被清理, log 文件保留但不再有新数据写入).',
          },
          shared: {
            title: '跨会话数据',
            intro: '所有会话共用的持久化位置. 保存/加载 / 导出 / 自定义资源都落在这里.',
          },
          code: {
            title: '代码侧 (只读)',
            intro: '内置资源和文档, 由 git 管理. 可以复制路径但不能在 Diagnostics 里 [打开] — 开发人员自己用 IDE 打开即可.',
          },
        },
        col: {
          name: '名称',
          path: '路径',
          size: '大小',
          files: '文件',
          exists: '存在',
          actions: '操作',
        },
        label: {
          data_root: 'testbench_data (根目录)',
          current_sandbox: '当前会话沙盒',
          current_session_log: '当前会话日志 (今日)',
          sandboxes_all: '所有沙盒 (各会话子目录)',
          logs_all: '日志目录',
          saved_sessions: '已保存会话',
          autosave: '自动存档',
          exports: '导出文件',
          user_schemas: '自定义评分 schemas',
          user_dialog_templates: '自定义对话模板',
          code_dir: '代码目录',
          builtin_schemas: '内置评分 schemas',
          builtin_dialog_templates: '内置对话模板',
          docs: '项目文档 (PLAN / PROGRESS / AGENT_NOTES)',
        },
        hint: {
          current_sandbox: '本次会话 ConfigManager 的 app_docs 替身; 人设 / 三层记忆 / 快照冷存都落在这里. 新建/重置会话会清空.',
          current_session_log: '本会话今天的 JSONL 日志. 每一条 chat.send / judge.run / memory.op 都会记一行. 跨日会切到新的 YYYYMMDD 文件.',
          sandboxes_all: '所有历史会话的沙盒. 销毁会话会清理对应子目录; 异常退出可能留下残留, 需要时可以手动删除不活跃的目录.',
          logs_all: '所有会话的 JSONL 日志按 `<session_id>-YYYYMMDD.jsonl` 组织. 可以直接复制路径给别人协作排错.',
          saved_sessions: '手动保存的会话档案 (.json) 落点.',
          autosave: '自动存档落点 (debounced, 最新 N 份).',
          exports: '导出 (Markdown / JSON / dialog template) 的目标目录. Evaluation → Aggregate 的 [导出报告] 目前已落在这里.',
          user_schemas: '用户自定义 ScoringSchema. 新建 / 复制内置 schema 都会写到这里.',
          user_dialog_templates: '用户自定义对话模板 (*.json). Setup → Scripts 里新建 / 复制内置模板写到这里.',
          code_dir: 'testbench 源码. 由 git 管理, 只读.',
          builtin_schemas: '仓库里自带的三套 schema (human-like / prompt-test / comparative-basic). 只读.',
          builtin_dialog_templates: '仓库里自带的对话模板. 只读.',
          docs: 'PLAN.md / PROGRESS.md / AGENT_NOTES.md 等项目文档位置.',
        },
        badge_session: '当前会话',
        action: {
          copy: '复制路径',
          open: '打开',
          open_disabled_missing: '路径不存在, 无法打开',
          open_disabled_readonly: '代码侧路径禁用 [打开] (白名单仅 testbench_data)',
          // P23 export sandbox snapshot 快捷入口.
          export_sandbox: '导出沙盒快照…',
          export_sandbox_hint: '把当前 session 的完整状态 (含 memory tarball base64) 导出为 JSON, 可通过导入端点逆向加载.',
          export_sandbox_disabled: '需要活跃会话才能导出沙盒快照.',
        },
        // P23 Paths → Export sandbox snapshot 模态副标题.
        export_modal_subtitle: '预选为 "完整 + JSON + 附带 memory tarball", 产物可通过 POST /api/session/import 逆向导入 (等同于 save_archive 的转储).',
        toast: {
          copied: '路径已复制到剪贴板',
          copy_failed: '复制失败, 请手动选中路径',
          opened: '已请求操作系统打开路径',
          open_failed_fmt: r => `打开失败: ${r}`,
          open_failed: '打开失败',
        },
        // P24 §3.1 H1 — system health card at the top of this page.
        health: {
          title: '系统健康',
          status: {
            healthy: '正常',
            warning: '注意',
            critical: '严重',
          },
          check: {
            disk_free_gb: '剩余磁盘',
            log_dir_size_mb: '日志目录大小',
            orphan_sandboxes: '孤儿沙盒',
            autosave_scheduler: '自动保存调度器',
            diagnostics_errors: '诊断错误 (累计)',
          },
          no_session: '无活跃会话',
          scheduler_alive: '运行中',
          scheduler_dead: '已停止',
          orphans_count: (n) => `${n} 个`,
          errors_count: (n) => `${n} 条`,
          checked_at_fmt: (ts) => `最后检查: ${ts}`,
          // 顶部"问题摘要"面板 — 仅在 status != healthy 时出现.
          problem_heading: '需要关注的项:',
          threshold_warning: (warn_at, crit_at, _key) =>
            `(注意阈值 ${warn_at}, 严重阈值 ${crit_at})`,
          threshold_critical: (warn_at, crit_at, _key) =>
            `(严重阈值 ${crit_at}, 已超)`,
          advice: {
            disk_free_gb: '建议清理 tests/testbench_data/ 或把该目录迁到更大的盘.',
            log_dir_size_mb: '可在 Diagnostics → Logs 里点 [清理旧日志] 缩减.',
            orphan_sandboxes: '下方"孤儿沙盒"区可逐条清理, 或点 [一键清理空沙盒] 批量删除 0 字节的残留.',
            autosave_scheduler: '调度器异常一般是服务重启或后台任务 cancel 失败, 重启服务通常恢复.',
            diagnostics_errors: '打开 Diagnostics → Errors 查看具体错误; 次数过多可在那里 [清空错误列表].',
          },
        },
        // P24 §15.2 B / P-D — orphan sandbox triage section.
        orphans: {
          title: '孤儿沙盒 (已结束会话的残留目录)',
          intro: '列出 sandboxes/ 下没有对应活跃会话的目录. 这些通常是进程被强杀 / 异常退出留下的; 也可能是你之前故意保留用来对比两次会话 memory 的. 系统**不自动清理** — 请自行核对后决定删除还是保留.',
          empty: '没有孤儿沙盒. 目录清爽.',
          load_failed_fmt: (err) => `加载失败: ${err}`,
          summary_fmt: (count, total) => `共 ${count} 条 · 占用 ${total}`,
          col: {
            session_id: '会话 ID / 路径',
            size: '大小',
            mtime: '最后修改',
          },
          delete_btn: '删除',
          confirm_delete_fmt: (sid, size) =>
            `确认删除孤儿沙盒 "${sid}" (占用 ${size})? 此操作不可撤销. 里面可能含有上次会话的 memory / logs / 快照冷存.`,
          delete_ok_fmt: (sid, freed) => `已删除 ${sid}, 释放 ${freed}`,
          delete_err: '删除失败',
          delete_partial: '部分删除',
          delete_partial_detail_fmt: (remaining) =>
            `系统返回 "已删除但有残留" — 可能是 Windows 文件锁. 剩余 ${remaining}, 可以重启服务后重试.`,
          // 一键清理空沙盒 — 仅对 0 字节的沙盒生效, 提供给懒得重启服务的用户.
          // (服务重启时 boot_cleanup 也会自动清空沙盒, 同样安全等级.)
          clear_empty_btn: (n) => n > 0
            ? `一键清理空沙盒 (${n} 个 0 字节)`
            : '一键清理空沙盒 (当前无 0 字节沙盒)',
          clear_empty_hint: '只清理完全没有文件的 0 字节沙盒. 有内容的沙盒不会被动. 和服务重启时 boot_cleanup 自动清理的逻辑一致.',
          clear_empty_disabled_hint: '当前所有孤儿沙盒都有内容, 没有可一键清理的 0 字节目录 (通常是因为服务重启时 boot_cleanup 已自动清过). 若想清理有内容的沙盒, 请用每行右侧的 [删除] 按钮.',
          clear_empty_none_toast: '当前没有 0 字节的空沙盒.',
          confirm_clear_empty_fmt: (n) =>
            `确认一键清理 ${n} 个完全空白 (0 字节) 的沙盒目录? 这些沙盒没有任何用户数据, 可安全删除. 非空的沙盒不会受影响.`,
          clear_empty_ok_fmt: (n) => `已清理 ${n} 个空沙盒.`,
          clear_empty_err: '一键清理失败',
          clear_empty_partial_fmt: (cleared, errors) =>
            `清理了 ${cleared} 个, ${errors} 个失败 (可能文件锁).`,
        },
      },
      reset: {
        title: 'Reset · 三级复位',
        intro: '根据清理范围从轻到重分三级. 每级都会在动手前自动建一条 pre_reset_backup 快照 (带 "备份" 徽章), 误操作时可以从 Diagnostics → Snapshots 里回退这个快照把会话"复活".',
        no_session: '请先建会话, Reset 操作需要活跃会话才能生效.',
        backup_note: '执行前会自动建 pre_reset_backup 快照, 可从时间轴回退.',
        level: {
          soft: {
            title: 'Soft · 轻复位',
            severity: '低风险',
            desc: '只清空当次测试的对话和评分结果. 人设 / 记忆 / 虚拟时钟 / 模型配置 / 快照时间线全部保留.',
            confirm_desc: '你将执行 Soft Reset: 清空 messages + eval_results, 保留其他所有状态.',
            exec_btn: '执行 Soft Reset',
          },
          medium: {
            title: 'Medium · 中复位',
            severity: '中等风险',
            desc: 'Soft 的范围 + 清空沙盒 memory/ 目录下所有 JSON 文件 (recent / facts / reflections / persona 归零). 虚拟时钟 / 模型配置 / 快照时间线保留.',
            confirm_desc: '你将执行 Medium Reset: Soft + 额外清空 memory/ 目录所有 JSON 文件.',
            exec_btn: '执行 Medium Reset',
          },
          hard: {
            title: 'Hard · 硬复位',
            severity: '高风险 (不可撤销)',
            desc: '销毁整个沙盒 _app_docs 目录结构 + 重建空骨架. 持久化的 persona.json / characters.json / facts.json 等全部被删. 清除除 pre_rewind_backup / pre_reset_backup 外的所有快照. 唯一保留: 模型配置 (不让你重新输 api_key) + 全局 schemas (不在沙盒里). 执行成功后页面会自动刷新一次, 把前端状态也一并清零 (避免组件挂着旧 state 造成 UI 异常).',
            confirm_desc: '你将执行 Hard Reset: 沙盒完全清零 + 快照时间线裁剪. 仅保留模型配置和全局评分 schemas. 此操作不可撤销, 但会先留一条 pre_reset_backup 在时间轴中. 执行成功后页面将自动刷新.',
            exec_btn: '执行 Hard Reset',
          },
        },
        bullets: {
          removed_label: '会清除的内容:',
          preserved_label: '会保留的内容:',
        },
        bullet: {
          messages: '对话消息 (session.messages)',
          eval_results: '评分记录 (session.eval_results)',
          memory: '三层记忆文件 (recent / facts / reflections / persona JSON)',
          persona: '人设 (persona 字段)',
          clock: '虚拟时钟 (cursor / bootstrap / pending)',
          stage: '流水线阶段 (stage_state)',
          timeline_non_backup: '非备份快照 (普通 capture 产生的条目)',
          timeline_backups: '备份快照 (pre_rewind_backup / pre_reset_backup)',
          model_config: '模型配置 (chat / simuser / judge / memory 四组参数)',
          schemas: '评分 schemas (全局, 不在沙盒里, 不受 reset 影响)',
          timeline: '快照时间线 (全部, 含备份)',
        },
        confirm: {
          title_fmt: levelTitle => `二次确认: ${levelTitle}`,
          warn: '确认后立即执行, 不可撤销. 如需恢复, 请从 Diagnostics → Snapshots 回退 pre_reset_backup 条目.',
          cancel: '取消',
          do_fmt: levelTitle => `确认执行 ${levelTitle}`,
        },
        last_result: {
          title_fmt: levelTitle => `上次执行结果 (${levelTitle}):`,
          kind: {
            messages: '清除消息',
            eval_results: '清除评分',
            memory_files: '清除记忆文件',
            app_docs_files: '清除沙盒文件',
            snapshots: '清除非备份快照',
          },
          backup_fmt: id => `pre_reset_backup 快照 id = ${id} (可在时间轴回退)`,
        },
        toast: {
          done_fmt: levelTitle => `${levelTitle} 执行完成`,
          busy_fmt: op => `其它操作正在运行 (${op}), 请稍后重试`,
          failed_fmt: r => `Reset 失败: ${r}`,
        },
      },
    },
    // P18: 快照 / 时间线 / 回退. 顶层命名空间是因为 topbar chip (快速回退)
    // 和 diagnostics → Snapshots 子页 (完整管理) 共用绝大部分文案; 放在
    // diagnostics 下会让 topbar_timeline_chip.js 看起来在跨命名空间取词.
    snapshots: {
      page: {
        title: 'Snapshots · 快照时间线',
        intro: '会话的完整快照列表 (消息 + 记忆 + 时钟 + stage + 评分). 每次发消息 / 编辑 / 记忆操作 / 阶段推进都会自动建一条; 5 秒内同类触发会自动合并避免刷屏. 点 [回退] 能把整个会话倒回到该时刻 (会自动建一条 pre_rewind_backup 兜底).',
        time_legend:
          '时间列的**主字段**(大字体)是**系统真实时间** (快照创建时的 wall clock), 带 ``@`` 前缀的**小字段**是**虚拟时钟 cursor** (当时会话内部时间), 两者通常不同; 虚拟时间不在也不会显示 (新建会话未设虚拟时间前).',
        summary_fmt: (total, hot, cold, max_hot) =>
          `共 ${total} 条 · 内存 ${hot}/${max_hot} · 磁盘 ${cold}`,
        manual_btn: '+ 手动建快照',
        refresh_btn: '刷新',
        clear_all_btn: '清空全部',
        loading: '加载中…',
        load_failed_fmt: reason => `加载失败: ${reason}`,
        empty: '尚无快照. 发消息 / 编辑 / 记忆操作都会自动建快照.',
        mechanism_heading: '快照是怎么存的? (点击展开)',
        mechanism_intro:
          '每次"发消息 / 编辑 / 记忆操作 / 阶段推进 / 人设更新 / 剧本加载 / 自动对话开始"都会调 capture(trigger=...) 建快照. 存储规则:',
        mechanism_point_hotcold_fmt: (max_hot) =>
          `冷热分层: 最新 ${max_hot} 条非备份快照留在内存 (hot, 表格里 "存储" 列显示 "内存"), 超出时把最老的一条压缩 (gzip) 写到沙盒 <sandbox>/.snapshots/<id>.json.gz (cold, 显示 "磁盘"). 冷快照仍在列表里, 点 [查看] 或 [回退] 时才按需解压 — UI 体验完全一致.`,
        mechanism_point_debounce_fmt: (seconds) =>
          `去抖合并: 同一 trigger 在 ${seconds} 秒内连续触发会原地覆盖上一条, 避免连按几下 [发送] 刷出一堆几乎一样的快照. 例外 (永远不合并): init (会话起点) / manual (手动建) / pre_rewind_backup (回退兜底).`,
        mechanism_point_backup:
          '回退兜底: 执行 [回退] 前会自动建一条 pre_rewind_backup (带 "备份" 徽章的黄色条目), 再还原到目标快照. 备份条目不占内存配额, 也不会被 [清空全部] 删掉, 可以作为 "撤销回退" 的锚点.',
        mechanism_point_rewind:
          '回退会截断: 回退到 snapshot X 时, 晚于 X 的普通快照会被丢弃 (toast 会显示 "丢弃 N 条"), 但备份条目会保留下来.',
        mechanism_point_reset:
          '会话级生命周期: SnapshotStore 挂在当前 Session 上, [新建会话] 或重置会话会整个丢弃时间线; 沙盒里的 .snapshots/ 目录也会随之清理.',
        mechanism_point_safety:
          '最后兜底: 建快照失败不会让业务操作回滚 — 发消息 / 改记忆 本身已经落地, 仅仅在服务器日志里记 warning 提示 "时间线有洞", 保证核心交互永不中断.',
      },
      col: {
        time: '时间',
        label: '标签',
        trigger: '触发',
        messages: '消息',
        memory: '记忆文件',
        stage: '阶段',
        storage: '存储',
        actions: '操作',
      },
      trigger: {
        init: '初始化',
        manual: '手动',
        send: '对话',
        edit: '编辑消息',
        memory_op: '记忆操作',
        stage_advance: '阶段推进',
        stage_rewind: '阶段回退',
        persona_update: '人设更新',
        script_load: '剧本加载',
        script_run_all: '剧本跑完',
        auto_dialog_start: '自动对话',
        pre_rewind_backup: '回退兜底',
      },
      storage: {
        hot: '内存',
        cold: '磁盘',
      },
      badge: {
        backup: '备份',
      },
      action: {
        rewind: '回退',
        rename: '重命名',
        delete: '删除',
        view: '查看',
      },
      prompt: {
        manual_label: '输入快照标签 (留空则自动生成):',
        rename_label: '新标签:',
        delete_confirm: fmt => `确定删除快照 "${fmt}"? 不可撤销.`,
        clear_confirm: '确定清空全部快照? 当前会话的时间线将被抹除 (pre_rewind_backup 备份条目会保留). 不可撤销.',
        rewind_confirm: fmt => `确定回退到 "${fmt}"? 所有此后的消息 / 记忆 / 评分都会被替换回快照时刻. 回退前会自动建一条 pre_rewind_backup.`,
      },
      toast: {
        created_fmt: fmt => `已建快照: ${fmt}`,
        create_failed_fmt: fmt => `建快照失败: ${fmt}`,
        rewound_fmt: (label, dropped) => dropped > 0
          ? `已回退到 ${label} (丢弃 ${dropped} 条后续快照)`
          : `已回退到 ${label}`,
        rewind_failed_fmt: fmt => `回退失败: ${fmt}`,
        renamed: '重命名已保存',
        rename_failed_fmt: fmt => `重命名失败: ${fmt}`,
        deleted: '已删除',
        delete_failed_fmt: fmt => `删除失败: ${fmt}`,
        cleared_fmt: n => `已清空 ${n} 条快照`,
        clear_failed_fmt: fmt => `清空失败: ${fmt}`,
        no_session: '请先建会话',
      },
      view: {
        title_fmt: fmt => `快照 ${fmt}`,
        meta_id: 'ID',
        meta_label: '标签',
        meta_trigger: '触发',
        meta_created: '真实时间',
        meta_virtual: '虚拟时间',
        meta_stage: '阶段',
        meta_msgs: '消息数',
        meta_mem: '记忆文件数',
        meta_backup: '备份标记',
        meta_storage: '存储',
        close: '关闭',
      },
    },
    chat: {
      // P08 引入 preview; P09 补齐 stream / composer / role / source 命名空间.
      // 保持四个子节点 (preview / stream / composer / role|source) 平行, UI 代码
      // 里出现的任何 `chat.*` key 都能在这里直接定位.
      role: {
        user: '用户',
        assistant: '助手',
        system: '系统',
      },
      source: {
        manual: '手动',
        inject: '注入',
        llm: 'LLM',
        simuser: '假想用户',
        script: '脚本',
        auto: '自动',
        // r5 T7: 外部事件 banner 伪消息 — 只在对话流里作可视标记, 不进
        // LLM wire (prompt_builder 单点过滤). 文案和普通 source pill
        // 保持"短短两三字"的风格, 避免 header 被挤乱.
        external_event_banner: '测试事件',
      },
      stream: {
        count: (n) => `共 ${n} 条消息`,
        refresh_btn: '刷新',
        clear_btn: '清空',
        // 2026-04-23 P25 Day 2 polish r6: "把当前对话内容追加到最近对话
        // 记忆" 一键快捷钮; 落盘到 memory/<character>/recent.json
        // (LangChain canonical {type, data.content} 形状, 与主程序 recent
        // 语义对齐). 过滤 external_event_banner / 空 content / 非
        // user|assistant|system role. 默认 append 模式.
        save_to_recent_btn: '保存到最近对话',
        save_to_recent_title: '把当前 Chat 对话追加写入 memory/recent.json (LangChain canonical 形状). 默认 append, banner / 空消息会被自动过滤.',
        empty: '尚无消息.',
        empty_hint: '在下方输入框输入一条用户消息, 按 [发送] 发起对话; 或用 [注入 sys] 写入一条系统级中段指令.',
        menu_title: '消息操作',
        menu: {
          edit: '编辑内容',
          timestamp: '编辑时间戳',
          rerun: '从此处重跑',
          delete: '删除',
        },
        prompt: {
          edit: '编辑消息内容 (取消则不修改):',
          timestamp: '输入 ISO8601 时间戳, 留空则用当前虚拟时间:',
          delete: '确定删除这条消息? 不可撤销.',
          rerun: '将截断从此消息之后的所有内容, 并把时钟回退到本条消息的 timestamp. 继续?',
          clear_all: '清空当前会话全部消息? 不可撤销.',
          save_to_recent: (n) =>
            `把当前 ${n} 条消息追加写入 memory/recent.json ?\n\n(banner / 空消息 / 非对话 role 会自动过滤. 追加模式, 不去重; 可在 Setup → Memory 编辑 recent 手动校对.)`,
        },
        toast: {
          bad_timestamp: '时间戳格式无效',
          rerun_done: (n) => `已截断 ${n} 条后续消息, 时钟已回退. 可继续编辑 / 重新发送.`,
          // 2026-04-22 Day 8 #3: 末尾是 user 时, 引导用户直接按 [Send] 让
          // AI 对这条 user 回复, 避免产生连续两条 user 消息.
          rerun_done_user_tail: (n) =>
            `已截断 ${n} 条后续消息. 末尾已是 user 消息, 直接点 [发送] (不打字) 即可让 AI 对这条重新回复; 或在 Composer 里编辑/补充内容再发送.`,
          // r6 save_to_recent 按钮 toast (succ / empty / error).
          save_to_recent_ok: (added, total, skipped) => {
            const parts = [];
            if (skipped.banner) parts.push(`${skipped.banner} 条 banner`);
            if (skipped.empty_content) parts.push(`${skipped.empty_content} 条空消息`);
            if (skipped.unsupported_role) parts.push(`${skipped.unsupported_role} 条非对话`);
            const tail = parts.length ? `; 过滤: ${parts.join(' / ')}` : '';
            return `已写入 recent.json: +${added} 条, 当前共 ${total} 条${tail}`;
          },
          save_to_recent_empty: '没有可写入的消息 (banner / 空内容 / 非对话 role 已全部过滤). 先发送几条真对话再试.',
          save_to_recent_error: (msg) => `写入失败: ${msg}`,
        },
        long_content_title: (n) => `长消息 (${n} 字符)`,
        // P12: assistant 消息上挂的 reference_content (脚本 expected / 手工
        // 写的"理想人类回复") 折叠块标题 + 空态提示.
        reference_title: '参考回复 (reference_content)',
        reference_hint: '由脚本 expected 回填或测试人员手动写入, 用于 Comparative Judger 对照评分. 不会发给目标 AI.',
        // P17 消息头内联评分徽章. 只挂在 assistant 气泡且有命中评分时;
        // verdict 只在 tooltip 里展示, 徽章主文本仅用 overall / gap,
        // 以免 header 行被撑爆.
        eval_badge: {
          overall_fmt: (ov) => `评分 ${ov}`,
          gap_fmt: (g) => `gap ${g}`,
          errored: '评分出错',
          tooltip_fmt: (schema, verdict) => `Schema: ${schema} · Verdict: ${verdict}`,
          click_hint: '点击查看此消息的全部评分',
        },
      },
      composer: {
        placeholder: '在此输入消息 (Ctrl+Enter 发送)...',
        send: '发送',
        sending: '发送中…',
        send_title_user: '把你输入的内容以 role=user 写入历史, 并立即调用 LLM 拿一条回复.',
        send_title_system: '把你输入的内容以 role=system 写入 session.messages, 立即调用 LLM 拿一条回复. 注意: 主程序运行期无 role=system 消息 (SystemMessage 只存在于初始化 position 0), 为避免 Gemini 对 shape 过敏 (400 空输入 / 200 空 reply 导致错位), wire 层会把本条消息重写为 role=user + `[system note] ` 前缀发给 LLM. Diagnostics → Logs 会留审计记录.',
        inject: '注入 sys',
        inject_title: '把你输入的内容以 role=system 写入历史, 但不调 LLM. 适合"中段改规则/改背景", 下一次点发送时 AI 才会看到.',
        system_mode_hint: 'role=system 下 [发送] 会写入 session.messages + 调 LLM (wire 层自动把本条转写为 role=user + `[system note] ` 前缀避免 provider 过敏); [注入 sys] 仅写入历史不跑 LLM.',
        inject_empty: 'textarea 为空, 无法注入.',
        clock_prefix: '虚拟时间: ',
        clock_unset: '未设置',
        next_turn_prefix: '下一轮 +',
        next_turn_custom: '自定义…',
        next_turn_clear: '清除',
        custom_prompt: '输入时长 (例: 1h30m / 2d / 90s / 纯数字按秒):',
        bad_duration: '时长格式无法解析',
        role_prefix: '角色: ',
        mode_prefix: '模式: ',
        mode: {
          manual: '手动',
          simuser: '假想用户 (SimUser)',
          script: '脚本化 (Scripted)',
          // P12 上线后 script 已启用, 但保留 script_deferred key 以防老代码
          // 引用; 文案里明确注明已启用, 避免误导.
          script_deferred: '脚本化 (Scripted)',
          auto: '双 AI 自动 (Auto)',
          // P13 上线后 auto 已启用; 保留 auto_deferred key 以防老代码引用,
          // 文案同步更新到"已启用"版本, 不再暗示未接入.
          auto_deferred: '双 AI 自动 (Auto)',
          // P09 的单文案值, 保留以便其它模块引用.
          deferred: '(Auto)',
        },
        mode_deferred_hint: '',
        // SimUser (P11) 专用文案. style key 与后端 STYLE_PRESETS 键对齐; 如新增
        // 风格, 这里补 label + 后端预设同步.
        simuser: {
          style_prefix: '风格:',
          style: {
            friendly: '友好',
            curious: '好奇',
            picky: '挑剔',
            emotional: '情绪化',
          },
          persona_toggle: '自定义人设',
          persona_toggle_title: '展开一个文本框, 可在本次会话里临时追加\u300c额外人设/背景\u300d给 SimUser LLM. 留空则只用风格预设.',
          persona_placeholder: '例: 你是一位 30 岁的程序员, 对本次对话话题有专业背景但想从外行视角提问...',
          persona_intro: 'SimUser 在本会话内生效的"额外人设/背景". 不会写回任何持久配置, 切回手动模式或重建会话即清空.',
          generate: '生成',
          generating: '生成中…',
          generate_title: '调用假想用户 LLM 生成一条"下一轮要说的"用户消息草稿, 自动填进左侧 textarea. 不会落盘也不会推进虚拟时钟; 你可以继续编辑后点[发送]以 source=simuser 写入.',
          generate_failed: '假想用户生成失败',
          generated_ok: '假想用户草稿已生成',
          generated_empty: '假想用户返回了空字符串 (可能在扮演\u300c沉默\u300d)',
          confirm_overwrite: '当前 textarea 已有内容, 再次生成会覆盖. 继续?',
        },
        // Scripted (P12) 专用文案. script name / description 由模板 JSON 自带,
        // 这里只放 UI 控件的静态文案.
        script: {
          template_prefix: '剧本:',
          no_template_selected: '— 请选一个剧本 —',
          load: '加载',
          loading: '加载中…',
          unload: '卸载',
          unload_title: '清空当前会话的脚本状态 (不影响已产生的消息).',
          next: '下一轮',
          next_title: '推进一个 user turn: 消费脚本 time 字段推进时钟 → 发给目标 AI → 若紧邻有 role=assistant 的 expected, 自动回填到 AI 回复的 reference_content.',
          next_running: '运行中…',
          run_all: '跑完剩余',
          run_all_title: '循环 [下一轮] 直到脚本末尾或遇到错误. 期间 Chat 输入 / 假想用户均被锁定.',
          run_all_running: '跑完中…',
          stop: '停止',
          stop_title: '中断当前 [下一轮] / [跑完剩余] 运行 — 当前已写入的轮次不会回滚, cursor 停在后端最新确认的位置.',
          stop_toast: '已发送停止信号, 等待后端收尾当前轮…',
          refresh_templates: '刷新列表',
          refresh_title: '重新扫描 dialog_templates 目录 (builtin + user) 并刷新下拉.',
          load_title: '把指定剧本加载到当前会话, 并应用 bootstrap (若会话暂无消息).',
          loaded_toast: (name, count) => `剧本 ${name} 已加载, 共 ${count} 轮.`,
          unloaded_toast: '剧本已卸载.',
          exhausted_toast: '剧本已跑完.',
          exhausted_status: '已跑完',
          progress: (cursor, total) => `${cursor}/${total}`,
          no_template: '当前没有加载剧本, 请先在下拉里选一条并点 [加载].',
          no_session: '先在顶栏新建一个会话, 再加载剧本.',
          load_failed: '剧本加载失败',
          schema_invalid: '剧本 JSON schema 无效',
          not_found: '找不到指定的剧本模板',
          turn_failed: '脚本执行失败',
          bootstrap_skipped_title: '脚本里包含 bootstrap 但会话已有消息, 未重设时钟. 如果需要让 bootstrap 生效, 请先清空对话或重建会话再加载.',
          templates_empty: '(没有可用剧本)',
          source_builtin: '内置',
          source_user: '自定义',
          overriding_builtin: ' (覆盖同名内置)',
          description_prefix: '说明',
          persona_hint_prefix: '角色提示',
          turn_warning_title: '脚本 time 字段警告',
          ref_auto_filled: '脚本 expected 已回填到 AI 回复的 reference_content.',
        },
        // P13 双 AI 自动对话. style key 复用 simuser.style.* (上方已定义),
        // 这里只补模式自身的 UI 文案. Start / Pause / Resume / Stop 文案
        // 以 "控制按钮 + 进度横幅" 两个入口分别使用, 统一收进 auto.*.
        auto: {
          style_prefix: '风格:',
          persona_toggle: '自定义人设',
          persona_toggle_title: '展开 textarea, 为 Auto-Dialog 的 SimUser 一方追加"额外人设/背景"描述. 与 SimUser 模式的人设彼此独立 (切换 mode 不会互相覆盖).',
          persona_placeholder: '例: 你是一位内向的新手用户, 对话题好奇但对技术细节没信心...',
          persona_intro: 'Auto-Dialog 的 SimUser 人设; 仅对 "双 AI 自动" 模式生效, 不会影响手动 SimUser 模式.',
          total_turns_prefix: '轮数:',
          total_turns_title: '要跑的目标 AI 回复次数. 每条 assistant 回复算一轮, 跑完 N 条即结束.',
          step_mode_prefix: '时钟步长:',
          step_mode_title: 'fixed = 每轮前固定推进 step_seconds 秒; off = 整段不动虚拟时钟.',
          step_mode: {
            fixed: '固定',
            off: '不动',
          },
          step_seconds_unit: '秒',
          step_seconds_title: 'fixed 模式下每轮推进的秒数 (1 ~ 604800).',
          start: '启动 Auto',
          starting: '启动中…',
          start_title: '启动双 AI 自动对话: SimUser ↔ Target AI 交替生成 N 轮, 期间消息流与手动发送一致, 可随时通过顶部进度条暂停/停止.',
          running_hint: '运行中 · 见顶部进度条',
          start_failed: '启动失败',
          no_session: '先在顶栏新建一个会话, 再启动 Auto-Dialog.',
          no_style: '请先选一个 SimUser 风格',
          invalid_turns: '轮数必须在 1 ~ 50 之间',
          invalid_step_seconds: 'step_seconds 必须在 1 ~ 604800 之间',
          toast_started: (n, mode) => `Auto-Dialog 已启动, 共 ${n} 轮, 步长: ${mode}.`,
        },
        pending_absolute: (iso) => `Next turn → ${iso}`,
        pending_delta: (label) => `Next turn +${label}`,
        no_session: '先在顶栏新建一个会话.',
        stream_error: '流式发送中断',
        send_failed: '发送失败',
        // P24 §12.5 · virtual clock rewound → ts auto-coerced to preserve monotonicity.
        timestamp_coerced_toast: '消息时间已自动前移, 保持消息时间顺序不倒退',
        timestamp_coerced_detail: (originalTs, coercedTs) =>
          `原时间: ${originalTs} → 调整为: ${coercedTs}. 原因: 虚拟时钟被回退到过去, 系统自动把新消息时间对齐到上一条消息时间避免列表错乱. 详情可在 Diagnostics → Errors 查看 op=timestamp_coerced.`,
        empty_content_toast: '消息内容不能为空',
        // 2026-04-22 Day 8 §13 F3 scope 扩展: Chat 发送命中注入模式时显示
        // advisory toast. 按"检测不改"原则不阻断, 仅告知 + 留审计.
        // 函数叶子用 `_fmt` 后缀 (§3A i18n-fmt-naming).
        //
        // 2026-04-22 验收反馈: 原 detail 文案太长 + 说教, 用户只需知道"检出
        // 了几条". 详细说明 / "检测不改原则"等已经在 Diagnostics → Errors
        // 的 security_filter_hint.prompt_injection 里讲过, 不必在 toast
        // 里重复. toast 只保留"检测到 N 条注入模式" 一行.
        injection_warning_toast_fmt: (n) =>
          `检测到 ${n} 条提示词注入模式`,
      },
      // P13 Auto-Dialog 顶部进度横幅. 只在有活跃 auto_state 时渲染.
      auto_banner: {
        label_running: 'Auto-Dialog 运行中',
        label_paused: 'Auto-Dialog 已暂停',
        label_stopping: '正在停止…',
        progress: (done, total) => `${done}/${total} 轮`,
        step_fixed: (seconds) => `步长 +${seconds}s`,
        step_off: '步长: 不动时钟',
        pause: '暂停',
        resume: '继续',
        stop: '停止',
        pause_title: '当前 step 跑完后不再进入下一轮. 已生成的消息保留.',
        resume_title: '继续跑剩余轮数.',
        stop_title: '停止 Auto-Dialog. 当前 step 结束后立即终止, 已生成的消息保留.',
        pause_failed: '暂停请求失败',
        resume_failed: '继续请求失败',
        stop_failed: '停止请求失败',
        stopped_toast: (reason, done, total) => {
          const reasonText = {
            completed: '正常跑完',
            user_stop: '手动停止',
            error: '出错中止',
          }[reason] || reason;
          return `Auto-Dialog 结束 (${reasonText}): ${done}/${total} 轮`;
        },
        error_title: 'Auto-Dialog 执行错误',
        // P24 Day 7 §12.3.F: 启动期批量配置校验失败的折叠面板.
        error_panel_title_fmt: (n) => `启动失败 · 共 ${n} 条配置错误`,
      },
      preview: {
        heading: 'Prompt 预览',
        refresh_btn: '刷新',
        view: {
          structured: '结构化视图',
          raw: '原始 wire',
        },
        status: {
          not_loaded: '尚未加载',
          click_to_load: '点击"刷新"以加载当前会话的 Prompt 预览.',
          loading: '加载中…',
          loaded: (ts) => `已刷新 @ ${ts}`,
          load_failed: '加载失败',
          no_session: '当前无活跃会话',
          not_ready: '会话尚未就绪',
          dirty: '会话状态有变动, 建议点击"刷新"重新拉取.',
        },
        empty: {
          no_session: '请先在顶栏新建一个会话, 才能预览 Prompt.',
          no_character: '当前会话的 persona 还没填 character_name. 去 Setup → Persona 补全后再回来刷新.',
          no_wire: 'wire_messages 为空 (异常情况, 至少应该有 system 消息).',
          error: '构建 Prompt 预览时出错, 详情见下方.',
        },
        meta: {
          character: '角色',
          master: '主人',
          language: '语言',
          template: '模板',
          template_default: '默认 (自动生成)',
          template_stored: '自定义 (persona)',
          system_chars: 'system 字符数',
          approx_tokens: '估算 tokens',
          virtual_now: '虚拟时间',
        },
        // 结构化视图各分节标题. key 与 PromptBundle.structured 对齐.
        section: {
          session_init:           'session_init (会话起始提示)',
          character_prompt:       'character_prompt (角色 system_prompt)',
          persona_header:         'persona_header (长期记忆标题)',
          persona_content:        'persona_content (长期记忆正文)',
          inner_thoughts_header:  'inner_thoughts_header (内心活动标题)',
          inner_thoughts_dynamic: 'inner_thoughts_dynamic (当前时间注入)',
          recent_history:         'recent_history (最近对话记录)',
          recent_history_empty:   '(无最近对话)',
          time_context:           'time_context (距上次对话提示)',
          holiday_context:        'holiday_context (节日/假期提示)',
          closing:                'closing (context summary ready)',
        },
        hint: {
          structured: '⚠ 本视图仅拆解【首轮初始 system_prompt】的组成 (session_init → character_prompt → persona → inner_thoughts → recent_history → time/holiday → closing), 不含后续轮次的 user / assistant / 注入 system 消息. 要看真正完整发给 AI 的对话流水, 请切到"原始 wire". 各分节独立折叠, Alt+点击 可一次展开/折叠全部.',
          raw: '这是真正送到 AI 的 messages 数组: messages[0] 即首轮初始 system_prompt 的扁平串接 (session_init + character_prompt + memory_flat + closing), 后续每条 user / assistant / 注入 system 都按本轮对话顺序排列. 发送 / 注入 / 编辑消息完成后本视图会自动刷新 (~200ms).',
        },
        length_badge: (n) => `${n} 字符`,
        recent_summary: (n) => `共 ${n} 条`,
        recent_badge: (count, chars) => `${count} 条 / ${chars} 字符`,
        warnings_heading: (n) => `预览提示 (${n})`,
        wire: {
          title: (idx, role) => `messages[${idx}] · ${role}`,
        },
        copy_wire_json: '复制 messages JSON',
        copy_system_string: '复制 system 字符串',
        copied_wire: '已复制 wire_messages JSON',
        copied_system: '已复制 system 字符串',
        // P25 Day 2 polish r4: "最近一次真正发给 AI 的 wire" 视图. 用来
        // 把 external event / auto-dialog 等 ephemeral instruction 路径
        // 发出去的真实 wire 暴露给 tester — 这些路径的 instruction 只存
        // 活一瞬间不入 session.messages, 原先的"从 session.messages 反推"
        // 预览永远看不到.
        last_wire: {
          section_heading: '最近一次真实 wire (ground truth)',
          section_hint: '这是 testbench 最近一次真正发给 LLM 的 wire_messages, 原封不动保留. 和下方"下次 /send 预估 wire"的区别: 预估 wire 是从 session.messages 反推的"如果现在点发送会是什么", 真实 wire 是"上一次 AI 看到了什么"— 外部事件 / auto-dialog 等 ephemeral instruction 路径的 instruction 只会在这里出现, 不会进 session.messages 也就不会进预估 wire. 作为测试平台, 这是 ground truth.',
          none: '本会话还没调用过 LLM. 触发一次 chat.send / 外部事件 / 自动对话 后, 这里会出现真实 wire 快照.',
          meta_source: '来源',
          meta_recorded_at: '记录于',
          meta_reply_chars: 'AI 回复字符数',
          meta_reply_pending: '— (尚未收到回复 / 调用失败)',
          meta_virtual_time: '虚拟时钟',
          meta_note: '备注',
          source_label: {
            'chat.send':            'chat.send (普通发送)',
            avatar_event:           'avatar_event (道具交互)',
            agent_callback:         'agent_callback (后台回调)',
            proactive_chat:         'proactive_chat (主动搭话)',
            auto_dialog_target:     'auto_dialog_target (双 AI · target)',
            auto_dialog_simuser:    'auto_dialog_simuser (双 AI · simuser)',
            'judge.llm':            'judge.llm (评估)',
            'memory.llm':           'memory.llm (记忆提取)',
            simulated_user:         'simulated_user (单独 simuser)',
          },
          next_wire_heading: '下次 /send 预估 wire',
          next_wire_hint: '基于当前 session.messages 反推的"如果现在点发送按钮, wire 会长什么样". 这一节不等于"上一次 AI 看到的 wire", 真正的历史在上方的"最近一次真实 wire".',
          reply_preview_heading: 'AI 回复预览',
          reply_preview_hint: 'LLM 返回的文本. 某些模型 (Gemini) 偶尔会返空字符串 (0 字符) — 如果看到这里是空且 wire 末尾是 role=system, 查 Diagnostics 的 chat_send_system_rewritten / 本面板长条注释.',
          reply_missing: '(本次未拿到回复 — 调用失败或还在传输中)',
          reply_empty: '(LLM 回复为空字符串 — 0 字符, 通常说明 wire shape 异常, 见上方注释)',
        },
        // P25 Day 2 polish r5: "真实 wire (顶) + 预估 wire (底)" 两段面板
        // 合并成单一面板. 新 key 给标题和 ephemeral 提示用;
        // last_wire.* 下面的旧 key (next_wire_heading / reply_* 等) 从此
        // 不再被引用, 保留不报错, 留给后续 diff 清理.
        wire_section: {
          heading_real: '当前 wire',
          heading_estimate: '当前 wire',
          ephemeral_warning: '⚠ 外部事件注入的消息是一次性结构, 仅在本次 LLM 调用时存在, 不会长期进入 session.messages. 对话历史可在左侧聊天区或记忆系统查看.',
          // P25 r7 (2026-04-23): 最近一次 LLM 调用来自非对话域 (例如
          // 记忆合成 / 评分), Chat 页只关心对话 AI 的 wire, 这里不
          // 显示那条 wire, 而是引导 tester 去对应页面找 [预览 prompt].
          // 参数 srcLabel = source 对应的中文标签 (如 "记忆总结 LLM").
          non_chat_source_hint: (srcLabel) =>
            `🔎 最近一次 LLM 调用来自 [${srcLabel}] (非对话 AI). 此面板只显示对话 AI 收到的 prompt. 如需查看该域的 prompt, 请到相关页面 (记忆系统各子页 / 评分 Run 页) 使用 [预览 prompt] 按钮.`,
        },
      },
      // P25 Day 2 — Chat sidebar 下半的 "外部事件模拟" 折叠面板.
      // 三 tab (Avatar / Agent Callback / Proactive), 复现主程序
      // avatar interaction / agent callback / proactive chat 的
      // 语义契约层 (prompt 注入 + memory 写入), 不复现实时流机制.
      external_events: {
        section_title: '外部事件模拟',
        section_hint: '复现主程序 "运行时外部触发 + 临时 prompt 注入 + 写 memory" 三类系统的语义契约 (详见 Settings → About → 外部事件注入详细说明). 每次提交都真实调用目标 AI, 消耗 token; 不复现实时流机制 / 多进程 queue / WebSocket.',
        tab: {
          avatar: 'Avatar 道具',
          agent_callback: 'Agent 回调',
          proactive: '主动搭话',
        },
        common: {
          mirror_to_recent_label: '同时写入 memory/recent.json',
          mirror_to_recent_hint: '默认不勾: 仅写 session.messages, 由 tester 手动触发 recent.compress/facts.extract/reflect 观察下游. 勾上: 语义层额外复现主程序 /cache 独立 memory 路径 (avatar) 或下一轮 user turn 附带 /cache (agent_callback / proactive).',
          invoke_btn: '触发事件',
          invoke_in_flight: '投递中…',
          invoke_in_flight_toast: '事件正在投递, 请稍候 (不要刷新页面, 中途放弃会留部分状态)',
          preview_btn: '预览 prompt',
          preview_in_flight: '预览中…',
          preview_failed_fmt: (msg) => `预览失败: ${msg}`,
          clear_dedupe_btn: '清空去重缓存',
          clear_dedupe_done: (size) => `已清空 avatar 去重缓存 (${size} 条)`,
          clear_dedupe_empty: 'avatar 去重缓存已为空, 无需清理',
          clear_dedupe_failed_fmt: (msg) => `清空失败: ${msg}`,
          invoke_failed_fmt: (msg) => `事件投递失败: ${msg}`,
          invoke_ok_fmt: (kind) => `${kind} 事件已处理`,
          no_session: '需要先新建 / 加载一个会话再触发外部事件.',
          busy: '当前会话正忙 (可能在跑 LLM / 写文件), 请稍后再点.',
        },
        avatar: {
          instruction_integration_hint: 'Instruction preview 展示实际发送给模型的道具事件提示。当前运行时直接发送客观事件事实：道具 / 动作 / 强度 / 附带奖励 / 彩蛋会合并进事件描述，文本上下文只参与 payload 预览和归一化检查，不直接拼入提示词正文.',
          tool_label: '道具',
          tool_option: {
            lollipop: '棒棒糖',
            fist: '猫爪',
            hammer: '锤子',
          },
          action_label: '动作',
          action_option: {
            offer: '第一口 (offer)',
            tease: '第二口 (tease)',
            tap_soft: '连续投喂 (tap_soft)',
            poke: '轻触 (poke)',
            bonk: '锤击 (bonk)',
          },
          intensity_label: '强度',
          intensity_option: {
            normal: '普通 (normal)',
            rapid: '快速 (rapid)',
            burst: '爆发 (burst)',
            easter_egg: '彩蛋 (easter_egg)',
          },
          intensity_unavailable_fmt: (tool, action) =>
            `当前 ${tool}/${action} 组合无可用强度`,
          touch_zone_label: '部位',
          touch_zone_option: {
            ear: '耳朵',
            head: '头',
            face: '脸',
            body: '身体',
          },
          touch_zone_hint: '仅 fist/hammer 写入 prompt; 棒棒糖无部位概念.',
          text_context_label: '文本上下文 (可选, ≤80 字符)',
          text_context_placeholder: '可选, 例: 主人今天好像有点累',
          text_context_too_long: '超过 80 字符的部分会在 _sanitize_avatar_interaction_text_context 里被截断.',
          reward_drop_label: '附带奖励 (reward_drop, 仅 fist 有效)',
          easter_egg_label: '彩蛋动画 (easter_egg)',
          interaction_id_label: 'interaction_id (可选, 留空自动生成)',
          interaction_id_placeholder: '例: evt-manual-0001',
        },
        agent_callback: {
          callbacks_label: '后台回调列表 (每行一条, 至少一条)',
          callbacks_placeholder: '例:\n已完成下载.\n已整理好播放列表.',
          callbacks_empty: '请至少填写一条回调文字',
        },
        proactive: {
          kind_label: '触发类型',
          kind_option: {
            home: 'home (B 站首页 + 微博热搜)',
            screenshot: 'screenshot (桌面截图场景)',
            window: 'window (窗口搜索场景)',
            news: 'news (新闻推荐)',
            community: 'community (喵宇宙社区卡牌)',
            video: 'video (视频推荐)',
            personal: 'personal (用户画像私聊)',
            music: 'music (音乐关键词推荐)',
          },
          topic_label: '主动对话话题 (可选)',
          topic_placeholder: '例: 今日 B 站热搜: 某游戏新 PV / 某主播新动态 / 某微博大 V 发言',
          topic_hint: '会被后端填进 proactive 模板的 {trending_content}/{personal_dynamic}/{current_chat} 占位. 留空 → 后端走占位回退文本 (让 LLM 自行聊起来).',
        },
        result: {
          section_accepted: '已处理',
          section_rejected: '未处理',
          reason_fmt: (code) => `原因: ${code}`,
          reason_label: {
            dedupe_window_hit: '8000ms 去重窗口内命中, 未新建条目',
            invalid_payload: 'payload 校验失败, 无合法 tool/action 组合',
            empty_callbacks: '未提供任何 callback 文本',
            pass_signaled: 'LLM 返回 [PASS], 合法跳过 (不写 message)',
            llm_failed: 'LLM 调用抛异常; 已回滚副作用',
            persona_not_ready: '人设尚未就绪, 先去 Setup → Persona 完成',
            chat_not_configured: 'Chat 模型组未配置, 先去 Settings → Models',
          },
          instruction_heading: 'Instruction 预览 (仅 wire, 不入 session.messages)',
          memory_pair_heading: 'Memory Pair 预览 (成对 user note + assistant reply)',
          persistence_heading: '写入决策',
          persisted_yes: '已写入 session.messages',
          persisted_no: '未写入 session.messages',
          dedupe_summary_fmt: (key, rank, size) =>
            `dedupe_key=${key} · rank=${rank} · 缓存 ${size} 条 (8000ms 窗口)`,
          dedupe_hit: '本次触发命中 8000ms 去重窗口 (rank 已升级或保持)',
          mirror_tri_heading: 'mirror_to_recent 三态',
          mirror_off: '本次未请求 mirror_to_recent',
          mirror_applied: '已同步写入 memory/recent.json',
          mirror_fallback_fmt: (reason) => `请求写 recent.json 但降级未写入: ${reason}`,
          coerce_heading: 'Payload Coerce (静默纠正已 surface)',
          coerce_entry_fmt: (field, requested, applied) =>
            `${field}: ${requested} → ${applied}`,
          reply_heading: 'LLM 回复',
          reply_empty: '(本次未产出 assistant reply)',
          elapsed_fmt: (ms) => `耗时 ${ms}ms`,
        },
        preview_modal: {
          title: '发送前预览 prompt',
          close_btn: '关闭',
          dry_run_hint: '以下为 dry-run 预览: 后端未写入 session.messages / last_llm_wire / 去重缓存, 也未调用 LLM. tester 确认无误后再回面板点 "触发事件" 真正下发.',
          wire_heading: '预览 wire (真实会发给 LLM 的序列)',
          wire_empty: '(本次无 wire 可预览)',
          coerce_heading: 'Payload Coerce 提示',
          reason_fmt: (code) => `预览失败原因: ${code}`,
          copy_wire_btn: '复制 wire JSON',
          copied_wire: '已复制到剪贴板',
          copy_failed: '复制失败 (剪贴板 API 不可用?)',
        },
      },
    },
    settings: {
      nav: {
        models: 'Models 模型',
        api_keys: 'API Keys 密钥',
        providers: 'Providers 服务商',
        autosave: 'Autosave 自动保存',
        ui: 'UI 偏好',
        about: '关于',
      },
      models: {
        heading: '模型配置',
        intro: '四组模型分别用于 目标 AI / 假想用户 / 评分 / 记忆合成. 修改后立刻生效于当前会话, 不会写入磁盘; 保存会话时 api_key 默认脱敏.',
        groups: {
          chat: { title: '目标 AI (chat)', hint: '被测对象, 接收 wire_messages 并输出回复.' },
          simuser: { title: '假想用户 (simuser)', hint: 'SimUser 模式下替测试人员生成 user 消息.' },
          judge: { title: '评分 AI (judge)', hint: '按 ScoringSchema 出评分 JSON. 推荐能力较强的模型.' },
          memory: { title: '记忆合成 (memory)', hint: '压缩 / 反思 / persona 更新的后台 LLM.' },
        },
        fields: {
          provider: '预设服务商',
          provider_manual: '自定义 (手动)',
          base_url: '服务端 base_url',
          api_key: 'API Key',
          model: '模型名',
          temperature: 'Temperature',
          max_tokens: 'Max tokens',
          timeout: '超时 (秒)',
        },
        placeholder: {
          base_url: '如 https://dashscope.aliyuncs.com/compatible-mode/v1',
          api_key: '留空 = 用预设/registry 兜底 (免费预设可留空)',
          model: '如 qwen-plus / gpt-4.1-mini',
          temperature: '留空 = 由模型端自决',
          max_tokens: '留空 = 不限制',
          timeout: '留空 = 60',
        },
        hint: {
          // 三个可选数值字段的行内提示. 明确"留空 = 不发送此参数给模型端"
          // 的语义, 避免用户以为空=0 或空=默认 1.0. 特别点出 o1/gpt-5-thinking
          // 这类拒绝 temperature 的模型必须留空.
          temperature: '留空表示不把 temperature 字段写进请求体 (让模型端用自己的默认值). o1 / o3 / gpt-5-thinking / Claude extended-thinking 等拒绝该参数的模型, 必须留空.',
          max_tokens: '留空表示不限制输出长度, 由模型自行决定; 填正整数则作为硬上限.',
          timeout: '客户端侧 httpx 超时, 不会发送给模型端. 流式长输出建议 ≥ 60s.',
        },
        api_key_status: {
          configured: '已配置 (请求时使用)',
          bundled_by_preset: '此预设内置 API Key (如免费版), 无需填写',
          // 免费 Lanlan 预设在 testbench 里已不可用: 服务端反滥用会拦截外部
          // 客户端 (返回 "STOP ABUSE THE API"), 该预设仅 NEKO 主程序自身可用.
          // 详见 docs/UPSTREAM_SYNC_2026-06.md (2026-06-19 排查结论).
          free_preset_unusable: '注意: 免费预设无法用于 testbench 真实 LLM 调用 (Lanlan 服务端反滥用拦截, 报 "STOP ABUSE THE API"), 仅 NEKO 主程序可用. 要在此跑真实对话/记忆/评分, 请改配付费 provider + API Key.',
          from_preset: name => `将使用 tests/api_keys.json 中的 ${name}`,
          missing: '未配置, 请填入或去 API Keys 页面补',
        },
        toast: {
          switched_manual: '已切换到自定义模式',
          applied: name => `已应用预设: ${name}`,
          applied_free: name => `已应用预设: ${name} (免费版, API Key 自动兜底, 可直接测试)`,
          applied_free_unusable: name => `已应用预设: ${name} — 但免费预设无法用于 testbench 真实 LLM 调用 (服务端反滥用拦截), 仅主程序可用; 真实测试请改配付费 provider + API Key.`,
        },
        buttons: {
          apply_preset: '应用预设',
          test: '测试连接',
          save: '保存',
          revert: '撤销',
          reload_key: '重新读取 API Keys',
        },
        status: {
          saved: '已保存',
          save_failed: '保存失败',
          testing: '测试中…',
          test_ok: latency => `连通 (${latency}ms)`,
          test_failed: '失败',
          not_configured_hint: 'base_url / model 必填; api_key 若留空将使用预设 / registry 兜底',
        },
      },
      api_keys: {
        heading: 'API Keys 状态',
        intro: '本表反映 tests/api_keys.json 各字段是否已填. 不回显明文. 修改本地文件后点"重新读取"即可刷新.',
        path_label: '文件路径',
        path_missing: '(文件不存在, 可从 tests/api_keys.json.template 拷贝生成)',
        last_read: '最后读取',
        reload: '重新读取',
        columns: {
          field: '字段',
          provider: '关联预设',
          status: '状态',
        },
        status_present: '已填',
        status_missing: '未填',
        extra_heading: '额外字段 (本表未列出, 仍会被主 app 使用)',
        load_error_label: '加载错误',
      },
      providers: {
        heading: 'Providers (只读)',
        intro: '读取自 config/api_providers.json → assist_api_providers. 修改请直接编辑 JSON 文件; testbench 不提供写入.',
        columns: {
          key: 'key',
          name: '名称',
          base_url: 'base_url',
          conversation_model: '对话模型',
          summary_model: '摘要模型',
          api_key: 'Key 状态',
        },
        free_tag: '免费',
        // 免费预设在 testbench 已不可用 (Lanlan 反滥用拦截外部客户端), 列表加显著标记.
        free_unusable_tag: '免费·testbench 不可用',
        free_unusable_title: '免费预设仅 NEKO 主程序可用; 从 testbench 直连会被 Lanlan 服务端反滥用拦截 (报 "STOP ABUSE THE API"). 要跑真实 LLM 测试请改用付费 provider + API Key. 详见 docs/UPSTREAM_SYNC_2026-06.md.',
        has_key: '✓',
        no_key: '✗',
      },
      ui: {
        heading: 'UI 偏好',
        intro: '本页设置会实时保存; 语言和主题切换暂未开放, 其余项均可用.',
        language_label: '界面语言',
        language_only_zh: '目前仅支持简体中文.',
        theme_label: '主题',
        theme_dark: '暗色 (默认)',
        theme_light_todo: '浅色 (暂未支持)',
        snapshot_limit_label: '快照内存上限 (条)',
        snapshot_limit_hint: '超过上限的老快照自动转入磁盘冷存; 可随时从 Diagnostics → Snapshots 查看完整时间线. 范围 1 - 500.',
        snapshot_debounce_label: '同类快照合并窗口 (秒)',
        snapshot_debounce_hint: '同一触发源 (比如连续发消息) 在窗口内的快照会被合并, 避免时间线被细碎事件刷屏. 范围 0 - 3600.',
        snapshot_limit_no_session: '需先建立会话才能调整快照配置; 配置仅对当前会话生效 (新建会话回默认值 30 / 5s).',
        // 函数叶子 + `_fmt` 后缀 (§3A i18n-fmt-naming): caller 必须用
        // `i18n(key, ...args)` 调用, 不能用 `{0}`/`{1}` 字面量占位符
        // (i18next 风格 — 本项目自定义 i18n 不支持, 会显示字面量).
        // 2026-04-22 Day 7 验收期用户反馈 "显示 {0} {1}" 的直接成因.
        snapshot_limit_save_ok_fmt: (limit, debounce) =>
          `已更新快照配置: 上限 ${limit} 条, 合并窗口 ${debounce}s`,
        snapshot_limit_save_err_fmt: (msg) => `保存失败: ${msg}`,
        snapshot_limit_invalid: '输入超出合法范围 (上限 1-500, 窗口 0-3600s)',
        // 非法值自动重置模式 — 替代纯报错, 用户不用手动改回默认值.
        snapshot_limit_reset_to_default_fmt: (fieldsList) =>
          `检测到超出合法范围的值, 已自动重置为默认: ${fieldsList}. 请确认后再点保存.`,
        fold_defaults_label: '默认折叠策略',
        fold_defaults_hint: '按内容类型分别设置默认展开/折叠; 选择会立即保存在浏览器本地, 切换会话不影响. 各类型的长度阈值超过时才会折叠, 短内容永远直接展开.',
        fold_defaults_row: {
          chat_message: '聊天长消息',
          log_entry: '诊断日志条目',
          error_entry: '诊断错误条目',
          preview_panel: 'Prompt 预览',
          eval_drawer: '评分详情抽屉',
        },
        fold_defaults_mode: {
          auto: '按长度阈值',
          open: '总是展开',
          closed: '总是折叠',
        },
        fold_defaults_threshold_label: '阈值 (字符)',
        fold_defaults_save_ok: '已更新折叠策略',
        reset_fold: '清除当前会话的 localStorage fold 记录',
        reset_fold_ok: '已清除 fold 键',
      },
      about: {
        heading: '关于 N.E.K.O. Testbench',
        version_label: '版本',
        last_updated_label: '最后更新日期',
        host_label: '监听地址',
        loading: '加载中…',
        limits_heading: '本期声明 (刻意不做的能力)',
        limits: [
          '本期只支持单活跃会话; 多浏览器标签会相互踩状态',
          '仅文本对话, 暂不接入 Realtime / 语音',
          '默认绑定 127.0.0.1, 不监听公网',
          'api_key 在内存中保留明文, 保存到磁盘时自动脱敏',
          '外部事件不做冷却 / 黑名单 / 用户隔离 (有意不做项)',
          'en翻译不做, 当前版本UI仅中文可用',
          '单 LLM 串行, 无并发调用',
        ],
        // Tester 文档入口 — 每条对应 /docs/<name> 端点 (由 health_router
        // 白名单提供). 点击在新标签页打开渲染后的 HTML 版本.
        docs_heading: '相关文档',
        docs_list: [
          { name: '测试用户使用手册 (中文)',  href: '/docs/testbench_USER_MANUAL' },
          { name: '外部事件注入详细说明',      href: '/docs/external_events_guide' },
          { name: '记忆分析导出使用说明',      href: '/docs/memory_export_guide' },
          { name: '版本更新记录 (CHANGELOG)',  href: '/docs/CHANGELOG' },
          { name: '代码与设计总体概述 (给开发者)', href: '/docs/testbench_ARCHITECTURE_OVERVIEW' },
        ],
        // 兜底: 内部开发文档在 docs/ 目录下, 不通过端点开放
        // (blueprint / progress / agent_notes / lessons_learned).
        internal_docs_hint: '内部开发文档 (计划 · 进度 · agent 笔记): tests/testbench/docs/',
      },
    },
    collapsible: {
      expand_all: '展开全部',
      collapse_all: '折叠全部',
      copy: '复制',
      copy_ok: '已复制',
      copy_fail: '复制失败',
      collapse: '折叠',
      length_chars: n => `${n} 字符`,
    },
    toast: {
      close: '关闭',
      dismiss_all: '清除全部',
    },
    // P25 r7 (2026-04-23): shared modal for Memory / Judge [预览 prompt]
    // buttons. Kept flat (not under chat.*) because it's used across
    // workspaces (Memory sub-pages + Evaluation/Run, and can be reused
    // by any future domain that needs "show wire, no LLM call").
    prompt_preview_modal: {
      wire_heading: '预览 wire (真实会发给 LLM 的序列)',
      wire_empty: '(本次无 wire 可预览)',
      copy_wire_btn: '复制 wire JSON',
      copied_wire: '已复制到剪贴板',
      copy_failed: '复制失败 (剪贴板 API 不可用?)',
      close_btn: '关闭',
    },
    session: {
      created: name => `会话已创建: ${name}`,
      destroyed: '会话已销毁',
      no_active: '当前无活跃会话',
      create_failed: '创建会话失败',
      destroy_failed: '销毁会话失败',
      confirm_destroy: '确认销毁当前会话? 沙盒目录将被清空.',
      // P23 — 统一导出模态 (顶栏 Menu / Aggregate / Diagnostics Paths 三入口共用).
      export_modal: {
        title: '导出当前会话',
        scope_heading: '导出范围 (Scope)',
        format_heading: '导出格式 (Format)',
        scope: {
          full: '完整 (full) — 人设 + 记忆 + 对话 + 评分 + 快照元信息',
          persona_memory: '人设 + 记忆 (persona_memory) — 无对话/评分',
          conversation: '对话 (conversation) — 只导出 messages',
          conversation_evaluations: '对话 + 评分 (conversation_evaluations)',
          evaluations: '仅评分 (evaluations) — 无 messages',
        },
        format: {
          json: 'JSON — 结构化, 可 diff / re-import',
          markdown: 'Markdown — 人类可读, 分节报告',
          dialog_template: 'Dialog template — 回流为可重跑的 script schema (仅 conversation)',
        },
        include_memory: '附带 sandbox memory tarball (base64 内嵌)',
        include_memory_hint:
          '仅在 scope=full 或 persona_memory + format=json 时生效. 产物可通过 POST /api/session/import 逆向加载, '
          + '但文件会显著变大 (memory 目录越大差距越悬殊).',
        api_key_redacted_hint:
          'API Key 强制脱敏: 导出产物里的 model_config.api_key 永远是 "<redacted>". '
          + '无"明文导出"开关 — 若要保留真密钥请用 [另存为…] 并手动取消脱敏.',
        filename_label: '目标文件名:',
        export_btn: '导出并下载',
        exporting: '导出中…',
        ok_toast: (name) => `已下载: ${name}`,
        note: {
          full: '产物包含全部会话字段 + snapshot 元信息, 是"我要把整条测试过程交给外部"时的选择.',
          persona_memory: '只把人设 + memory 打包, 给"复用同一套人设在另一组测试里起测"场景用.',
          conversation: '仅保留对话消息 + 必要元信息, 适合给评审看"这段对话发生了什么".',
          conversation_evaluations: '对话 + 评分 + aggregate 统计, 一份"对话+评分"综合报告.',
          evaluations: '仅保留评分结果 + aggregate, 适合跨会话对比 / 只关心评分的分析.',
          dialog_template:
            '把对话回流为 script_runner 兼容的 JSON schema: user turn 的时间差分自动填入 time.advance, '
            + 'assistant turn 的原文作为 expected. 保存到 user dialog_templates/ 目录即可重跑.',
        },
        err: {
          invalid_combo: '当前 scope 与 format 组合不合法. dialog_template 仅对 conversation scope 开放.',
          network: '网络错误, 请检查服务端是否在运行.',
          busy: '会话正忙 (自动保存 / 加载中), 请稍后重试.',
          backend: (msg) => `导出失败: ${msg || '未知错误'}`,
          download: (msg) => `浏览器写盘失败: ${msg || '未知'}`,
        },
      },
      save_modal: {
        title_save: '保存当前会话到存档',
        title_save_as: '另存为新的会话存档',
        name_label: '存档名',
        name_placeholder: '示例: demo_run_01',
        name_required: '必须填写存档名.',
        name_invalid: '存档名只能包含字母/数字/下划线/短横/点, 需以字母或数字开头, 长度 ≤ 64.',
        name_taken: (name) => `存档 "${name}" 已存在; 请换个名字, 或使用 [保存] 进行覆盖.`,
        redact_api_keys: '脱敏 API Key (推荐)',
        redact_hint:
          '勾选后 model_config 中的 api_key 会替换成 "<redacted>", 存档可安全分享. '
          + '取消勾选会明文写入存档, 仅自用且在可信环境下才关掉.',
        confirm_plaintext:
          '确认将 API Key 明文写入存档?\n\n'
          + '只有在你完全掌控该存档的存放/传输路径 (非团队共享) 时才建议这么做. '
          + '一旦存档被分享或泄漏, 文件内的密钥会立即暴露.',
        save_btn: '保存 (覆盖同名)',
        save_as_btn: '另存为',
        ok_toast: (name) => `存档已保存: ${name}`,
        err_toast: '保存失败',
        injection_badge: (hitCount, fieldCount) =>
          `⚠ 检测到 ${hitCount} 处疑似 prompt-injection 模式 (${fieldCount} 个字段). 保存不会过滤, 仅作提示.`,
        injection_tooltip_header:
          '以下字段包含框架检测器识别出的可疑模式 (仍会原样保存):',
      },
      load_modal: {
        title: '会话存档管理',
        refresh: '刷新',
        empty: '尚无任何会话存档. 先新建并使用 [保存到存档] / [另存为…] 生成第一个.',
        list_failed: (msg) => `加载存档列表失败: ${msg || '未知错误'}`,
        row_meta: (savedAt, msgCount, snapCount, evalCount, sizeStr, redactedBadge) =>
          `${savedAt} · ${msgCount} 消息 · ${snapCount} 快照 · ${evalCount} 评分 · ${sizeStr}${
            redactedBadge ? ' · ' + redactedBadge : ''
          }`,
        row_error: (err) => `无法解析存档: ${err}`,
        redacted_badge: 'api_key 已脱敏',
        load_btn: '加载',
        delete_btn: '删除',
        // P24 §3.2 / H2: field-level archive lint (not tarball verify).
        lint_btn: '体检',
        lint_btn_hint: '对存档的 JSON 做字段级校验, 找出结构性错误 / 未知字段 / 旧版本残留 (不触发 memory tarball 完整性校验, 那是 Load 时 memory_hash_verify 的事).',
        lint_clean_fmt: (name) => `存档 "${name}" 的 JSON 结构完整无异常.`,
        lint_has_errors_fmt: (name, errs, warns) =>
          `存档 "${name}" 有 ${errs} 个错误 · ${warns} 个警告 (可能导致加载失败)`,
        lint_has_warnings_fmt: (name, warns) =>
          `存档 "${name}" 有 ${warns} 个警告 (加载仍能工作)`,
        lint_err: '体检请求失败',
        lint_err_prefix: '❌ 错误',
        lint_warn_prefix: '⚠ 警告',
        confirm_load: (name) => `加载存档 "${name}" 会彻底替换当前会话 (页面自动刷新). 确定?`,
        confirm_delete: (name) => `确认删除存档 "${name}"? 此操作不可撤销 (disk-level).`,
        ok_toast: (name) => `正在加载存档: ${name}`,
        err_toast: (name) => `加载存档 "${name}" 失败`,
        // P22.1 G3/G10 + P24 §14A.2 — memory 完整性校验未通过时的提示.
        hash_mismatch_title: '⚠ memory 完整性校验未通过',
        hash_mismatch_detail:
          '存档已载入, 但 memory tar.gz 的内容哈希与保存时记录的不一致. 可能是存档被手动编辑 / 传输过程中损坏 / 跨版本改动导致. 建议先到 Diagnostics → Errors 查看 op=integrity_check 的详情, 核对 memory 内容后再继续.',
        delete_ok: (name) => `已删除存档: ${name}`,
        delete_err: (name) => `删除存档 "${name}" 失败`,
        reload_hint: (name) =>
          `已加载 ${name || '存档'}, 页面将自动刷新以同步所有工作区状态…`,
        import_btn: '导入 JSON…',
        import_hint:
          '可选两种路径: (a) 直接把 .json 文件里的内容粘贴到下方文本框 → 点 [从文件导入 JSON…] 提交; '
          + '(b) 文本框留空 → 点 [从文件导入 JSON…] 打开文件选择器选本机 .json 文件, 自动读取并提交. '
          + '导入后存档落在本机的 saved_sessions/, 再点 [加载] 才会切会话.',
        import_placeholder: '{ "kind": "testbench_session_export", ... }',
        import_name_label: '存档名覆盖 (可选)',
        import_name_placeholder: '留空沿用 payload 里的 name',
        import_overwrite: '允许覆盖同名存档',
        import_back: '← 返回列表',
        import_go: '导入',
        import_ok: (name) => `已导入存档: ${name}`,
        import_err: '导入失败',
        import_parse_err: '粘贴的内容不是合法 JSON',
        // 2026-04-22 Day 8 验收反馈 #4: 重做主按钮 — 智能走文件或粘贴.
        import_go_file: '从文件导入 JSON…',
        import_file_hint: '智能导入: 文本框有内容则直接提交文本框; 文本框为空则打开文件选择器, 选 .json 后自动读取并提交. 无需两步操作.',
        import_file_loaded: (name) => `已读入文件: ${name}`,
        import_file_read_err: '读取文件失败',
      },
      // P22 — 自动保存恢复横幅 (启动时检测到前一次运行的 orphan autosave 就出).
      restore_banner: {
        title: (n) => `检测到上次运行的 ${n} 个会话有未保存的自动备份`,
        body: '可以点 [查看并恢复] 打开恢复面板; 不想恢复点 × 关闭横幅即可 (自动备份保留在磁盘, 可随时从 "恢复自动保存…" 再进).',
        open_btn: '查看并恢复',
        dismiss_hint: '本次运行不再提示 (服务重启后会再出现)',
      },
      // P22 — 自动保存管理模态 (从 topbar dropdown 或 restore banner 进).
      restore_modal: {
        title: '自动保存备份',
        intro:
          '**每个会话独立**保留最近的 rolling slot (current / prev / prev2, 由 Settings → 自动保存 → 保留份数 控制, 最多 3). '
          + '若你在同一服务期间切换过多个会话, 每个会话的 slot 都会在此列出, 所以条目总数可能大于"保留份数". '
          + 'autosave 强制脱敏 api_key, 过期 (默认 24h) 自动清理.',
        refresh: '刷新',
        empty: '暂无自动保存条目. 一旦修改会话内容, 5 秒后会自动写入第一份.',
        list_failed: (msg) => `加载自动保存列表失败: ${msg || '未知错误'}`,
        row_meta: (autosaveAt, msgCount, snapCount, evalCount, sizeStr) =>
          `${autosaveAt} · ${msgCount} 消息 · ${snapCount} 快照 · ${evalCount} 评分 · ${sizeStr}`,
        row_error: (err) => `无法解析: ${err}`,
        title_tooltip: (sessionId, slotLabel, sessionName) =>
          `session_id: ${sessionId}\nslot: ${slotLabel || 'current'}${sessionName ? '\nname: ' + sessionName : ''}`,
        slot_0: '最新',
        slot_1: '上一份',
        slot_2: '更早',
        restore_btn: '恢复',
        delete_btn: '删除',
        clear_all_btn: '清空全部自动保存',
        confirm_restore: (entryId) =>
          `恢复 "${entryId}" 会替换当前会话 (页面自动刷新). 确认?`,
        confirm_delete: '确认删除这条自动保存? 此操作不可撤销.',
        confirm_clear_all:
          '确认清空**所有**自动保存条目? 此操作不可撤销 (pre_load_* 安全备份不受影响).',
        restore_ok: '自动保存恢复完成, 页面将刷新…',
        restore_err: '恢复失败',
        delete_ok: '已删除这条自动保存',
        delete_err: '删除失败',
        clear_all_ok: (n) => `已清空 ${n} 条自动保存`,
        clear_all_err: '清空失败',
      },
      // P22 — Settings → 自动保存 子页.
      autosave_settings: {
        heading: '自动保存 (Autosave)',
        intro:
          '会话状态修改后, 系统会在后台做**防抖式**自动保存: 默认 5s 内无新修改即落盘, '
          + '60s 内有持续修改也会强制落盘一次. **每个会话独立**保留最近 N 份 (最新 / 上一份 / 更早, N 由下方"保留份数"控制, 最多 3). '
          + '多会话并存时, 列表里总条目数 = 会话数 × N, 属于正常现象. '
          + 'autosave 永远脱敏 api_key, 到期 (默认 24h) 自动清理. '
          + '启动时若检测到上次运行的 orphan 条目, 会以横幅提示并支持从列表恢复.',
        status_loading: '读取状态中…',
        status_heading: '当前状态',
        status_fields: {
          enabled: '启用',
          dirty: '待保存',
          last_flush_at: '上次保存',
          last_error: '上次错误',
          last_source: '上次触发源',
          stats: '运行统计',
        },
        status_stats_fmt: (n, f, e, d, l) =>
          `通知 ${n} · 落盘 ${f} · 错误 ${e} · 禁用跳过 ${d} · 锁忙跳过 ${l}`,
        status_none: '未激活 (无当前会话)',
        status_na: '—',
        flush_btn: '立即保存一次',
        flush_ok: '已强制保存当前会话 autosave',
        flush_err: '强制保存失败',
        config_heading: '配置',
        config_fields: {
          enabled: '启用自动保存',
          debounce_seconds: '防抖时长 (秒)',
          debounce_hint: '收到修改后等待这么久没有新修改再落盘; 范围 0.5 ~ 300.',
          force_seconds: '强制落盘上限 (秒)',
          force_hint:
            '持续修改时, 从第一次修改算起超过这么久会强制落盘, 防止"被连续改动拖到永不保存"; 需 ≥ 防抖时长, 上限 3600.',
          rolling_count: '保留份数',
          rolling_hint: '**每个会话独立**保留最近 N 份 (最多 3), 覆盖崩溃时最新文件半写的场景. 多会话场景下总条目数 = 会话数 × N.',
          keep_window_hours: '保留时长 (小时)',
          keep_window_hint:
            '超过此时长的条目在服务启动时自动清理 (范围 1 ~ 720 即 30 天).',
        },
        config_save: '保存配置',
        config_reset: '恢复默认',
        config_saved: '配置已保存',
        config_invalid: (msg) => `配置有误: ${msg}`,
        config_save_err: '保存配置失败',
        open_restore_modal: '打开自动保存管理面板',
        boot_cleanup_hint:
          '注: 配置只影响新一轮 debounce 周期与下次启动时的清理; 已经落盘的条目不会重写.',
      },
    },
    errors: {
      network: '网络请求失败',
      server: code => `服务端错误 (HTTP ${code})`,
      unknown: '未知错误',
    },
    common: {
      ok: '确定',
      cancel: '取消',
      close: '关闭',
      save: '保存',
      loading: '加载中…',
      not_implemented: '尚未实装',
      // P24 §12.3.E #13 — shared [打开文件夹] button (dev_note L15).
      open_folder_btn: '打开文件夹',
      open_folder_hint: {
        generic: '在操作系统的文件管理器里打开对应目录.',
        current_sandbox: '打开当前会话的沙盒目录. 人设 / memory / snapshot 冷存都在里面.',
        user_schemas: '打开自定义评分 schemas 目录 (testbench_data/scoring_schemas). 点 [从文件夹导入] 也是从这里读.',
        user_dialog_templates: '打开自定义剧本目录 (testbench_data/dialog_templates). 可以在这里手动放 / 改 .json 模板文件.',
      },
    },
    model_reminder: {
      dismiss_hint: '本次服务运行期不再提醒 (服务重启后会再出现)',
      welcome: {
        title: '欢迎使用 N.E.K.O. Testbench — 先配置 AI 模型',
        body:
          '新建会话后, chat / simuser / judge / memory 四组 AI 模型默认都是空的, '
          + '后续生成 persona、假想用户回复、对话、记忆摘要、评分都要走 LLM, '
          + '所以请先去 Settings → Models 给至少 chat / simuser / judge 三组配上模型. '
          + '如果你还没填过 provider 的 API Key, 请先编辑 `tests/api_keys.json`, '
          + '或挑一个 `is_free_version` 的免费 provider 直接用.',
        goto_btn: '去 Settings → Models',
      },
      warn: {
        title: '请先配置 AI 模型 API Key',
        body:
          '测试流程 (生成 persona / 假想用户回复 / 对话 / 评分等) 都要调用 LLM. '
          + '当前没有任何可用的 provider — 请编辑 `tests/api_keys.json` 和/或 '
          + '`tests/api_providers.json`, 然后去 Settings → API Keys 点 [Reload] 让后端重读.',
        goto_btn: '去 Settings → API Keys',
      },
    },
    stage: {
      name: {
        persona_setup: '人设准备',
        memory_build: '记忆搭建',
        prompt_assembly: 'Prompt 组装',
        chat_turn: '对话轮次',
        post_turn_memory_update: '轮后记忆更新',
        evaluation: '评分',
      },
      name_short: {
        persona_setup: '人设',
        memory_build: '记忆',
        prompt_assembly: 'Prompt',
        chat_turn: '对话',
        post_turn_memory_update: '记忆更新',
        evaluation: '评分',
      },
      op: {
        persona_edit: {
          label: '去 Setup → Persona 编辑人设',
          description:
            '六阶段里最早的一步: 配置角色名 / 用户名 / 基础人设文本. '
            + '只要人设为空或未保存, system prompt 拼不出来, 后面所有环节都会报 "PreviewNotReady".',
          when_to_run:
            '首次建会话 / 换人设 / 想让 SimUser 有针对性的人设风格时. '
            + '"执行并推进" 会把阶段切到 memory_build 同时跳到 Setup → Persona 子页, 请手动填写并 Save 后再回来点 "执行并推进" 推进到下一阶段.',
          when_to_skip:
            '人设已经填过 (context 面板里 persona_configured=true) / 想跳过使用**空白人设**模拟最原始对话时可以 skip.',
        },
        memory_edit: {
          label: '去 Setup → Memory 填写初始记忆',
          description:
            '给角色配置初始的 recent / facts / reflections / persona 记忆. '
            + '记忆为空时 Lanlan 也能跑对话, 但测试 "记忆影响回复" 这类场景需要先造数据.',
          when_to_run:
            '准备测试记忆召回 / 事实矛盾 / 反思触发等场景时. '
            + '记忆条目数见下方 context 面板 (memory_counts). 已有足够数据时可 skip.',
          when_to_skip:
            '测试零记忆冷启动, 或完全依赖对话运行时动态生成的记忆 (走 post_turn_memory_update 流程)时.',
        },
        prompt_preview: {
          label: '去 Chat 右栏预览 wire messages',
          description:
            '在 Chat workspace 右栏查看**真正要发给 LLM** 的 wire messages, '
            + '确认 system prompt + 历史 + 记忆注入拼接符合预期再发.',
          when_to_run:
            '改完人设 / 记忆 / 模型配置后第一次发消息前**强烈建议看一眼**, '
            + '否则容易把错误 prompt 发出去然后推断 "模型不行".',
          when_to_skip:
            '确信当前拼接逻辑没变过 (只是再发一轮) 时可以直接 skip 到 chat_turn.',
        },
        chat_send: {
          label: '在 Chat 发送一条消息',
          description:
            '任一对话模式均可: 手动 (自己打) / SimUser (AI 模拟用户) / '
            + 'Scripted (加载剧本) / Auto-Dialog (双 AI 自动跑). 每种模式自身也可以无限反复地发.',
          when_to_run:
            '前面几步就绪后正常进入对话阶段. 注意: **点 "执行并推进" 不会自动发消息**, '
            + '只是把阶段切到 post_turn_memory_update; 实际发消息请在 Chat workspace 的 Composer 里操作.',
          when_to_skip:
            '用不上对话 (比如只想测人设加载) 时可 skip, 但通常不会.',
        },
        memory_trigger: {
          label: '去 Setup → Memory 触发一次记忆 op',
          description:
            '对话之后的记忆合并/抽取/反思: recent.compress (压缩历史为摘要) / '
            + 'facts.extract (从对话抽取事实) / reflect (反思) / persona.add_fact / resolve_corrections. '
            + '每个 op 都是 "Trigger → 预览 drawer → Accept" 的确认流程, 绝不自动写盘.',
          when_to_run:
            '对话累积到想要压缩或抽事实时. 下方 context 面板里:'
            + ' messages_count 多 → 可能该 recent.compress;'
            + ' pending_memory_previews 非空 → 已有未 Accept 的 op 预览在等,'
            + ' 先去 Memory 子页处理那几个.',
          when_to_skip:
            '想继续跑对话不做记忆更新时 skip. 注意 skip 只切阶段,'
            + ' 不会清任何现有 pending 预览.',
        },
        evaluation_run: {
          label: '去 Evaluation → Run 跑评分',
          description:
            '去 Evaluation → Run 子页选一个 ScoringSchema (Absolute / Comparative / '
            + '单消息 / 整段对话 四组合任选) + 目标消息 + 必要的 reference, '
            + '然后点 [运行评分]. 结果自动写入 session.eval_results, 同步出现在 '
            + 'Evaluation → Results / Aggregate 子页和 Chat 内联评分徽章里. '
            + '若只想看一次分数不落盘, 用请求体里的 persist=false (或者直接在 API 层调 /api/judge/run).',
          when_to_run:
            '对话积累到想打分时. 下方 context 面板里 messages_count 够多 '
            + '(至少几轮 assistant 回复) 再来; 刚开始的空会话跑评分没有素材.',
          when_to_skip:
            '想继续跑对话不评分时 skip. skip 只切阶段, 不会清已有的评分结果, '
            + '后续可以回退到此阶段再跑.',
        },
      },
      chip: {
        collapsed_prefix: stageShort => `阶段: ${stageShort}`,
        no_session: '阶段: (未建会话)',
        expand_hint: '点击展开 Stage Coach',
        collapse_hint: '点击折叠',
        expanded_default_tip: '本 workspace 下 Stage Coach 默认展开. 点右上 × 可折叠.',
      },
      panel: {
        intro_title: '这是一个帮手, 不是流程警察',
        intro_body:
          'Stage Coach 只是把测试流程做成了一张**可选的 checklist**, '
          + '同时**收集一些数据** (消息数 / 各类记忆条目数 / 虚拟时钟 / '
          + '未确认的 memory op 等) 放在下方"当前上下文"面板里, 帮你判断**现在该做什么/能不能跳过**.\n'
          + '它**不会自动跑任何 op** — 发消息 / 编辑人设 / 跑 memory 压缩 / 评分, '
          + '全都要你自己在对应的 workspace 里点按钮.\n'
          + '"执行并推进" 和 "跳过" **只改阶段指针** (以及跳转到相关子页), '
          + '完全不会动对话记录 / 记忆 / 虚拟时钟; "回退" 同理, 只让你回到某个阶段的 UI 视角, '
          + '既不会撤销已发消息也不会清记忆. 所以随便点, 出错也没副作用.',
        stage_bar_title: '流水线阶段',
        op_card_title: '下一步推荐',
        when_to_run: '什么时候该跑',
        when_to_skip: '什么时候可以跳过',
        context_title: '当前上下文 (帮你判断该不该跑)',
        history_title: '最近动作',
        history_empty: '暂无记录',
      },
      context: {
        messages_count: count => `消息数: ${count}`,
        messages_split: (user, asst) => `(user ${user} · assistant ${asst})`,
        last_message: role =>
          role ? `末尾消息角色: ${role}` : '末尾消息: (无)',
        memory_counts: c =>
          `记忆: recent ${c.recent} · facts ${c.facts}`
          + ` · reflections ${c.reflections} · persona_facts ${c.persona_facts}`,
        persona_configured: ok =>
          ok ? '人设已配置 ✓' : '人设未配置',
        pending_previews_none: '暂无记忆 op 待确认',
        pending_previews: ops => `记忆 op 待确认: ${ops.join(', ')}`,
        script_loaded: ok => ok ? '已加载脚本' : '未加载脚本',
        auto_running: ok => ok ? 'Auto-Dialog 运行中' : 'Auto-Dialog 未运行',
        virtual_now: t => `虚拟时钟: ${t || '(未设)'}`,
        pending_advance: s =>
          s == null ? '无暂存时长' : `下轮暂存推进 ${s} 秒`,
        warnings: ws =>
          ws.length === 0 ? '' : `(采集警告: ${ws.join(', ')})`,
      },
      buttons: {
        preview: '预览 dry-run',
        // P24 §12.3.E #17 (Day 5): non-memory op 的 disabled Preview 按钮
        // 已改为不渲染, 本 hint key 目前无 caller 但保留以备日后恢复.
        preview_disabled_hint:
          '本阶段推荐不提供 stage 层 dry-run: memory op 请去 Setup → Memory 的 Trigger 按钮,'
          + ' 评分请去 Evaluation → Run 子页 (选 schema + 目标后点 [运行评分]).',
        advance: '执行并推进',
        skip: '跳过',
        rewind_open: '回退…',
        rewind_apply: '跳到此阶段',
        go_target: '跳转到目标页',
        collapse: '折叠',
        // 2026-04-22 Day 8 手测 #1: 用户反馈"跳过/回退与直接点阶段节点冗余".
        // 实际差异在 history 标签和 navigate 行为 — 加 tooltip 让 hover 就明白.
        advance_hint:
          '执行并推进: 把阶段指针切到下一阶段 + 跳转到目标页面 (Setup/Chat/Evaluation 等). '
          + 'history 里标 "advance" 代表你顺序走完了这一步. **不会**自动跑任何 op, '
          + '发消息/填人设/跑评分仍需你自己去目标页操作.',
        skip_hint:
          '跳过: 只把阶段指针切到下一阶段, **不跳转页面**, history 里标 "skipped" '
          + '(复盘时能看到"这一步被跳了")或"数据统计"用. 实际行为跟 advance 推进一阶段等价, '
          + '差异仅在 history 标签 + 有无自动 navigate.',
        rewind_hint:
          '回退: 弹窗选一个历史阶段跳回去. **与阶段条上直接点节点等价** — '
          + '只改 UI 视角, 不撤销消息/记忆/虚拟时钟等任何数据. 这个按钮只是提供 chip '
          + '工具栏的 inline 入口, 方便不展开整个 Stage Coach 面板的情况下也能快速回退.',
        track_node_hint:
          '点击跳到此阶段 (与 [回退…] 按钮等价). 纯 UI 视角切换, 不影响任何数据.',
      },
      action: {
        nav_persona: '已跳转到 Setup → Persona, 请填写并保存后回来点 "执行并推进"',
        nav_memory: '已跳转到 Setup → Memory 子页',
        nav_chat_preview: '已跳转到 Chat, 请在右栏确认 wire messages',
        nav_chat_send: '已跳转到 Chat, 请在 Composer 里发送消息',
        nav_evaluation_run: '已跳转到 Evaluation → Run, 请选 schema + 目标消息后点 [运行评分]',
      },
      toast: {
        advance_ok: (from, to) => `阶段推进: ${from} → ${to}`,
        skip_ok: (from, to) => `阶段跳过: ${from} → ${to} (history 标 skipped)`,
        rewind_ok: (from, to) => `阶段回退: ${from} → ${to} (数据未改)`,
        advance_failed: '阶段推进失败',
        busy_fmt: (op) => `会话正忙 (${op}), 请等当前任务完成后再推进阶段`,
        fetch_failed: '读取 stage 状态失败',
        no_session: '请先建会话',
        preview_unsupported: '此 op 不提供 stage 层 dry-run, 详见推荐说明 (memory → Setup/Memory Trigger, 评分 → Evaluation/Run)',
      },
    },
  },
};

let _locale = 'zh-CN';

/** 切换当前语言 (P04 接入 UI; 目前仅接受 zh-CN). */
export function setLocale(locale) {
  if (!I18N[locale]) {
    console.warn(`[i18n] unsupported locale: ${locale}, keeping ${_locale}`);
    return;
  }
  _locale = locale;
}

export function getLocale() {
  return _locale;
}

/**
 * 按点号 key 读取文案. 支持值为函数时直接调用.
 *
 * @param {string} key   `topbar.session.new`
 * @param {...any} args  若字典中的值是函数, 透传这些参数
 * @returns {string}
 */
export function i18n(key, ...args) {
  const dict = I18N[_locale];
  const parts = key.split('.');
  let node = dict;
  for (const p of parts) {
    if (node && typeof node === 'object' && p in node) {
      node = node[p];
    } else {
      console.warn(`[i18n] missing key: ${key}`);
      return key;
    }
  }
  if (typeof node === 'function') {
    try {
      return node(...args);
    } catch (err) {
      console.warn(`[i18n] formatter ${key} threw:`, err);
      return key;
    }
  }
  return node;
}

/** 读对象/数组原值 (供渲染 todo list 等结构化文案). */
export function i18nRaw(key) {
  const dict = I18N[_locale];
  const parts = key.split('.');
  let node = dict;
  for (const p of parts) {
    if (node && typeof node === 'object' && p in node) {
      node = node[p];
    } else {
      console.warn(`[i18n] missing key: ${key}`);
      return null;
    }
  }
  return node;
}

/**
 * 扫描 DOM 节点, 回填 `data-i18n` / `data-i18n-title` / `data-i18n-placeholder`
 * 属性. 同一节点可以用多个属性.
 */
export function hydrateI18n(root = document) {
  for (const el of root.querySelectorAll('[data-i18n]')) {
    el.textContent = i18n(el.dataset.i18n);
  }
  for (const el of root.querySelectorAll('[data-i18n-title]')) {
    el.title = i18n(el.dataset.i18nTitle);
  }
  for (const el of root.querySelectorAll('[data-i18n-placeholder]')) {
    el.placeholder = i18n(el.dataset.i18nPlaceholder);
  }
  for (const el of root.querySelectorAll('[data-i18n-aria-label]')) {
    el.setAttribute('aria-label', i18n(el.dataset.i18nAriaLabel));
  }
}
