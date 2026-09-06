/**
 * chat/external_events_panel.js — Chat 左栏 composer **下方** 的 "外部事件
 * 模拟" 面板 (P25 Day 2; 挂载点由 workspace_chat.js 从 sidebar 迁至 leftPane).
 *
 * 复现主程序三类 "运行时外部触发 + 临时 prompt 注入 + 写 memory" 系统的
 * 语义契约层 (P25_BLUEPRINT §1.2 / §2.6):
 *
 *   - **Avatar**: tester 点 "道具 × 动作 × 强度 (× 部位)" → 发 POST 到
 *     /api/session/external-event kind=avatar. 8000ms 去重 + rank upgrade.
 *   - **Agent Callback**: tester 填后台回调列表 → 发 kind=agent_callback.
 *     Instruction 只入 wire; 反之 LLM 回复 append_message 写 session.
 *   - **Proactive**: tester 选 7 种 trigger kind → 发 kind=proactive.
 *     LLM 可能返回 [PASS] (合法跳过, pass_signaled), 非 [PASS] 走 append.
 *
 * 布局:
 *   - 面板自己是 leftPane 最底部的一个折叠块 (details/summary), **默认
 *     折叠**, 由 tester 主动点 summary 展开 (功能定位: 和 composer 同
 *     "输入性" 控件, 但日常对话不用, 默认不占视觉).
 *     收起状态 summary 高度 ~32px, 不会挤压 message stream.
 *   - 展开后 5 个区:
 *       (a) 3 个 tab 切换 (Avatar / Agent Callback / Proactive)
 *       (b) 当前 tab 的 payload 表单 + [触发事件] / [mirror_to_recent] /
 *           面板右上角 [清空去重缓存] 按钮
 *       (c) Instruction preview (折叠, 注明 "仅 preview 未入 session.messages")
 *       (d) Memory pair preview (仅 avatar 显示)
 *       (e) Persistence decision (persisted / dedupe_info / mirror 三态 /
 *           Payload coerce 黄色警告, coerce 非空时才显示)
 *       (f) LLM reply bubble
 *
 * 关键纪律:
 *   - **Invoke event 是 mutation, 严禁 AbortController** (L19 + §A.8 R6):
 *     in-flight 期间按钮 disabled + 文案换 spinner + toast 提醒; 用户若要
 *     中止必须等 api.js 默认 90s 超时或刷新页面. 四处副作用
 *     (写 messages / 可选写 recent / 写 dedupe cache / 写 diagnostics ring)
 *     中途 abort 会留部分落地状态.
 *   - [清空去重缓存] 是幂等 GET-equivalent (POST 但纯清理), **允许** signal/abort.
 *   - 所有显示文案都走 i18n, 不硬编码中文 (ui-user-input-overflow 规则
 *     仅约束可编辑区, 提示区走常规 i18n).
 *   - `host.__offSessionChange` teardown (L18): mount 时订阅 session:change
 *     清空本面板 state + 重渲染空态, unmount 时 off.
 *
 * 不做的事:
 *   - 不做事件历史/最近 N 次摘要小组件 (§A.8 R2: 若做须同步加 emit 侧 → 本阶段不做).
 *   - 不做 proactive 的 trending_content / use_session_memory_context 等
 *     高级字段 (后端 Day 1 simulate_proactive 只消费 payload.kind, 其它
 *     字段走 build_prompt_bundle 的当前 recent; §A.7 原版就是"只有 kind").
 *   - 不做 proactive "override memory context" textarea (§4.3 "默认隐藏",
 *     Day 1 handler 也没透这个钩子, Day 3 再看是否启用).
 */

import { api } from '../../core/api.js';
import { i18n } from '../../core/i18n.js';
import { toast } from '../../core/toast.js';
import { emit, on, store } from '../../core/state.js';
import { el } from '../_dom.js';

// ─────────────────────────────────────────────────────────────
// Payload 白名单 (和 config/prompts/avatar_interaction_contract.py 对齐)
// ─────────────────────────────────────────────────────────────
//
// L30: 这是 pure helper 的 "表单白名单" 拷贝, 不是逻辑拷贝. 主程序常量
// 表真变了, 面板 dropdown 过滤规则会和后端不同步, 但后端 payload
// 校验 (normalize_avatar_interaction_payload) 会兜底 400. 本文件仅做
// "让 tester 少看不合法的选项"层的引导; 不做独立语义层.
const AVATAR_TOOLS = ['lollipop', 'fist', 'hammer'];
const AVATAR_ACTIONS_BY_TOOL = {
  lollipop: ['offer', 'tease', 'tap_soft'],
  fist: ['poke'],
  hammer: ['bonk'],
};
const AVATAR_INTENSITY_COMBOS = {
  lollipop: {
    offer: ['normal'],
    tease: ['normal'],
    tap_soft: ['rapid', 'burst'],
  },
  fist: {
    poke: ['normal', 'rapid'],
  },
  hammer: {
    bonk: ['normal', 'rapid', 'burst', 'easter_egg'],
  },
};
// 部位仅对 fist / hammer 进入 prompt (主程序
// `_AVATAR_INTERACTION_TOUCH_ZONE_PROMPT_TOOLS`).
const AVATAR_TOUCH_ZONE_TOOLS = new Set(['fist', 'hammer']);
const AVATAR_TOUCH_ZONES = ['ear', 'head', 'face', 'body'];
const PROACTIVE_KINDS = [
  'home',
  'screenshot',
  'window',
  'news',
  'community',
  'video',
  'personal',
  'music',
];
const AVATAR_TEXT_CONTEXT_MAX = 80;

// ─────────────────────────────────────────────────────────────
// mount — 返回 { destroy }
// ─────────────────────────────────────────────────────────────

export function mountExternalEventsPanel(host) {
  host.innerHTML = '';
  host.classList.add('external-events-panel');

  // 本地 state — 不放 global store, 因为面板专属 (参数选择 + 上次结果).
  const state = {
    activeKind: 'avatar',
    mirrorToRecent: false,
    avatar: {
      tool: 'fist',
      action: 'poke',
      intensity: 'normal',
      touchZone: 'head',
      textContext: '',
      rewardDrop: false,
      easterEgg: false,
      interactionId: '',
    },
    agentCallback: {
      callbacksText: '',
    },
    proactive: {
      kind: 'home',
      // P25 Day 2 polish r5 T6: 测试人员可手填 "主动对话话题".
      // 后端 _fill_proactive_instruction 把此字段填入 {trending_content} /
      // {personal_dynamic} / {current_chat} 三个 "主内容" 占位 (哪个生效
      // 由 kind 决定, 但后端不挑, 三槽都替换). 空字符串时后端回落到
      // _PROACTIVE_TOPIC_EMPTY_FALLBACK, 避免 LLM 看到未填的 {trending_content}
      // 字面量.
      topic: '',
    },
    inFlight: false,
    clearingDedupe: false,
    previewing: false,         // r5 T5: "预览 prompt" 按钮 in-flight lock
    lastResult: null,          // SimulationResult-shaped dict
    lastResultKind: null,      // 'avatar' / 'agent_callback' / 'proactive'
    lastRequestError: null,    // string | null (HTTP / 网络层)
  };

  // details/summary 作为折叠容器; 默认折叠 (P25 Day 2 调整).
  // tester 点 summary 才展开; 这样面板默认只占 ~32px 高, 不挤压 message
  // stream. 展开后 .chat-main 的 grid (auto/1fr/auto/auto) 会压缩 stream 1fr.
  const details = el('details', {
    className: 'external-events-details',
    open: false,
  });
  const summary = el('summary', { className: 'external-events-summary' },
    i18n('chat.external_events.section_title'),
  );
  details.append(summary);
  host.append(details);

  const inner = el('div', { className: 'external-events-inner' });
  details.append(inner);

  // ── 一次性渲染 ──────────────────────────────────────────────
  function renderAll() {
    inner.innerHTML = '';

    // Top hint
    inner.append(el('p', { className: 'external-events-hint' },
      i18n('chat.external_events.section_hint'),
    ));

    // 没有活跃 session 时, 所有控件都展示但都 disabled.
    const hasSession = !!store.session?.id;

    if (!hasSession) {
      inner.append(el('div', { className: 'empty-state' },
        i18n('chat.external_events.common.no_session'),
      ));
      return;
    }

    inner.append(renderTabBar());
    inner.append(renderPayloadForm());
    inner.append(renderInvokeRow());
    inner.append(renderClearDedupeRow());
    const resultBlock = renderResultBlock();
    if (resultBlock) inner.append(resultBlock);
  }

  function renderTabBar() {
    const bar = el('div', { className: 'external-events-tabs' });
    for (const kind of ['avatar', 'agent_callback', 'proactive']) {
      const active = state.activeKind === kind;
      const btn = el('button', {
        type: 'button',
        className: 'tab-btn' + (active ? ' active' : ''),
        disabled: state.inFlight,
        onClick: () => {
          if (state.activeKind === kind) return;
          state.activeKind = kind;
          renderAll();
        },
      }, i18n(`chat.external_events.tab.${kind}`));
      bar.append(btn);
    }
    return bar;
  }

  function renderPayloadForm() {
    const wrap = el('div', { className: 'external-events-form' });
    if (state.activeKind === 'avatar') {
      wrap.append(...renderAvatarFields());
    } else if (state.activeKind === 'agent_callback') {
      wrap.append(...renderAgentCallbackFields());
    } else {
      wrap.append(...renderProactiveFields());
    }
    return wrap;
  }

  // ── Avatar 表单 ───────────────────────────────────────────
  function renderAvatarFields() {
    const rows = [];
    const a = state.avatar;

    // 开头一行 hint: avatar prompt 只发送直接事件事实。
    // reward_drop/easter_egg 通过事件事实体现；text_context 仅为历史 payload
    // 兼容和诊断预览保留，不发送给模型，也不参与运行时反应。
    rows.push(el('p', { className: 'form-hint' },
      i18n('chat.external_events.avatar.instruction_integration_hint'),
    ));

    // tool
    const toolSel = buildSelect({
      options: AVATAR_TOOLS.map((t) => ({
        value: t,
        label: `${t} · ${i18n(`chat.external_events.avatar.tool_option.${t}`)}`,
      })),
      value: a.tool,
      onChange: (v) => {
        a.tool = v;
        // action 跟着 reset 到第一个合法.
        const firstAction = AVATAR_ACTIONS_BY_TOOL[a.tool]?.[0] || '';
        a.action = firstAction;
        // intensity reset.
        const firstInt =
          (AVATAR_INTENSITY_COMBOS[a.tool]?.[a.action] || [])[0] || 'normal';
        a.intensity = firstInt;
        renderAll();
      },
    });
    rows.push(labeledRow('chat.external_events.avatar.tool_label', toolSel));

    // action (根据 tool 过滤)
    const allowedActions = AVATAR_ACTIONS_BY_TOOL[a.tool] || [];
    const actSel = buildSelect({
      options: allowedActions.map((act) => ({
        value: act,
        label: i18n(`chat.external_events.avatar.action_option.${act}`),
      })),
      value: a.action,
      onChange: (v) => {
        a.action = v;
        const firstInt =
          (AVATAR_INTENSITY_COMBOS[a.tool]?.[a.action] || [])[0] || 'normal';
        a.intensity = firstInt;
        renderAll();
      },
    });
    rows.push(labeledRow('chat.external_events.avatar.action_label', actSel));

    // intensity (根据 tool+action 过滤)
    const allowedIntensities =
      AVATAR_INTENSITY_COMBOS[a.tool]?.[a.action] || [];
    let intSel;
    if (allowedIntensities.length) {
      intSel = buildSelect({
        options: allowedIntensities.map((i) => ({
          value: i,
          label: i18n(`chat.external_events.avatar.intensity_option.${i}`),
        })),
        value: a.intensity,
        onChange: (v) => { a.intensity = v; },
      });
      rows.push(labeledRow(
        'chat.external_events.avatar.intensity_label', intSel));
    } else {
      const note = el('span', { className: 'form-hint warn' },
        i18n('chat.external_events.avatar.intensity_unavailable_fmt',
          a.tool, a.action),
      );
      rows.push(labeledRow(
        'chat.external_events.avatar.intensity_label', note));
    }

    // touch_zone (仅 fist/hammer 启用; 其它 tool 显示禁用 + hint)
    const touchEnabled = AVATAR_TOUCH_ZONE_TOOLS.has(a.tool);
    const zoneSel = buildSelect({
      options: AVATAR_TOUCH_ZONES.map((z) => ({
        value: z,
        label: i18n(`chat.external_events.avatar.touch_zone_option.${z}`),
      })),
      value: a.touchZone,
      onChange: (v) => { a.touchZone = v; },
      disabled: !touchEnabled,
    });
    // r5 polish r6 T4: label + select 同行 (与 tool/action/intensity 保持一致),
    // hint 独立一行放在下方. 之前把 select+hint 包成 form-col 再喂给 block row
    // 会让 label 独占一行, 视觉节奏和同一 tab 其它字段不一致.
    rows.push(labeledRow(
      'chat.external_events.avatar.touch_zone_label', zoneSel));
    rows.push(el('p', { className: 'form-hint form-hint-standalone' },
      i18n('chat.external_events.avatar.touch_zone_hint')));

    // text_context
    const textarea = el('textarea', {
      className: 'external-events-textarea',
      rows: 2,
      maxLength: AVATAR_TEXT_CONTEXT_MAX,
      placeholder: i18n('chat.external_events.avatar.text_context_placeholder'),
      value: a.textContext,
      onInput: (e) => { a.textContext = e.target.value; },
    });
    // text_context textarea 是多行输入, row 走 block (label 独占一行,
    // textarea 在下 width:100%).
    rows.push(labeledRow(
      'chat.external_events.avatar.text_context_label', textarea, { block: true }));

    // reward_drop (仅 fist)
    if (a.tool === 'fist') {
      rows.push(labeledCheckbox({
        labelKey: 'chat.external_events.avatar.reward_drop_label',
        checked: a.rewardDrop,
        onChange: (v) => { a.rewardDrop = v; },
      }));
    }

    // easter_egg
    rows.push(labeledCheckbox({
      labelKey: 'chat.external_events.avatar.easter_egg_label',
      checked: a.easterEgg,
      onChange: (v) => { a.easterEgg = v; },
    }));

    // interaction_id
    const idInput = el('input', {
      type: 'text',
      className: 'external-events-input',
      placeholder: i18n('chat.external_events.avatar.interaction_id_placeholder'),
      value: a.interactionId,
      onInput: (e) => { a.interactionId = e.target.value; },
    });
    rows.push(labeledRow(
      'chat.external_events.avatar.interaction_id_label', idInput));

    return rows;
  }

  // ── Agent Callback 表单 ────────────────────────────────────
  function renderAgentCallbackFields() {
    const ac = state.agentCallback;
    const textarea = el('textarea', {
      className: 'external-events-textarea',
      rows: 4,
      placeholder: i18n('chat.external_events.agent_callback.callbacks_placeholder'),
      value: ac.callbacksText,
      onInput: (e) => { ac.callbacksText = e.target.value; },
    });
    // callbacks textarea 是多行输入 (一行一条 callback), row 走 block.
    return [labeledRow(
      'chat.external_events.agent_callback.callbacks_label', textarea, { block: true })];
  }

  // ── Proactive 表单 ─────────────────────────────────────────
  function renderProactiveFields() {
    const p = state.proactive;
    const rows = [];

    const kindSel = buildSelect({
      options: PROACTIVE_KINDS.map((k) => ({
        value: k,
        label: i18n(`chat.external_events.proactive.kind_option.${k}`),
      })),
      value: p.kind,
      onChange: (v) => { p.kind = v; },
    });
    rows.push(labeledRow(
      'chat.external_events.proactive.kind_label', kindSel));

    // r5 T6: 主动对话话题 — 对应后端 payload.topic, 后端 builder 把它
    // 填进模板的 {trending_content} / {personal_dynamic} / {current_chat}
    // 三个主内容占位. 空串时后端有 fallback, tester 不填也不会崩.
    const topicTextarea = el('textarea', {
      className: 'external-events-textarea',
      rows: 2,
      placeholder: i18n('chat.external_events.proactive.topic_placeholder'),
      value: p.topic,
      onInput: (e) => { p.topic = e.target.value; },
    });
    rows.push(labeledRow(
      'chat.external_events.proactive.topic_label', topicTextarea, { block: true }));

    rows.push(el('p', { className: 'form-hint' },
      i18n('chat.external_events.proactive.topic_hint')));

    return rows;
  }

  // ── [预览 prompt] + [触发事件] + [mirror_to_recent] 行 ───
  function renderInvokeRow() {
    const row = el('div', { className: 'external-events-invoke-row' });

    const mirrorChk = el('input', {
      type: 'checkbox',
      checked: state.mirrorToRecent,
      disabled: state.inFlight,
      onChange: (e) => { state.mirrorToRecent = !!e.target.checked; },
    });
    const mirrorLabel = el('label', {
      className: 'diag-checkbox-label',
      title: i18n('chat.external_events.common.mirror_to_recent_hint'),
    },
      mirrorChk,
      el('span', {},
        i18n('chat.external_events.common.mirror_to_recent_label')),
    );

    // r5 T5: 预览按钮 — 在 tester 点 "触发事件" 之前, 先 dry-run 看
    // "这次如果点下去会给 LLM 发什么 wire". 后端 /preview endpoint 不
    // 写 session.messages / last_llm_wire / dedupe cache, 不调 LLM.
    const previewBtn = el('button', {
      type: 'button',
      className: 'btn',
      disabled: state.inFlight || state.previewing,
      onClick: onPreviewClicked,
    }, state.previewing
        ? i18n('chat.external_events.common.preview_in_flight')
        : i18n('chat.external_events.common.preview_btn'));

    const invokeBtn = el('button', {
      type: 'button',
      className: 'btn primary',
      disabled: state.inFlight || state.previewing,
      onClick: onInvokeClicked,
    }, state.inFlight
        ? i18n('chat.external_events.common.invoke_in_flight')
        : i18n('chat.external_events.common.invoke_btn'));

    const spacer = el('span', { className: 'external-events-invoke-spacer' });
    row.append(mirrorLabel, spacer, previewBtn, invokeBtn);
    return row;
  }

  // ── [清空去重缓存] 行 (只对 avatar 有实际意义, 但三类 tab 都可用) ──
  function renderClearDedupeRow() {
    const row = el('div', { className: 'external-events-clear-dedupe-row' });
    const btn = el('button', {
      type: 'button',
      className: 'ghost tiny',
      disabled: state.clearingDedupe,
      onClick: onClearDedupeClicked,
    }, i18n('chat.external_events.common.clear_dedupe_btn'));
    row.append(btn);
    return row;
  }

  // ── 结果区 ──────────────────────────────────────────────
  function renderResultBlock() {
    if (state.lastRequestError) {
      return el('div', { className: 'external-events-request-error' },
        i18n('chat.external_events.common.invoke_failed_fmt',
          state.lastRequestError),
      );
    }
    const r = state.lastResult;
    if (!r) return null;

    const wrap = el('div', { className: 'external-events-result' });

    // Header: accepted / rejected + kind badge + elapsed_ms
    const headerCls =
      'external-events-result-header ' +
      (r.accepted ? 'accepted' : 'rejected');
    const kindBadge = el('span', { className: 'external-events-kind-badge' },
      state.lastResultKind || '');
    const elapsed = el('span', { className: 'external-events-elapsed' },
      i18n('chat.external_events.result.elapsed_fmt', r.elapsed_ms || 0));
    const accepted = el('div', { className: headerCls },
      el('span', { className: 'external-events-result-label' },
        r.accepted
          ? i18n('chat.external_events.result.section_accepted')
          : i18n('chat.external_events.result.section_rejected'),
      ),
      kindBadge,
      elapsed,
    );
    wrap.append(accepted);

    // Reason
    if (r.reason) {
      const codeLabel = i18n(
        `chat.external_events.result.reason_label.${r.reason}`,
      );
      const line = el('div', { className: 'external-events-reason-line' },
        i18n('chat.external_events.result.reason_fmt', r.reason),
      );
      if (codeLabel && codeLabel !== `chat.external_events.result.reason_label.${r.reason}`) {
        line.append(el('span', { className: 'external-events-reason-human' },
          ' · ' + codeLabel));
      }
      wrap.append(line);
    }

    // Instruction preview
    if (r.instruction) {
      const d = el('details', { className: 'external-events-instruction' });
      d.append(el('summary', {},
        i18n('chat.external_events.result.instruction_heading')));
      d.append(el('pre', { className: 'mono' }, r.instruction));
      wrap.append(d);
    }

    // Memory pair preview (仅 avatar)
    if (state.lastResultKind === 'avatar'
        && Array.isArray(r.memory_pair) && r.memory_pair.length) {
      const mem = el('div', { className: 'external-events-memory-pair' });
      mem.append(el('div', { className: 'diag-entry-section-label' },
        i18n('chat.external_events.result.memory_pair_heading')));
      for (const msg of r.memory_pair) {
        const role = msg?.role || 'user';
        const contentText = extractMessageText(msg);
        mem.append(el('div', {
          className: `external-events-memory-row role-${role}`,
        },
          el('span', { className: 'external-events-memory-role' }, role),
          el('span', { className: 'external-events-memory-body' }, contentText),
        ));
      }
      wrap.append(mem);
    }

    // Persistence decision
    const persistence = el('div', { className: 'external-events-persistence' });
    persistence.append(el('div', { className: 'diag-entry-section-label' },
      i18n('chat.external_events.result.persistence_heading')));
    persistence.append(el('div', { className: 'external-events-persisted' },
      r.persisted
        ? i18n('chat.external_events.result.persisted_yes')
        : i18n('chat.external_events.result.persisted_no'),
    ));
    const dd = r.dedupe_info;
    if (dd && typeof dd === 'object') {
      // 后端返回 shape (见 pipeline/external_events.py::simulate_avatar_interaction):
      //   { hit, cache_size, cache_size_before?, dedupe_key, dedupe_rank }
      // 故意不展示未知字段, 只渲染实际存在的摘要.
      const key = dd.dedupe_key || '—';
      const size = dd.cache_size ?? 0;
      const rank = dd.dedupe_rank ?? 0;
      persistence.append(el('div', { className: 'external-events-dedupe-line' },
        i18n('chat.external_events.result.dedupe_summary_fmt',
          key, rank, size),
      ));
      if (dd.hit) {
        persistence.append(el('div', { className: 'external-events-dedupe-hit' },
          i18n('chat.external_events.result.dedupe_hit')));
      }
    }
    // mirror 三态
    const mi = r.mirror_to_recent_info || { requested: false, applied: false };
    persistence.append(renderMirrorBadge(mi));
    wrap.append(persistence);

    // Coerce (仅非空显示)
    if (Array.isArray(r.coerce_info) && r.coerce_info.length) {
      const coerce = el('div', { className: 'external-events-coerce' });
      coerce.append(el('div', { className: 'diag-entry-section-label warn' },
        i18n('chat.external_events.result.coerce_heading')));
      for (const c of r.coerce_info) {
        coerce.append(el('div', { className: 'external-events-coerce-entry' },
          i18n('chat.external_events.result.coerce_entry_fmt',
            c.field,
            safeJson(c.requested),
            safeJson(c.applied),
          ),
          c.note ? el('span', { className: 'external-events-coerce-note' },
            ' — ' + c.note) : null,
        ));
      }
      wrap.append(coerce);
    }

    // LLM reply
    const replyBlock = el('div', { className: 'external-events-reply' });
    replyBlock.append(el('div', { className: 'diag-entry-section-label' },
      i18n('chat.external_events.result.reply_heading')));
    if (r.assistant_reply) {
      replyBlock.append(el('div', { className: 'external-events-reply-body' },
        r.assistant_reply));
    } else {
      replyBlock.append(el('div', { className: 'external-events-reply-empty' },
        i18n('chat.external_events.result.reply_empty')));
    }
    wrap.append(replyBlock);

    return wrap;
  }

  function renderMirrorBadge(mi) {
    const heading = el('div', { className: 'diag-entry-section-label' },
      i18n('chat.external_events.result.mirror_tri_heading'));
    let body;
    if (!mi.requested) {
      body = el('div', { className: 'external-events-mirror mirror-off' },
        i18n('chat.external_events.result.mirror_off'));
    } else if (mi.applied) {
      body = el('div', { className: 'external-events-mirror mirror-applied' },
        i18n('chat.external_events.result.mirror_applied'));
    } else {
      body = el('div', { className: 'external-events-mirror mirror-fallback' },
        i18n('chat.external_events.result.mirror_fallback_fmt',
          mi.fallback_reason || '—'));
    }
    const wrap = el('div', { className: 'external-events-mirror-wrap' });
    wrap.append(heading, body);
    return wrap;
  }

  // ── Actions ───────────────────────────────────────────────
  async function onInvokeClicked() {
    if (state.inFlight) return;
    // session busy guard — 前端先自检, 后端 409 再兜底.
    const st = store.session?.state;
    if (st && st !== 'idle') {
      toast.info(i18n('chat.external_events.common.busy'));
      return;
    }

    const body = buildRequestBody();
    if (!body) return; // buildRequestBody 内部已 toast

    state.inFlight = true;
    state.lastRequestError = null;
    renderAll();

    toast.info(i18n('chat.external_events.common.invoke_in_flight_toast'));

    // **不** 传 signal (L19 + §A.8 R6: mutation 严禁 abort).
    const resp = await api.post(
      '/api/session/external-event',
      body,
      {
        expectedStatuses: [400, 404, 409, 422, 500],
      },
    );

    state.inFlight = false;
    if (resp.ok) {
      state.lastResult = resp.data || null;
      state.lastResultKind = state.activeKind;
      if (state.lastResult?.accepted) {
        toast.ok(i18n('chat.external_events.common.invoke_ok_fmt',
          state.activeKind));
        // P25 Day 2 polish hotfix: 通告 message_stream / preview_panel 等
        // 订阅者, "后端刚带外写入了 session.messages, 请自查刷新".
        // reason='external_event' 是个新白名单值, message_stream 在 subscribe
        // 端专门识别这个 reason 才真 refresh (见 message_stream.js 的
        // offMessagesChanged 订阅处 comment). 主 /chat/send 走 SSE 不过这
        // 条路, 避免把正在流的 delta DOM 节点抹掉.
        emit('chat:messages_changed', { reason: 'external_event' });
      }
    } else {
      state.lastResult = null;
      state.lastResultKind = null;
      state.lastRequestError =
        resp.error?.message || `HTTP ${resp.status}`;
      toast.err(i18n('chat.external_events.common.invoke_failed_fmt',
        state.lastRequestError));
    }
    renderAll();
  }

  // r5 T5: 预览 prompt — dry-run, 不写 session, 不调 LLM.
  async function onPreviewClicked() {
    if (state.previewing || state.inFlight) return;
    const sid = store.session?.id;
    if (!sid) return;

    // preview 允许 "空 callbacks" / "空 topic" 等部分 payload — tester
    // 的用意就是看 "就算我不填这个字段, 会发出去什么". 这里不走
    // buildRequestBody() 的 toast 门禁, 而是直接拼最小 payload.
    const body = buildPreviewBody();
    state.previewing = true;
    renderAll();

    const resp = await api.post(
      '/api/session/external-event/preview',
      body,
      {
        expectedStatuses: [400, 404, 409, 422, 500],
      },
    );

    state.previewing = false;
    renderAll();

    if (!resp.ok) {
      const msg = resp.error?.message || `HTTP ${resp.status}`;
      toast.err(i18n('chat.external_events.common.preview_failed_fmt', msg));
      return;
    }
    openPreviewModal(resp.data || {});
  }

  // Preview 用的 body: 和 buildRequestBody 结构相同, 但不做 "必填校验"
  // (空 callbacks 走后端 reason=empty_callbacks 路径, UI 显示为准).
  function buildPreviewBody() {
    if (state.activeKind === 'avatar') {
      const a = state.avatar;
      const payload = {
        interaction_id: a.interactionId?.trim() || genInteractionId(),
        target: 'avatar',
        tool_id: a.tool,
        action_id: a.action,
      };
      if (a.intensity) payload.intensity = a.intensity;
      if (AVATAR_TOUCH_ZONE_TOOLS.has(a.tool) && a.touchZone) {
        payload.touch_zone = a.touchZone;
      }
      if (a.textContext) payload.text_context = a.textContext;
      if (a.tool === 'fist' && a.rewardDrop) payload.reward_drop = true;
      if (a.easterEgg) payload.easter_egg = true;
      return { kind: 'avatar', payload };
    }
    if (state.activeKind === 'agent_callback') {
      const lines = state.agentCallback.callbacksText
        .split('\n').map((s) => s.trim()).filter(Boolean);
      return { kind: 'agent_callback', payload: { callbacks: lines } };
    }
    // proactive
    return {
      kind: 'proactive',
      payload: {
        kind: state.proactive.kind,
        topic: state.proactive.topic || '',
      },
    };
  }

  async function onClearDedupeClicked() {
    if (state.clearingDedupe) return;
    const sid = store.session?.id;
    if (!sid) return;
    state.clearingDedupe = true;
    renderAll();
    const resp = await api.post(
      '/api/session/external-event/dedupe-reset',
      {},
      {
        expectedStatuses: [404, 409, 500],
      },
    );
    state.clearingDedupe = false;
    if (resp.ok) {
      const cleared = resp.data?.cleared ?? 0;
      if (cleared > 0) {
        toast.ok(i18n('chat.external_events.common.clear_dedupe_done',
          cleared));
      } else {
        toast.info(i18n('chat.external_events.common.clear_dedupe_empty'));
      }
    } else {
      toast.err(i18n('chat.external_events.common.clear_dedupe_failed_fmt',
        resp.error?.message || `HTTP ${resp.status}`));
    }
    renderAll();
  }

  function buildRequestBody() {
    if (state.activeKind === 'avatar') {
      const a = state.avatar;
      const payload = {
        interaction_id: a.interactionId?.trim()
          || genInteractionId(),
        target: 'avatar',
        tool_id: a.tool,
        action_id: a.action,
      };
      if (a.intensity) payload.intensity = a.intensity;
      if (AVATAR_TOUCH_ZONE_TOOLS.has(a.tool) && a.touchZone) {
        payload.touch_zone = a.touchZone;
      }
      if (a.textContext) payload.text_context = a.textContext;
      if (a.tool === 'fist' && a.rewardDrop) payload.reward_drop = true;
      if (a.easterEgg) payload.easter_egg = true;
      return {
        kind: 'avatar',
        payload,
        mirror_to_recent: state.mirrorToRecent,
      };
    }

    if (state.activeKind === 'agent_callback') {
      const lines = state.agentCallback.callbacksText
        .split('\n').map((s) => s.trim()).filter(Boolean);
      if (!lines.length) {
        toast.info(i18n('chat.external_events.agent_callback.callbacks_empty'));
        return null;
      }
      return {
        kind: 'agent_callback',
        payload: { callbacks: lines },
        mirror_to_recent: state.mirrorToRecent,
      };
    }

    // proactive
    return {
      kind: 'proactive',
      payload: { kind: state.proactive.kind },
      mirror_to_recent: state.mirrorToRecent,
    };
  }

  // ── Session teardown: L18 防 state listener 泄漏 ──────────
  const offSessionChange = on('session:change', () => {
    // 会话切换 → 清空上次结果 + 重渲染; 现有 payload 输入保留 (tester 常
    // 想在新 session 跑同样 payload).
    state.lastResult = null;
    state.lastResultKind = null;
    state.lastRequestError = null;
    state.inFlight = false;
    state.clearingDedupe = false;
    renderAll();
  });
  host.__offSessionChange = offSessionChange;

  renderAll();

  return {
    destroy() {
      try { offSessionChange?.(); } catch (_) { /* ignore */ }
      host.innerHTML = '';
      host.classList.remove('external-events-panel');
    },
  };
}

// ─────────────────────────────────────────────────────────────
// helpers
// ─────────────────────────────────────────────────────────────

/**
 * Label + control 一行容器.
 *
 * 默认 (opts.block === false) 渲染成 "label: [control]" 水平并列 —
 * CSS 层 `.external-events-form .form-row` 默认已是 row + wrap +
 * align-items:center (2026-04-23 r3 起, 对齐项目 Virtual Clock 基准).
 *
 * 长控件 (textarea / 多行输入) 应传 `{block: true}` 切回 column — 配合
 * `.form-row.block` 让 label 独占一行, 控件在下占满宽度.
 *
 * LESSONS_LEARNED §1.6 (语义契约 vs 运行时机制): 这里 "短控件" 和 "长
 * 控件" 是 UI 语义层的契约, 不是组件库层的强校验 — 调用点自觉传对.
 */
function labeledRow(labelKey, child, opts = {}) {
  const cls = 'form-row' + (opts.block ? ' block' : '');
  return el('div', { className: cls },
    el('label', { className: 'form-label' }, i18n(labelKey)),
    child,
  );
}

function labeledCheckbox({ labelKey, checked, onChange }) {
  const chk = el('input', {
    type: 'checkbox',
    checked,
    onChange: (e) => onChange(!!e.target.checked),
  });
  return el('label', { className: 'diag-checkbox-label form-row inline' },
    chk,
    el('span', {}, i18n(labelKey)),
  );
}

function buildSelect({ options, value, onChange, disabled = false }) {
  const sel = el('select', {
    className: 'tiny',
    disabled,
    onChange: (e) => onChange(e.target.value),
  });
  for (const o of options) {
    const opt = el('option', { value: o.value }, o.label);
    if (o.value === value) opt.selected = true;
    sel.append(opt);
  }
  return sel;
}

function extractMessageText(msg) {
  if (!msg) return '';
  const c = msg.content;
  if (typeof c === 'string') return c;
  if (Array.isArray(c)) {
    const first = c[0];
    if (first && typeof first === 'object' && 'text' in first) {
      return String(first.text || '');
    }
  }
  return '';
}

function safeJson(v) {
  if (typeof v === 'string') return JSON.stringify(v);
  if (v === null || v === undefined) return String(v);
  try { return JSON.stringify(v); } catch { return String(v); }
}

function genInteractionId() {
  // 本地最小唯一 id; 后端不看这个字符串的结构, 只看存在性.
  const ts = Date.now().toString(36);
  const rnd = Math.random().toString(36).slice(2, 7);
  return `ui-${ts}-${rnd}`;
}

/**
 * r5 T5: 弹 "预览 prompt" 模态框.
 *
 * 数据来自 POST /api/session/external-event/preview, 后端字段 (见
 * external_event_router.py::post_external_event_preview):
 *   - kind: avatar | agent_callback | proactive
 *   - wire_preview: [{role, content}, ...]   — 真实将会发给 LLM 的 wire
 *   - instruction_final: str                 — wire 末尾 user 条的 content
 *   - instruction_template_raw: str          — 模板源 (未做 slot 替换前)
 *   - coerce_info: [{field, requested, applied, note}, ...]
 *   - reason / error_code / error_message    — 发不出去时的原因
 *
 * Modal 只做展示 + 关闭, 不做二次编辑/提交; tester 看完 prompt 后
 * 回 panel 按 "触发事件" 才真的跑. 这样保持 "预览 ≠ 触发" 干净分离.
 */
function openPreviewModal(data) {
  const backdrop = el('div', {
    className: 'modal-backdrop external-events-preview-modal',
  });
  const dialog = el('div', { className: 'modal' });

  const head = el('div', { className: 'modal-head' },
    el('h3', {}, i18n('chat.external_events.preview_modal.title')),
  );

  const body = el('div', { className: 'modal-body' });

  // 顶部 hint — "这是 dry-run, 没写 session, 没调 LLM".
  body.append(el('p', { className: 'hint' },
    i18n('chat.external_events.preview_modal.dry_run_hint'),
  ));

  // reason/error — 发不出去的场景 (invalid_payload / empty_callbacks /
  // persona_not_ready).
  if (data.reason) {
    const errBox = el('div', {
      className: 'external-events-preview-error hint warn',
    });
    errBox.append(el('div', {},
      i18n('chat.external_events.preview_modal.reason_fmt', data.reason),
    ));
    if (data.error_code || data.error_message) {
      errBox.append(el('div', { className: 'mono small' },
        `${data.error_code || ''} ${data.error_message || ''}`.trim(),
      ));
    }
    body.append(errBox);
  }

  // Wire preview list
  const wire = Array.isArray(data.wire_preview) ? data.wire_preview : [];
  if (wire.length) {
    body.append(el('h4', {},
      i18n('chat.external_events.preview_modal.wire_heading'),
    ));
    const list = el('div', { className: 'external-events-preview-wire' });
    wire.forEach((msg, idx) => {
      const role = msg?.role || '?';
      const content = typeof msg?.content === 'string'
        ? msg.content : JSON.stringify(msg?.content);
      const isTail = idx === wire.length - 1;
      const item = el('details', {
        className: `wire-row wire-role-${role}` + (isTail ? ' wire-tail' : ''),
        open: isTail,
      });
      item.append(el('summary', {},
        `#${idx} · ${role.toUpperCase()} · ${content.length} chars`,
      ));
      item.append(el('pre', { className: 'mono' }, content));
      list.append(item);
    });
    body.append(list);

    body.append(el('div', { className: 'modal-actions-inline' },
      el('button', {
        type: 'button',
        className: 'btn',
        onClick: async () => {
          try {
            await navigator.clipboard.writeText(
              JSON.stringify(wire, null, 2));
            toast.ok(i18n('chat.external_events.preview_modal.copied_wire'));
          } catch {
            toast.err(i18n('chat.external_events.preview_modal.copy_failed'));
          }
        },
      }, i18n('chat.external_events.preview_modal.copy_wire_btn')),
    ));
  } else if (!data.reason) {
    body.append(el('div', { className: 'empty-state muted' },
      i18n('chat.external_events.preview_modal.wire_empty'),
    ));
  }

  // Coerce info
  const coerce = Array.isArray(data.coerce_info) ? data.coerce_info : [];
  if (coerce.length) {
    body.append(el('h4', {},
      i18n('chat.external_events.preview_modal.coerce_heading'),
    ));
    for (const c of coerce) {
      body.append(el('div', { className: 'external-events-coerce-entry' },
        `${c.field}: ${JSON.stringify(c.requested)} → ${JSON.stringify(c.applied)}`,
        c.note ? el('div', { className: 'muted small' }, c.note) : null,
      ));
    }
  }

  const foot = el('div', { className: 'modal-foot' },
    el('button', {
      type: 'button',
      className: 'btn primary',
      onClick: close,
    }, i18n('chat.external_events.preview_modal.close_btn')),
  );

  dialog.append(head, body, foot);
  backdrop.append(dialog);

  function close() {
    backdrop.remove();
    document.removeEventListener('keydown', onKey);
  }
  function onKey(ev) {
    if (ev.key === 'Escape') {
      ev.preventDefault();
      close();
    }
  }
  // 点击 backdrop 外围 (而不是 dialog 本身) 关闭. 不要直接 =close,
  // 否则点击 dialog 内部也会冒泡到 backdrop.
  backdrop.addEventListener('click', (ev) => {
    if (ev.target === backdrop) close();
  });
  document.addEventListener('keydown', onKey);

  document.body.appendChild(backdrop);
}
