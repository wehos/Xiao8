# -*- coding: utf-8 -*-
# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Centralized prompts + templates for user **negative-intent / avoidance directives**.
Two related but distinct tools live here:

(1) **Ban-topic extraction (with term)**: ``DIRECTIVE_PATTERNS`` regex templates for
    7 locales + ``extract_directives()``. Matches imperative "verb + object"
    structures; the capture group yields the topic directly. On a hit,
    ``memory.user_directives`` persists it for 3 days (TTL:
    ``USER_DIRECTIVE_TTL_SECONDS``); on the next ``_build_initial_prompt`` startup the
    active terms are injected into the system prompt so the model avoids them.

(2) **Negative-intent keyword scan (boolean)**: ``NEGATIVE_KEYWORDS_I18N`` +
    ``scan_negative_keywords()``. A frozenset substring scan; a hit means "the user
    wants to end the current topic" (covering both the *explicit avoidance* and the
    *annoyance* families). Downstream, the evidence system
    (``app/memory_server._amaybe_trigger_negative_keyword_hook``) asynchronously runs
    one LLM target check (``NEGATIVE_TARGET_CHECK_PROMPT``) deciding which fact gets
    the disputation signal.

Motivation
----------
Users occasionally say explicitly "别再提 X / 不要叫我 X / stop saying X /
その話はもう" — all explicit ban-topic directives. The current-session LLM sees the
original message and needs no help here; but by the **next session restart**
(archive / cold start / reconnect) that message has long been compressed away and the
model steps on the same landmine again.

Where it lands: run the regex extraction at the user_utterance entry point → on hit →
write to ``memory/{name}/user_directives.json`` (3-day TTL, storage handled by
``memory/user_directives.py``). The next ``_build_initial_prompt`` renders the active
entries into a block appended to the end of the system prompt.

Convention: prefer false positives
----------------------------------
- All locale templates run **in parallel**, independent of language detection
  (mixed Chinese/English speech is common)
- Captured terms only get a light trim (strip surrounding punctuation + particles),
  no semantic validation
- A term is stored only when its length ∈ [2, 40]; out-of-range terms are dropped
- The regexes only cover directives **with a concrete object** (ban_topic).
  Object-less "闭嘴/换话题/shut up" is already visible to the LLM in context and is a
  poor fit for persistence, so it is **not** extracted
- Cost of a false positive = the user says it once more; model cost = one extra
  system-prompt line; cost of a miss = the user gets offended again. Hence the bias
  toward leniency.
- ⚠️ The zh templates are the one exception, and only against *Japanese* input.
  They carry both scripts in one pattern, and Traditional ``別`` is the same
  codepoint as the Japanese kanji while ``提 / 講 / 談 / 討論`` are shared outright —
  so "特別講演について話しましょう。" is structurally a zh hit. There the false
  positive is not "one extra line": it pollutes a Japanese user's directive store
  systematically, for three days at a time. Two guards keep that closed; see the
  comment above ``_PATTERNS_RAW``.

ban-topic regex vs. negative-keyword scan
-----------------------------------------
- The regex can capture the term directly (imperative structure is clear); it feeds
  the user_directives persistence
- The substring scan only decides "is there negative intent" and captures no term;
  it is the fast pre-filter for evidence (LLM re-checks the target on a hit) and also
  covers the "annoyed" family ("烦死", "annoying" — no term, not a directive, but
  still a negative signal)
"""  # noqa: DOCSTRING_CJK
from __future__ import annotations

import re
import threading
import unicodedata
from typing import List, Tuple

from config.prompts._locale import normalize_prompt_locale, prompt_locale_fallback_key
from config.prompts.prompts_sys import _loc


# 抓到 term 后剥两端的字符：标点 + 各语言语气助词 / 修饰小尾巴。
# 全在尾部 strip，不影响中间内容。
_TRIM_TRAIL = (
    # ASCII / CJK 标点 / 空白
    " \t\n\r"
    ".,!?;:\"'`()[]{}<>"
    "。！？，；：、…—·"
    # ⚠️ 与 _ZH_BRACKET_PAIRS 成对：凡是被当作话题分隔符的括号，两端都必须在这里
    # 剥掉，否则 term 会带着括号存进去（`〔重要，紧急〕`）。有测试钉这条不变量。
    "“”‘’（）【】《》「」『』〈〉〔〕［］〖〗"
)
# 各语言的句末助词 / 语气词（出现在 term 尾部时一并剥掉）。
#
# ⚠️ 按 locale 分开，不是一张全局表：CJK 助词在不同语言里是**同一个码位的不同
# 词**。``唄`` 在中文是 ``呗`` 的繁体语气词，在日文是"歌"（子守唄 = 摇篮曲）；
# 拿中文那套去剥日文 term，``子守唄`` 会被削成 ``子守``（codex P2）。``了`` 同理
# （日文 完了 / 終了）。所以哪个 locale 的模板命中，就只剥哪个 locale 的助词。
_TRIM_TRAIL_TOKENS_BY_LOCALE: dict[str, Tuple[str, ...]] = {
    "zh": (
        "了", "啊", "呀", "吧", "嘛", "哦", "呗", "啦", "呢", "嘞", "诶",
        # zh-TW / 台湾口语：简体语料里少见但台湾日常极常用——不补的话
        # "別再提工作喔" 存下来的 term 是 "工作喔"。
        # ⚠️ 三个字**整个不收**，正则放行组里也没有：
        #   ``唄``（``呗`` 的繁体）在日文里是"歌"，ban 一个日文歌名
        #     （"别再提花の唄了。"）会被削成 "花の"；
        #   ``耶`` / ``捏`` / ``囉`` / ``啰`` **同时也是常见词尾字**（坎耶 / 拿捏 /
        #     揉捏 / 嘍囉 / 喽啰），"别再提精准拿捏。" 会被削成非词 "精准拿"、
        #     "别再提小喽啰。" 被削成 "小喽"（后者 parent 是对的，是本 PR 拉坏的，
        #     繁简两侧一起，codex P2）。
        # 判据是代价方向：收了它们，常见说法能拿到干净的 term（"工作耶"→"工作"），
        # 但罕见话题被腰斩成**非词**；不收，常见说法多带一个字（"工作耶"）——term
        # 里仍然完整含着真话题，模型对得上。宁可多一个字，不可少一个字。
        # ⚠️ ``囉`` 在台湾日常里比 ``耶`` 还高频（好囉 / 走囉），一度舍不得撤。但频率
        # 不是这条判据的自变量——高频只决定"多带一个字"发生得多不多，而腰斩成非词的
        # 那一侧代价不因罕见而变小。同一段注释里 ``耶`` / ``捏`` 已经这么定过。
        "喔", "唷", "齁", "欸", "誒", "咧", "喲",
    ),
    "ja": ("ね", "よ", "わ", "の", "って", "なんて", "という"),
    "ko": ("요", "은", "는", "이", "가", "을", "를", "에", "에서"),
}

_HAN_RE = re.compile(r"[一-鿿㐀-䶿]")
# 不含汉字 = 与汉字不共码位 = 任何 locale 带上它都不会撞。自动发现而不是手点名单：
# 以后加一张新的假名 / 谚文 / 西里尔助词表，它会自动进这个集合。
_SCRIPT_DISJOINT_FAMILIES = tuple(
    fam
    for fam, toks in _TRIM_TRAIL_TOKENS_BY_LOCALE.items()
    if not any(_HAN_RE.search(tok) for tok in toks)
)

# 反问尾巴：跟在句末助词后面（"工作了好嗎"），正则的可选助词组只放行一个，
# 剩下的会并进 term，靠 trim 的循环逐层剥掉。
#
# ⚠️ 单列一张表、且**只在 term 不带括号时**才剥。这几个短语同时也是大量作品名的
# 结尾（《我們好不好》/《最近你好嗎》），而剥括号发生在剥语气词之前——不设条件的
# 话 "别再提电影《我们好不好》。" 存下来的是 "电影《我们"，把标题腰斩成非词
# （codex P2）。
#
# 判据这一维是干净闭集：**原始 term 里有没有出现过括号字符**。带括号 = 里面是被
# 引用的专名，专名尾部的 "好不好" 是名字的一部分；不带括号才是说话人的反问语气。
# 反过来"哪些短语可能是作品名结尾"是开集，枚举不干净。
# ⚠️ 裸的 ``吗 / 嗎`` 也要收：``別再提工作嗎？`` 存成 ``工作嗎``、``不要再說工作了嗎？``
# 存成 ``工作了嗎``（后者连 ``了`` 都剥不掉，因为剥到 ``嗎`` 就停了）。它们和上面那批
# 多字短语一样受引号判据保护，``《你可以吗》`` 不会被腰斩（codex P2）。
_TAIL_INTERROGATIVES_BY_LOCALE: dict[str, Tuple[str, ...]] = {
    "zh": ("好吗", "好嗎", "好不好", "可以吗", "可以嗎", "行吗", "行嗎", "吗", "嗎"),
}

# ⚠️ 没有自己 CJK 助词表的 locale（en / ru / es / pt）回落到 **zh + ja** 的并集：
# 混合语言是这个模块明确支持的路径（"stop talking about 前女友了" / "stop saying
# 仕事ね"），term 整段可能是中文也可能是日文，不剥就把助词原样存进去（codex P2）。
# 分表要隔离的是 zh/ja/ko **互相**污染——它们命中时用自己那张表，不受这里影响。
# ⚠️ 这里只列 zh：假名 / 谚文那两张表由 _trim_term 无条件追加（见
# _SCRIPT_DISJOINT_FAMILIES），列在这儿反而是一份会漂移的冗余。
_TRIM_TRAIL_FALLBACK_LOCALES = ("zh",)

# 与 locale 无关的尾巴：ASCII 词，字形上不可能和别的语言撞。中英混说很常见
# （"别提 my ex please"），所以这些对每个 locale 都剥。
_TRIM_TRAIL_TOKENS_ANY = ("please", "porfa", "porfavor")


def _norm_lang(lang: str) -> str:
    """Normalize a lang code (``zh-CN`` → ``zh``, ``pt-BR`` → ``pt``, etc.).

    The render functions in this module resolve templates by exact dict key; if the
    upstream passes ``user_language`` through unchanged (with a region suffix),
    everything falls into the English fallback — a user-visible regression.
    Normalizing once at the boundary is more robust than requiring every caller to
    normalize first.

    Strategy: prefer ``config._runtime.normalize_language_code`` (the app registers
    ``utils.language_utils.normalize_language_code`` at startup, which understands
    Steam literals like ``schinese`` → ``zh``; unknown languages map to ``en`` —
    render functions fall back to English); when the resolver is unbound, degrade to
    a local split fallback.

    ⚠️ This helper serves the i18n **template rendering** path (unknown → en). If you
    need an "unknown → Chinese" fallback (e.g. the contract of
    ``scan_negative_keywords``), do not reuse this helper; write a local strip — see
    that function's implementation.
    """
    if not lang:
        return 'en'
    try:
        from config._runtime import normalize_language_code as _nlc
        out = _nlc(lang, format='short') or lang
    except Exception:
        out = lang
    # Defensive split: resolver 未绑定（partial entrypoint / 测试直跑）时
    # ``_nlc`` 会**原样**返回输入；这里手动剥 region 后缀，保 zh-CN → zh
    # 这种基础归一化在测试环境也能工作。已是短码则 split 是 no-op。
    if '-' in out or '_' in out:
        out = out.split('-', 1)[0].split('_', 1)[0]
    return out or 'en'


# 可存储 term 的长度区间。``_trim_term`` 也要知道下限：把 term 剥到低于下限，结果
# 是整条指令被丢弃，那还不如把有歧义的那个尾字留着（见 _trim_term）。
_TERM_MIN_LEN = 2
_TERM_MAX_LEN = 40


def _trim_term(term: str, locale: str = "") -> str:
    """Trim a term: strip trailing particles/modifiers first, then surrounding punctuation + whitespace.

    ``locale`` selects which language's particle list applies, because the same
    codepoint is a different word per language: ``U+5504`` is a Chinese sentence
    particle and the Japanese word for "song". Locales with no CJK list of their
    own (en / ru / es / pt) fall back to Chinese — mixed-code input carries a
    Chinese tail far more often than any other. ASCII tails are always stripped.

    Stripping never takes a term below ``_TERM_MIN_LEN``. Several particles are
    also ordinary word-final characters (拿捏 / 坎耶 / 好咧), and there is no local
    rule that tells the two readings apart. When the choice is "particle reading →
    term too short → the whole directive is dropped" versus "keep the character",
    keeping it is the only option that preserves anything at all.
    """  # noqa: DOCSTRING_CJK
    if not term:
        return ""
    # 走 config.prompts._locale 的公共归一化，不自己剥 region 后缀：
    # tests/unit/test_prompt_locale_normalizer.py 明确禁止在 _locale.py 之外手写
    # locale 匹配（手写的那六份正是 "esperanto" 被当成西语、Steam 码掉进英文的来源）。
    family = normalize_prompt_locale(
        locale, default="", simplified="zh", keep_traditional=False,
    )
    families = (
        (family,)
        if family in _TRIM_TRAIL_TOKENS_BY_LOCALE
        else _TRIM_TRAIL_FALLBACK_LOCALES
    )
    # ⚠️ **不含汉字**的那些助词表，每个 locale 都带上——自动发现，不点名。
    #
    # 分表是为了拆开 CJK 之间的同码位歧义（``唄`` 中文语气词 / 日文"歌"，``了`` 是
    # 日文 完了/終了 的构词成分），而那种歧义只可能发生在**汉字**上。谚文、假名与
    # 汉字都不共码位，任何 locale 带上它们都不会撞。只带命中 locale 自己那张的话：
    #   ``别再提전남친은。``  存成 ``전남친은``（base 是 ``전남친``）
    #   ``别再提仕事ね。``    存成 ``仕事ね``（base 是 ``仕事``）
    # 都是 codex P2。locale 说的是**哪条模板命中**，不是话题本身是什么语言。
    families = tuple(dict.fromkeys(families + _SCRIPT_DISJOINT_FAMILIES))
    cjk = tuple(
        tok for fam in families for tok in _TRIM_TRAIL_TOKENS_BY_LOCALE[fam]
    )
    # ⚠️ 反问尾巴**不跟着 fallback 走**：``好吗 / 吗`` 是**句子级**的中文疑问语气，
    # 而 en/es/ru/pt 模板的句法本身已经框定了宾语——``stop saying 你好吗`` 里的
    # ``你好吗`` 整个就是被 ban 的东西，剥掉尾巴会存成 ``你好``（codex P2）。
    # 助词那批要 fallback（中英混说的 term 常整段是中文），反问短语不要。
    interrogatives = _TAIL_INTERROGATIVES_BY_LOCALE.get(family, ())
    # 引号里的语气词判据对**所有** CJK 尾词生效，不只多字反问短语：单字的也一样是
    # 名字的一部分（``《想見你喔》`` / ``《就是愛唷》``，codex P2）。
    # ⚠️ ASCII 的 please / porfa 也要一起门控。原先以为「它们不会出现在 CJK 作品名里」
    # 所以不用管——但英文作品名里会（``《Never Please》`` 被削成 ``Never``，parent 是
    # 完整的；忽略大小写之后连 ``Please`` / ``PLEASE`` 一起中招，codex P2）。
    # 门控只管**引号之内**：``别再提工作 please。`` 里的 please 在引号外，照旧剥掉。
    gated = cjk + interrogatives + _TRIM_TRAIL_TOKENS_ANY
    # ⚠️ **长的先试**：短 token 往往是长 token 的后缀（``吗`` ⊂ ``好吗``），先剥短的
    # 会把长的那次判断绕过去。
    # ⚠️ ASCII 那批现在也在 gated 里，别再单独加一遍——重复之后「把 ASCII 尾巴从剥词
    # 表里拿掉」就成了空操作，变异永远见不了红（变异跑出来的）。
    tokens = sorted(gated, key=len, reverse=True)
    # ⚠️ 长度下限只保护**有歧义**的那批：``耶 / 捏 / 咧`` 同时也是常见词尾字（坎耶 /
    # 拿捏 / 好咧），剥到存不下等于整条指令被丢，那还不如留着。而 ASCII 的 please /
    # porfa、假名、谚文都不可能是中文词的一部分——对它们套下限的话
    # ``別再提錢please。`` 会存成 ``錢please``，而 parent 是剥掉之后按长度丢弃
    # （codex P2）。判据：token 里含汉字才算有歧义。
    ambiguous = frozenset(tok for tok in gated if _HAN_RE.search(tok))
    original = term.strip()
    quoted_until = _zh_quoted_span_end(original)
    s = original
    # 剥掉的尾部字符数。⚠️ 判据要的是尾词在**原始 term** 里的位置，而 s 会被两端反复
    # 削短；只有右端的削减会影响绝对下标，所以只需累计这一侧。
    right_removed = 0
    changed = True
    # 被长度下限挡下来的尾词。⚠️ 它的**后缀**也不许再剥：``别提钱好吗？`` 里 ``好吗``
    # 因为只剩 ``钱`` 被挡下，紧接着 ``吗`` 又把它削成非词 ``钱好``（codex P2）。
    floor_blocked: set[str] = set()
    # 反复剥尾词，直到稳定（"了啊吧" 这种连续助词）
    while changed:
        changed = False
        for tok in tokens:
            # ⚠️ ASCII 尾词的大小写判据：**全小写或全大写才剥，首字母大写不剥**。
            # ``別再提工作 PLEASE。`` 里的全大写是对同一个客套词的强调，该剥（codex P2
            # 要求过忽略大小写）；但无条件忽略大小写会把**首字母大写**的题名词一起削掉
            # ——``Never Please is off limits.`` 存成 ``Never``，parent 是完整的
            # （codex P2 反向）。首字母大写在英文里正是「这是名字的一部分」的信号。
            # 代价方向和 CJK 那批一致：宁可多一个词，不可吃掉名字的一半。
            #
            # ⚠️ ``s.lower()`` 必须在**循环内**算：``s`` 在这个 for 里就会被改短，
            # 提到外面去算一次的话第二个 token 会拿陈旧的串去比，多削一个字
            # （``電影《你好》續集好嗎`` 变成 ``電影《你好》續``——自测抓到的）。
            tail = s[-len(tok):]
            if not (s.endswith(tok) or (tail.lower() == tok and tail.isupper())):
                continue
            if any(tok != blocked and blocked.endswith(tok) for blocked in floor_blocked):
                continue
            trimmed = s[: -len(tok)]
            shorter = trimmed.rstrip()
            # ⚠️ 尾词就是整条话题时一律不剥：``stop saying please`` / ``no menciones
            # porfa`` 里 ``please`` 本身就是被 ban 的东西，剥掉整条指令就没了
            # （codex P2）。这条不看歧义——空串没有任何保留价值。
            if not shorter:
                continue
            # 剥到低于下限 = 整条指令被丢；**有歧义的**尾字宁可留着（codex P2）。
            if len(shorter) < _TERM_MIN_LEN and tok in ambiguous:
                floor_blocked.add(tok)
                continue
            # 语气词 / 反问短语同时也是大量作品名的结尾，所以只在它**落在引号之外**
            # 时才剥。判据是这个尾词在原 term 里的起点有没有越过最后一段括号的收尾：
            #   ``《最近你好嗎》``        → ``好嗎`` 在括号内，剥了就腰斩标题；
            #   ``《你好》好嗎``          → 越过了收尾括号，是句子级语气，该剥；
            #   ``電影《你好》續集好嗎``  → 同样越过了，中间隔着普通修饰词也一样该剥。
            # ⚠️ 早先用的代理判据是「剥完之后前缀是不是正好以收尾括号结尾」，第三行
            # 就判错（codex P2）——中间隔一个 ``續集`` 它就不认了。
            tail_start = len(original) - right_removed - len(tok)
            if tok in gated and tail_start < quoted_until:
                continue
            right_removed += len(s) - len(shorter)
            s = shorter
            changed = True
            # ⚠️ 剥掉一个之后必须**从头重扫**。tokens 是按长度排序的，但排序只在
            # 进入这一轮时成立——剥掉句末的 ``呢`` 会**露出**更长的 ``好吗``，而
            # 这一轮的游标已经走过它了，接着匹配到的是它的后缀 ``吗``，存下非词
            # ``工作好``（``我不想聊工作好吗呢？``，parent 是 ``工作好吗``；codex P2）。
            # 长度下限那道闸（floor_blocked）管的是另一头，顶不了这个。
            break
        # 同时剥两端标点
        new_s, right_delta = _strip_trail(s)
        if new_s != s:
            right_removed += right_delta
            s = new_s
            changed = True
    return s.strip()


def _strip_trail(s: str) -> Tuple[str, int]:
    """Strip punctuation off both ends; returns the result and the right-side count.

    ⚠️ 括号只在**真的包住整条 term** 时才剥。无条件放进 strip 字符集的话，标题只是
    term 的一个后缀时收尾括号会被削掉——``别再提电影〈你好〉。`` 存成 ``电影〈你好``
    （parent 是完整的；``〈〉〔〕［］〖〗`` 四对是本 PR 新加进 _TRIM_TRAIL 的，
    codex P2）。判据是**配对**，不是「这个字符是不是括号」：
      · 整条被一对括号包住 → 连括号一起剥（``《你好》`` → ``你好``）；
      · 末尾是收尾括号、而它的开括号还在 term 里 → 保住（``电影〈你好〉``）；
      · 末尾是落单的收尾括号 → 照旧剥掉（``你好》`` → ``你好``）。
    ⚠️ 顺带把 ``《》 【】 「」`` 那几对一起纠正了（parent 在 ``电影《你好》`` 上也削）。
    留一半不改就是又一处「同类两张表各走各的」，而且新旧两批的行为会不一致。
    """  # noqa: DOCSTRING_CJK
    right = 0
    while True:
        # ⚠️ 只处理**非对称**的一对。对称引号（``"``）不进 _ZH_COUNTERPART，所以下面
        # 那轮通用 strip 本来就会把两端一起剥掉——在这里再写一支是死代码（穷举
        # 40320 条输入，加不加没有任何可观察差异）。
        if len(s) >= 2 and _ZH_COUNTERPART.get(s[0]) == s[-1]:
            s = s[1:-1]
            right += 1
            continue
        # ⚠️ 一次只剥**一个**字符再重算：剥掉一个之后谁配上谁会变。
        # ⚠️ 括号的判据是「**这个位置**配上了没有」，不是「另一半在不在串里」。后者
        # 太粗：``电影《你好》续集》`` 里 ``《`` 确实在，但它早就被前面那个 ``》``
        # 配掉了，末尾这个是多余的，parent 会剥掉（codex P2）。
        unmatched = _zh_unmatched_delims(s)
        if s and s[-1] in _TRIM_TRAIL and (
            s[-1] not in _ZH_COUNTERPART or len(s) - 1 in unmatched
        ):
            s = s[:-1]
            right += 1
            continue
        if s and s[0] in _TRIM_TRAIL and (
            s[0] not in _ZH_COUNTERPART or 0 in unmatched
        ):
            s = s[1:]
            continue
        return s, right


def _zh_unmatched_delims(s: str) -> set:
    """Indices of bracket characters that never paired up, scanned in order."""
    stack: List[int] = []
    unmatched: set = set()
    for index, char in enumerate(s):
        if char in _ZH_CLOSE_FOR_OPEN:
            stack.append(index)
        elif char in _ZH_COUNTERPART:
            if stack and _ZH_CLOSE_FOR_OPEN[s[stack[-1]]] == char:
                stack.pop()
            else:
                unmatched.add(index)
    unmatched.update(stack)
    return unmatched


# ---------------------------------------------------------------------------
# 正则模板：(locale, kind, compiled_pattern, capture_group_index)
#
# 每条 pattern 必须有一个 capture group 给 term。
# kind 目前只有 ``ban_topic``（带 term）；将来若加 ``rename_request`` 等
# 在此扩展。
# ---------------------------------------------------------------------------

# 各 locale 内的"动词块"（说/提/talk about/言う/...）由各 locale 自己列。
# pattern 全部 re.compile 以 IGNORECASE / UNICODE 跑。

# ---------------------------------------------------------------------------
# zh 的两道守卫：复合词左界 + 假名
# ---------------------------------------------------------------------------
# 繁体不另开一套 pattern，直接写进同一条（``[别別]`` / ``[说說]`` / ``讨论|討論``）。
# 同一个句法结构维护两份 regex，改一侧忘另一侧是迟早的事（#2655 里同类漂移出现
# 过四次）。代价：命中记录里的 locale 一律是 ``zh``——那个字段只做诊断，不查表。
#
# 但 ``別`` 与日文是同一码位（``說`` 不是，日文写作 ``説`` U+8AAC），而
# ``提 / 講 / 談 / 討論`` 本来就是中日共用字，所以补繁体等于把日文输入拉进 zh
# 模板的射程。下面两道守卫各管一维：

# (1) 左界：``X别`` 是复合词词尾而非祈使否定。
# ⚠️ 这一维**不可能枚举干净**——"这个别提了" 与 "个别说法"、"这部分别提了" 与
# "分别说明" 在字面上完全同形，中文分词层面就是歧义。所以这里只收零反例的四组
# （简繁各一个字形）：特别 / 性别 / 区别 / 级别 后面接 说/提/讲/谈 在中日文里都
# 极常见（"他特别提到你的名字" 今天就会被抓成 ban_topic），而 "X特|别提" 这种
# 切法在中文里不存在。
# 其余（个别 / 分别 / 告别 / 类别…）保持既有的宽松口径：它们各自都有真实反例，
# 收紧会把 "工作这个别提了" 这类主用例整片打死——宁可留既有误报。
#
# ⚠️ 这道守卫**只用在模板 1**（否定词 + 动词 + 宾语）。模板 2 / 4 的 ``别`` 前面
# 是被捕获的**话题本身**，话题正好以这些字结尾时（"模特别提了。"、"这种可能性别
# 提了。"）守卫会把整条指令吃掉——而模板 2/4 要求动词后面**紧跟**终结符，本来就
# 很难被复合词命中（残留只有 "他特别提了。" 这种退化形，term 是 2 字垃圾，与本
# PR 之前同）。模板 1 相反：复合词后面接的是句子剩余部分，误报必然发生（codex P2）。
#
# ``个/個`` 能收进来正是因为守卫已经收窄到模板 1：``工作这个别提了`` 这个设计上的
# 主用例走模板 2，不受影响；而模板 1 里 "这个别提工作了" 这种切法在中文里不成立。
# 收了它才挡得住 ``個別提案書。`` 这类**纯汉字**日文（没有假名，(2) 的守卫够不着），
# 顺带修掉简体既有的 ``个别说法不太准确。``（codex P2）。
_BIE_COMPOUND_LEFT = "特性区區级級个個"
_ZH_BIE = f"(?<![{_BIE_COMPOUND_LEFT}])[别別]"

# ``休`` 反过来：作否定词是文言用法（"休提当年勇"），现代聊天里基本不出现；而
# 退休 / 午休 / 调休 全是复合词。所以只在词首认它，不做字符枚举。
# ⚠️ 再排掉 ``休講``：词首规则拦不住句首的它（"休講だって。" / "休講情報。"），而
# ``講`` 是本 PR 新加的动词，等于给整条模板 1 开了个日文入口。中文没人写 "休講"
# ——这个否定用法在现代中文里只剩 "休提 / 休想"（对抗排查）。
_XIU_COMPOUND_LEFT = "退午调調补補年病公轮輪全双雙不歇罢罷特半"
_ZH_XIU = f"(?<![{_XIU_COMPOUND_LEFT}])休(?!講)"

# ⚠️ 否定词是**闭集**，而且 _ZH_NEG 与下面三条日文守卫的证据正则必须用同一份源。
# 手抄了四份的时候，往 _ZH_NEG 加一个词（比如 ``勿``）而忘了同步证据，中文侧照常
# 工作、但 ``勿提君の名は。`` 会被日文守卫整条吞掉，而同结构的 ``莫提君の名は。``
# 不会——同一模板内否定词之间的行为不对称，且全文件没有一条测试会红。
#
# 单字与多字分开：日文的 ``〜別`` 后缀问题只存在于单字（见 _ZH_NEG_VERB_EVIDENCE
# 的左界注释），多字的 ``不要 / 不准`` 不可能是日文名词后缀。
_ZH_NEG_SINGLES = ("别", "別", "莫", "休", "甭")
_ZH_NEG_MULTIS = ("不要", "不许", "不許", "不准")
# 正则里用的单字形态带各自的复合词守卫（_ZH_BIE 的左界 / _ZH_XIU 的 ``(?!講)``）；
# 证据正则用的是上面那份**裸字形**，它自己另配左界。
_ZH_NEG_SINGLES_GUARDED = (_ZH_BIE, "莫", _ZH_XIU, "甭")
# ⚠️ ``休`` 在证据里仍要排掉 ``講``：``休講``（日文＝停课）本身会命中「否定词 +
# 言说动词」这条结构证据。端到端上它不可达（_ZH_XIU 的 ``(?!講)`` 让 ``休講…`` 根本
# 产不出 zh 匹配），所以一度当死分支删掉过；但**证据正则自己不许在日文语料里命中**
# 是一条更强、也更该守的性质——有测试按分支逐条扫。见 _ZH_NEG_UNAMBIGUOUS。
_ZH_NEG = (
    "(?:" + "|".join(_ZH_NEG_SINGLES_GUARDED + _ZH_NEG_MULTIS) + ")"
)

# ---------------------------------------------------------------------------
# 言说动词表 —— **生成**，不手写
# ---------------------------------------------------------------------------
# ⚠️ 手写清单在这里失败过两次：模板 1 的宾语是 ``(.{1,40}?)``，什么都能吃，所以
# 单字 ``提`` 一旦匹配成功，剩余部分整体成功，正则**没有理由**回溯去试 ``提起``。
# 于是复合动词必须排在它的单字前缀之前。
#
# ⚠️ 这里**不**做「言说动词 × 结果补语」的笛卡尔积。曾经把 ``到 / 起 / 及`` 当补语
# 一并吃掉，理由是 ``別提到我前女友。`` 该存 ``我前女友`` 而不是 ``到我前女友``。
# 但 ``提／到达时间`` 和 ``提到／达时间`` 是同一串字，两种切分都合语法，局部没有任何
# 规则能分开——于是 ``别提到达时间。`` 存成 ``达时间``、``别聊起点问题。`` 存成
# ``点问题``、``别提及格线的事。`` 存成 ``格线的事``（codex P2，简体也回归）。
# 「以 到/起/及 开头的词」是开集，枚不干净；而代价是不对称的：多留一个 ``到``，
# term 里仍然完整含着真话题，模型对得上；吃掉一个字，存进去的是非词。
_ZH_SAY_VERBS = ("说", "說", "提", "聊", "讲", "講", "谈", "談", "扯")
# 双字言说动词。⚠️ 只收**单字拆不开**的那些：``讨`` 在现代汉语里不能单用（"别讨政治"
# 不成话），所以 ``讨论`` 必须整体进表；它吃掉以 ``论`` 开头的话题（"别讨论文格式。"
# → ``文格式``）是没法避免的代价，base 也一样。
#
# ⚠️ ``谈论 / 談論`` **不收**：``谈`` 本身就是独立动词，把 ``谈论`` 当复合词是可选的，
# 而代价是实打实的——``别谈论语考试。`` 被削成 ``语考试``、``别谈论语。`` 整条消失
# （base 分别是 ``论语考试`` / ``论语``；codex P2）。以 ``论`` 开头的话题是开集
# （论语 / 论文 / 论坛 / 论证 / 论政治），跟结果补语那条是同一族问题：多留一个
# ``论`` 话题仍完整，吃掉一个字存进去的是非词。
_ZH_SAY_COMPOUNDS = ("讨论", "討論")
# 言说动词里哪些字形**日文也有**，且能跟 ``別`` 组成日文复合名词：
#   別提案（べつていあん，另一份提案）/ 別講座 / 別談話 / 別討論。
# 这是「``別提`` 到底是中文祈使还是日文复合词」这一维的**真正**判据——歧义是**动词**
# 的属性，不是位置的属性。之前按位置切（先只挡「左邻是汉字」、后来又单独挡串首），
# 每挡一格 codex 就能再举出下一格（``今回は、別提案…`` / ``「別提案…」``）。
# ⚠️ 剩下那些一个都不可能：简体 ``说 讲 谈 讨论`` 不是日文汉字；繁体 ``說`` 也不是
# （日文写 ``説``，U+8AAC ≠ U+8AAA）；``聊`` 在日文里不成词头；``扯`` 日文根本没有
# 这个字。所以 ``別聊 / 別扯 / 別說`` 无论出现在哪里都只可能是中文——它们不设左界、
# 也不要求 ``再``，否则 ``別聊君の名は。`` / ``請別扯君の名は。`` 整条 0 命中，而同一
# 句简体因为 ``别`` 在 _ZH_EVIDENCE_CHARS 里就是好的（codex P2）。
# ⚠️ 有相等断言钉着：往这里加一个不该加的字，对应的繁中说法立刻整条丢掉。
_ZH_SAY_VERBS_JA_SHARED = ("提", "講", "談", "討論")
# 其中**双字**的那些。⚠️ 双字复合词整个进了触发词，term 会直接以假名开头，
# 「一个汉字接假名」那条形状判据够不着，见 _is_japanese_sentence_match。
_ZH_COMPOUND_JA_SHARED_VERBS = tuple(
    v for v in _ZH_SAY_VERBS_JA_SHARED if len(v) > 1
)
# 称呼类：结构是「动词 + 我 + 称呼」，与上面两族都不同
_ZH_ADDRESS_VERBS = ("管我叫", "称呼我为?", "稱呼我為?", "喊我", "叫我")
# 不情愿类指令头（``我不想聊X`` 那条模板）。⚠️ 单列成常量是因为括号体的 temper
# 也要认它：只认「否定词 + 动词」的话，``别提价格<预算.我不想聊收入>目标.`` 里的
# 第二条指令认不出来，两条被并成一条（codex P2，简体回归）。
_ZH_RELUCTANCE = ("不想", "不愿意", "不願意", "不愿", "不願", "懒得", "懶得",
                  "没心情", "沒心情")
# 前置话题模板（``X 别提了``）自己的触发词表，比通用言说动词**窄**：谈 / 談 / 扯 /
# 讨论 / 討論 放在这个位置不成话（"工作别扯了" 勉强，"工作别讨论了" 要带宾语）。
# ⚠️ 单列成常量是因为它一度直接写死在模板里，于是这里能接受、证据表却不认：
# ``君の名は別提起了。`` / ``君の名は別提及了。`` 整条 0 命中，而同句简体和带 ``再``
# 的 ``別再提起了`` 都是好的（codex P2）。_ZH_EVIDENCE_WORDS 现在从它派生，
# test_directive_tables 里有一条守卫钉住「这里能接受的每个形式 + 了 都是中文证据」。
_ZH_PREPOSED_SAY_VERBS = ("提起", "提及", "说", "說", "提", "聊", "讲", "講")


def _zh_verb_alternation(*, with_address: bool) -> str:
    """Build the verb alternation, compounds always ahead of their single-char prefix."""
    parts = list(_ZH_SAY_COMPOUNDS)
    if with_address:
        parts += list(_ZH_ADDRESS_VERBS)
    parts += list(_ZH_SAY_VERBS)
    # ⚠️ 原子组：复合动词排在单字前缀之前只解决了"谁先匹配"，解决不了**回溯**。
    # ``别提起了。`` 里 ``提起`` 先中，但后面只剩 ``了`` 凑不够两个单位的宾语，
    # 引擎就退回 ``提``、把 ``起了`` 当成话题存下来——而这个模块的 docstring 明确
    # 说不抽无宾语的指令（codex P2）。原子化之后动词一旦选定就不再回头，整条匹配
    # 直接失败，正是想要的结果。
    return "(?>" + "|".join(parts) + ")"


_ZH_VERBS_WITH_ADDRESS = _zh_verb_alternation(with_address=True)
_ZH_VERBS_PLAIN = _zh_verb_alternation(with_address=False)

# 模板 2 / 4 尾部会跟一个填充词（``就`` / ``的事`` / ``这个``…），它属于句子而不属于
# 话题。⚠️ 这个不能在正则里"顺手吃掉"：前缀是 lazy 的，正则会优先把**话题的最后一个
# 字**塞进可选组——"他的成就别提了。" 存成 "他的成"。用左界字符黑名单挡也不行，
# ``就`` 作为词尾是开集（成就 / 迁就 / 功成名就 / 一蹴而就 / 練就 / 鑄就…），漏一个
# 就腰斩一个真实话题（codex P2）。
#
# 改成在**抽完之后**比对：一条 term 如果正好等于"另一条 term + 一个填充词"，那它就是
# 同一个话题多带了个尾巴，丢掉长的。这条判据不猜词边界，只看同一句话里实际抽出了
# 什么——"股票就" 会因为 "股票" 也在结果里而被丢，"功成名就" 因为没有 "功成名"
# 这条 term 而保留。
_ZH_TRAILING_FILLERS = (
    "就", "的事", "的", "这个", "這個", "这事", "這事",
    "这话题", "這話題", "这件事", "這件事",
)

# ---------------------------------------------------------------------------
# 语义为空的 term —— 存，但不拿去做出口硬拦截
# ---------------------------------------------------------------------------
# "别再讲这个了" 在语法上**有**宾语，正则照抓不误，抽出来的 term 是 ``这个``
# —— 一个所指完全依赖上下文、脱离那一轮就没有任何意义的伪宾语。
#
# ⚠️ 危害是**不对称**的，这条表的存在理由全在这个不对称上：
#   · 注入 prompt 那一侧无害。``- 这个`` 只是一行模型无法执行的噪音，而抽取
#     侧对这类 term 的处理是被既有测试成片钉死的既定行为（繁简一致、复合词
#     守卫的主用例都拿 ``这件事`` 当载体），不该由本条顺手改掉。
#   · 但主动搭话的出口硬闸做的是**子串匹配**（generation._proactive_directive_hits）。
#     ``这个`` 是汉语最高频的词之一，一旦入库，几乎每一条主动搭话都会命中被
#     drop —— 表现为主动搭话整体静默，持续到 TTL 过期（递增后最长 30 天），
#     而用户今天还没有任何界面能看到、更别说删掉这一条。
#
# 所以判据落在**消费侧**而不是抽取侧：软约束照旧全量注入（模型有上下文，多一行
# 噪音无妨），硬拦截跳过这批词。谁放大了危害就在谁那里收口。
#
# ⚠️ 这一维是**闭集**，跟 ``就`` 那种开集词尾不同：纯指代词是有限的功能词，不是
# 内容词。所以这里可以枚举，而 _ZH_TRAILING_FILLERS 那批只能靠事后比对。
# 判据是"这个词单独拿出来指代什么" —— ``这个`` 什么都不指，``加班`` 指加班。
# 反过来说，只有 term **整体**等于这些词才跳过；``这个项目`` 有实义中心词，照拦。
# ⚠️ 这张表**不需要** 'zh-TW' 键，与 PROMPT_ZH_TW 门管的那类表不是一回事：
# 它不是给 ``_loc`` 查表渲染的 prompt 模板（那种表缺 zh-TW 会让繁中用户拿到
# 英文），而是一份判据词表，消费侧 ``is_semantically_empty_term`` 拿的是所有
# locale 的**并集**做匹配、从不按 locale 查键。繁体字形（這個 / 那個 / 這件事）
# 就内联在 'zh' 这一项里，繁中用户照样命中。开一个 'zh-TW' 键反而会造出
# "同一批词分两处维护、改一边忘另一边"的漂移面——这个模块为此吃过四次亏
# （见 _ZH_NEG 那段"派生不手抄"的注释）。
# ⚠️ **人称代词与指代词是同一维，不是两维**。判据（"单独拿出来指代什么"）对
# ``我`` / ``me`` / ``我们`` 的答案与对 ``这个`` 的完全一样：它们指向说话现场的
# 参与者，不指向任何话题内容。而危害比指代词那批**更大** —— ``别再提我了`` /
# ``stop talking about me`` 恰恰是这功能最自然的用法之一，抽出来的 term 就是
# ``me``，实测让三条最普通的英文主动搭话草稿（"Hey, let me know how the build
# went!" 等）3/3 全被 drop，词边界在这里救不了场（``me`` 本身就是完整的词）。
# 那正是这张表当初为 ``it`` → ``favorite`` 建立时要挡的同一类 P1，而 TTL 递增
# 之后中招代价从 3 天变成最长 30 天。
_SEMANTICALLY_EMPTY_TERMS_BY_LOCALE: dict[str, frozenset[str]] = {  # noqa: PROMPT_ZH_TW  # 判据词表非渲染模板，繁体字形内联在 zh 项
    "zh": frozenset({
        "这个", "這個", "那个", "那個", "这些", "這些", "那些",
        "这事", "這事", "那事", "这件事", "這件事", "那件事", "那件事",
        "这话题", "這話題", "那话题", "那話題",
        "这个话题", "這個話題", "那个话题", "那個話題",
        "这种事", "這種事", "那种事", "那種事",
        "这样的事", "這樣的事", "这类事", "這類事",
        "刚才的事", "剛才的事", "刚刚的事", "剛剛的事",
        "这一切", "這一切", "那一切",
        # 人称 / 反身 / 领属。单字的（我 / 你 / 他）够不到 _TERM_MIN_LEN=2，
        # 抽取侧本来就存不下，不列进来当噪音。
        "我们", "我們", "咱们", "咱們", "你们", "你們",
        "他们", "他們", "她们", "她們", "它们", "它們",
        "自己", "我自己", "你自己", "他自己", "她自己", "自个儿", "自個兒",
        "我的", "你的", "他的", "她的", "我们的", "我們的", "你们的", "你們的",
    }),
    "en": frozenset({
        "this", "that", "these", "those", "it", "them",
        "this thing", "that thing", "this topic", "that topic",
        "this stuff", "that stuff", "this one", "that one",
        "the topic", "the subject", "anything", "everything", "all this",
        # 人称 / 反身 / 领属（"i" 是单字符，够不到 _TERM_MIN_LEN）
        "me", "you", "we", "us", "him", "her", "he", "she",
        "my", "your", "our", "his", "hers", "its", "their", "theirs",
        "mine", "yours", "ours",
        "myself", "yourself", "yourselves", "ourselves",
        "himself", "herself", "itself", "themselves", "oneself",
    }),
    "ja": frozenset({
        "これ", "それ", "あれ", "この話", "その話", "あの話",
        "この件", "その件", "あの件", "こんな話", "そんな話",
        "この話題", "その話題",
        # 人称 / 反身。単漢字（私 / 僕 / 君 / 俺）は _TERM_MIN_LEN に届かない。
        "わたし", "あたし", "ぼく", "おれ", "あなた", "きみ", "おまえ", "お前",
        "自分", "自分自身", "私たち", "僕たち", "俺たち", "わたしたち",
        "あなたたち", "君たち", "私自身",
    }),
    "ko": frozenset({
        "이거", "그거", "저거", "이것", "그것", "저것",
        "이 얘기", "그 얘기", "이 이야기", "그 이야기",
        "이 일", "그 일", "이 주제", "그 주제",
        # 인칭 / 재귀
        "우리", "우리들", "저희", "너희", "당신", "그들", "그녀",
        "자기", "자신", "자기자신", "제가", "저는",
    }),
    "ru": frozenset({
        "это", "то", "этом", "этому", "об этом", "эту тему", "эта тема",
        # Личные / возвратные / притяжательные
        "мы", "вы", "он", "она", "они", "оно",
        "меня", "тебя", "нас", "вас", "его", "её", "ее", "их",
        "мне", "тебе", "нам", "вам", "себя", "себе",
        "мой", "твой", "наш", "ваш", "свой", "моя", "твоя", "наша", "ваша",
    }),
    "es": frozenset({
        "esto", "eso", "aquello", "este tema", "ese tema", "esta cosa",
        # Personales / reflexivos / posesivos
        "yo", "tú", "tu", "él", "el", "ella", "ellos", "ellas",
        "nosotros", "nosotras", "vosotros", "vosotras", "usted", "ustedes",
        "mí", "mi", "ti", "te", "nos", "su", "sus",
        "mío", "mía", "tuyo", "tuya", "suyo", "suya",
        "mí mismo", "sí mismo", "ti mismo",
    }),
    "pt": frozenset({
        "isso", "isto", "aquilo", "esse tema", "este tema", "essa coisa",
        # Pessoais / reflexivos / possessivos
        "eu", "tu", "ele", "ela", "eles", "elas", "nós", "vós",
        "você", "vocês", "mim", "ti", "si", "me", "te", "nos", "vos",
        "meu", "minha", "teu", "tua", "seu", "sua", "nosso", "nossa",
        "dele", "dela", "si mesmo", "mim mesmo",
    }),
}

# 所有 locale 的并集。⚠️ 与抽取本身一样按**并集**判，不按命中 locale 分表：
# 混合语言输入是这个模块明确支持的路径（"stop saying 这个"），而这批词跨语言
# 不存在同形歧义 —— 它们在任何一种语言里都是纯指代词，没有哪个是别的语言的
# 实义内容词。（``_TRIM_TRAIL_TOKENS_BY_LOCALE`` 必须分表是因为 ``唄`` 那类
# 同码位歧义，这里没有那个问题。）
# ⚠️ 建集时就 NFC 归一，别只归一查询侧：源码字面量本身可能以分解形式存进文件
# （编辑器 / 剪贴板差异），那样查询侧再怎么归一也对不上。两侧同一形式才闭合。
_SEMANTICALLY_EMPTY_TERMS = frozenset(
    unicodedata.normalize("NFC", t)
    for terms in _SEMANTICALLY_EMPTY_TERMS_BY_LOCALE.values() for t in terms
)


def term_needs_case_sensitive_match(term: str) -> bool:
    """Whether a term must be matched case-sensitively: a name spelled like a pronoun.

    English writes ``the US`` / the films ``Us`` and ``Her`` with capitals and
    the pronouns ``us`` / ``her`` without, so case is the one local signal that
    separates the two. Same criterion ``_trim_term`` already uses to keep
    ``Never Please`` intact while still stripping a trailing ``please``.

    ⚠️ **Only for terms that actually collide with the table.** Widening this to
    "any capitalized term" costs far more than it buys: ``Work`` (an IME
    capitalizing the first letter, or just a sentence-initial capture) would
    then stop matching ``work`` / ``WORK`` in a draft, a fresh miss on the
    ordinary path — while the collision this exists for is confined to terms
    whose casefold is in ``_SEMANTICALLY_EMPTY_TERMS``. Pinned by
    ``test_matcher_is_case_insensitive``.

    Complementary to ``is_semantically_empty_term`` — for a term whose casefold
    is in the table, exactly one of the two is true (lowercase → exempt from the
    hard gate, capitalized → gated but matched case-sensitively). Terms outside
    the table get False from both and follow the ordinary path.
    """
    normalized = unicodedata.normalize("NFC", term.strip())
    if not any(ch.isupper() for ch in normalized):
        return False
    return normalized.casefold() in _SEMANTICALLY_EMPTY_TERMS


def is_semantically_empty_term(term: str) -> bool:
    """Whether a term is a bare referent that means nothing outside its own turn.

    Consumers use this to decide whether a directive term is specific enough to
    hard-block output on. It is deliberately NOT applied at extraction time —
    see the table's comment for why the two sides differ.

    ⚠️ Capitalized terms are never exempt. ``stop talking about US`` (the
    country) and the films ``Us`` / ``Her`` all yield terms whose casefold lands
    on a pronoun in the table; exempting them means the hard gate skips a topic
    the user explicitly banned, which is strictly worse than before the pronoun
    entries existed. The proactive gate pairs this with a case-sensitive match
    for such terms, so ``US`` no longer matches the ``us`` in "Want us to…" —
    fixing only this half would silence proactive chat wholesale instead.

    ⚠️ NFC first. Accented Spanish/Portuguese entries (``él`` / ``mí`` / ``você``)
    can reach the store decomposed (``e`` + combining acute) from an IME or a
    pasted string, which is a different codepoint sequence from the composed
    form written in the table above — the lookup would silently miss and the
    pronoun would go back to hard-blocking every draft. The proactive gate
    normalizes for the same reason (``generation._normalize_for_match``); this
    predicate has to agree with it or the two disagree on the same term.
    """
    normalized = unicodedata.normalize("NFC", term.strip())
    if any(ch.isupper() for ch in normalized):
        return False
    return normalized.casefold() in _SEMANTICALLY_EMPTY_TERMS

# 话题里允许出现的**一个单位**。四条 zh 模板共用一份，别再各写各的。
#
# ⚠️ 换行必须**显式**排除：原先这些捕获组写的是 ``.``，在没有 DOTALL 时天然不匹配
# 换行；换成负字符类之后这个性质就没了，多行消息里 term 会把换行连同**下一条指令**
# 一起吞掉（"别提工作\n别提加班" → term "工作\n别提加班"）。
#
# ⚠️ 句读也要排除，否则捕获会跨过本该收尾的标点去够更长的匹配
# （"功成名就别提了，功成名别提了。" → "了，功成名别提"）。但**书名号 / 引号里的
# 标点属于话题本身**：`电影《你好，李焕英》别提了。` 的逗号在片名里，一刀切排除会
# 把 term 截成 "李焕英"（codex P2）。所以一个"单位"是「非句读非换行的单字」**或**
# 「一整段配对括起来的内容」——括号内不限标点，但不许跨行。
_ZH_BRACKET_PAIRS = (
    ("《", "》"), ("「", "」"), ("『", "』"), ("“", "”"), ("【", "】"),
    ("（", "）"), ("〈", "〉"), ("〔", "〕"), ("［", "］"), ("〖", "〗"),
    # ASCII 也要收：``"Everything, Everywhere"别提了。`` / ``电影(Hello, World)别提了。``
    # 在 parent 上是完整的，不收就成了回归（codex P2）。
    # ⚠️ 不收单引号 ``'``：英文里它是词内撇号（don't / it's），配对没有意义。
    # ⚠️ ASCII 方括号也要收：``_TRIM_TRAIL`` 本来就把它们当两端分隔符剥，却没进配对
    # 表，于是 ``[Hello, World]别提了。`` 在逗号处被截成 ``World``（codex P2）。
    ('"', '"'), ("(", ")"), ("[", "]"), ("{", "}"), ("<", ">"),
)
# 行分隔符：一条指令不跨行，这张表是那条判据的**唯一**来源。⚠️ 不能只写 ``\\r\\n``
# ——Python 的 ``\\s`` 还认 U+2028 / U+2029 / U+0085 / \\v / \\f 和 U+001C~U+001F，
# 少排一类就有一类能跨行（codex P2）。括号体、话题字符类、横向空白三处都从这里取。
_ZH_LINE_SEP = r"\r\n\v\f\x1c-\x1f\x85\u2028\u2029"

# 横向空白（除行分隔符外的任何空白）。⚠️ 定义挪到这里是因为 _ZH_DIRECTIVE_AHEAD
# 要用它，而那条在括号体构建之前就得算出来。判据本身见下面 _ZH_HSPACE 那段注释。
_ZH_HSPACE_ONE = r"[^\S" + _ZH_LINE_SEP + r"]"
_ZH_HSPACE = _ZH_HSPACE_ONE + "*"


# 所有否定词的**首字**。⚠️ 给 ASCII 括号体 temper 用（见 _zh_bracket_body）：
# 括号体里出现任何一个否定词的开头，就说明这对括号跨了两条指令。从表派生，
# 加一个否定词自动跟上。
_ZH_NEG_FIRST_CHARS = "".join(
    dict.fromkeys(neg[0] for neg in _ZH_NEG_SINGLES + _ZH_NEG_MULTIS)
)


# 一条**完整**的指令头（否定词 + 可选的 ``再`` + 言说动词）。⚠️ 给括号体 temper 用。
# 判据必须是「一条指令」而不是「一个否定词字符」：先写死成 ``别別`` 漏掉了
# ``不要 / 不许 / 甭``，改成禁**首字**之后又矫枉过正——``电影(不可思议; 2020)别提了。``
# 退回 ``2020``、``电影(莫名其妙; 2020)`` / ``电影(休闲; 2020)`` 同样（codex P2，
# 三条都是简体回归）。普通词里以 ``不 / 莫 / 休 / 别`` 开头的太多了，字符这一维
# 根本不是判据；真正说明「括号跨了两条指令」的是**后面跟着言说动词**。
# ⚠️ 顺带把 ``别再提(告别版)。`` 这类也放开了：``别`` 后面是 ``版`` 不是动词。
# ⚠️ 用**加了护的**否定词表和**完整的**动词表——判据必须和 zh 模板本身的语法一致，
# 否则两头对不上：
#   · 裸 ``别`` 会把复合词误判成指令——``电影(个别讨论; 2020)别提了。`` 里的
#     ``个别`` + ``讨论`` 看着像一条指令，括号护不住，整条退回 ``2020``。
#     模板自己用的是 _ZH_BIE（左邻不能是 个 / 告 这类），这里也得用。
#   · 漏掉称呼类动词（叫我 / 喊我 / 管我叫）则相反：``别叫我价格<预算.别叫我
#     收入>目标.`` 两条指令被并成一条（codex P2，两条都是简体回归）。
#   · 只认「否定词 + 动词」还不够：``我不想 / 沒心情 / 懶得`` 那条模板也是一条指令，
#     ``别提价格<预算.我不想聊收入>目标.`` 同样被并成一条（codex P2，简体回归）。
_ZH_DIRECTIVE_AHEAD = (
    "(?:"
    + "(?:" + "|".join(_ZH_NEG_SINGLES_GUARDED + _ZH_NEG_MULTIS) + ")"
    + _ZH_HSPACE + "(?:再)?" + _ZH_HSPACE
    + _ZH_VERBS_WITH_ADDRESS
    + "|"
    + "(?:" + "|".join(_ZH_RELUCTANCE) + ")"
    + _ZH_HSPACE + "(?:再)?" + _ZH_HSPACE + _ZH_VERBS_PLAIN
    + ")"
)


def _is_ascii_delim(char: str) -> bool:
    """Whether a delimiter is ASCII, i.e. doubles as an operator in running text."""
    return char.isascii()


def _zh_bracket_body(lo: str, hi: str) -> str:
    """One bracketed run: bounded body, and symmetric pairs temper the negation."""
    banned = re.escape(hi) + _ZH_LINE_SEP
    # ⚠️ **非对称的 ASCII** 那几对（``() [] {} <>``）不许跨句读配对。它们在中文行文里
    # 常常是比较号 / 代码片段，两条互不相干的指令各带一个就会被当成一整段引文：
    # ``别再提价格<预算。别再提收入>目标。`` 被并成一条 ``价格<预算。别再提收入>目标``，
    # parent 是分开的两条（codex P2）。
    # ⚠️ 全角括号不设这条：它们本来就只用来引起引文，不会被当运算符，而真作品名里
    # 带句号的确实有。⚠️ 对称的 ``"`` 也不设——那是上一轮量过之后专门定的（排掉句读会
    # 腰斩 ``"Everything. Everywhere"``，并在模板 2/4 上产出非词），见下面 lo == hi 那段。
    ascii_pair = lo != hi and _is_ascii_delim(lo)
    unit = f"[^{banned}]"
    if lo != hi:
        # ⚠️ 认**一层**同种嵌套：正则本身只会在第一个同种收尾处闭合，所以
        # ``《电影《你好》续集，第二章》别提了。`` 里外层在内层的 ``》`` 就断了，后面
        # 的逗号不再受保护、只存下 ``第二章``；``《甲《乙》丙，丁》别提了。`` 更是整条
        # 消失（base 两条都完整；codex P2）。``_zh_quoted_span_end`` 那个深度扫描管的
        # 是**剥尾巴**，管不到这里的匹配本身。
        #
        # 正则做不了任意深度，但作品名里的嵌套实际上只有一层（``《X《Y》Z》``）。
        # 两个分支互斥（单字那支把 lo / hi 都排掉了），不会引进歧义回溯。
        # ⚠️ 嵌套那一支也要一起排掉句读，否则上面那条形同虚设——引擎会走这一支把
        # ``<预算。别再提收入>`` 整段当成一层嵌套吃下去（自测抓到的）。
        inner_banned = re.escape(lo) + re.escape(hi) + _ZH_LINE_SEP
        # ⚠️ 只排**句末**标点，分号留着。分号在闭合的 ASCII 代码段里是合法内容
        # （``代码{foo;bar}别提了。`` 被截成 ``bar``，parent 是 ``代码{foo;bar``；
        # codex P2），而跨句合并那一族靠 ``。！？`` 就挡住了。
        # ⚠️ 只加在 inner_banned 上。ASCII 那几对全是**非对称**的，上面那个
        # ``unit = f"[^{banned}]"`` 在这个分支里会被整个覆盖掉——往 banned 上加是
        # 死代码（变异跑出来的）。
        if ascii_pair:
            # ⚠️ 只排**全角**句末标点。ASCII 的 ``? !`` 在闭合的标题 / 代码段里是
            # 内容——``电影(Who?)别提了。`` 整条 0 命中，parent 还留着 ``电影(Who``
            #（codex P2）。
            # ⚠️ 光排全角句读不够，还要 temper 掉 ``别 別``——判据和下面对称引号那支
            # 是**同一条**：引文里包住一整条指令必然要带否定词。放开 ASCII 句读之后
            # ``别再提价格<预算.别再提收入>目标.`` 被并成一条 ``价格<预算.别再提收入>目标``，
            # parent 是分开的两条（codex P2，``{} [] () <>`` 四对都是）。
            # ⚠️ 按字符 temper 而不是按 ASCII 句读补一格：分隔的标点是开集（同一句
            # 换成逗号、分号一样合并），而「体里出现否定词」直指病因——两条指令被
            # 当成一段引文。代价只落在「ASCII 括号里**又**带否定词**又**带标点」的
            # 标题上，``告别版`` 这类不带标点的照旧走单字分支、完整保留。
            # ⚠️ 否定词字符从否定词表**派生**，别手抄。先写死成 ``别別`` 两个字，于是
            # ``不要提价格<预算.不要提收入>目标.`` / ``不许提…`` / ``甭提…`` 照样被并成
            # 一条（codex P2，三条都是简体回归）。同一个「派生不手抄」的教训，同一天
            # 第三次。
            inner_banned += "。！？"
        # ⚠️ 嵌套那一支也要 temper。它是**另一条**路径，只给外面那个单字分支加
        # 前视等于形同虚设——引擎会走这一支把 ``<预算.别提收入>`` 整段当成一层嵌套
        # 吃下去，``别提价格<<预算.别提收入>目标>.`` 又被并成一条（codex P2，
        # 简体回归）。和「嵌套支也要排句读」是同一个坑，同一处，第二次。
        nested = (
            re.escape(lo)
            + f"(?:(?!{_ZH_DIRECTIVE_AHEAD})[^{inner_banned}])"
            + f"{{0,{_TERM_MAX_LEN}}}{re.escape(hi)}"
        )
        unit = f"(?:(?!{_ZH_DIRECTIVE_AHEAD})[^{inner_banned}]|{nested})"
    if lo == hi:
        # ⚠️ 对称的一对（只有 ASCII ``"``）容易被误配：孤立的双引号很常见——英寸号、
        # 代码片段——两个不相干的句子各带一个就会被当成一整段引文：
        # ``尺寸5"别提了。尺寸6"别提了。`` 被并成一条 ``尺寸5"别提了。尺寸6``，两条
        # 指令全丢（codex P2）。非对称括号没有这个问题（``《`` 不会被误当收尾）。
        #
        # 判据是 temper 掉 ``别/別``：引文里包住一整条指令必然要带否定词。这样
        # ``尺寸5"别提了。尺寸6"别提了。`` / ``尺寸5"别提了，尺寸6"别提了。`` 配不上
        # 对，两条指令都留下。作品名只有在带否定词**又**带标点时才受影响（不带标点的
        # ``"再别康桥"`` 走单字分支，照样完整）。
        #
        # ⚠️ 句读（``。！？；``）和逗号都**不**从字符类里排除，是量过之后的选择，不是
        # 遗漏（coderabbit 报过一次「这条护栏没落地」——代码属实，但落地它更坏）：
        #   · 排掉逗号：``"Everything, Everywhere"`` 被腰斩成 ``Everything``（回归）。
        #   · 排掉句读：``别再提"Everything. Everywhere"。`` 退回 ``Everything``、
        #     ``别再提"你好。世界"好吗？`` 退回 ``你好``；更糟的是前置话题那两条模板会
        #     产出**非词**——``"工作。加班"别提了。`` 变成 ``加班``、
        #     ``关于"工作。加班"就别提了。`` 变成 ``加班"就``，两条都比 parent 还差。
        #   · 代价方向：留着句读最坏是多带一段（``我不想聊尺寸5"工作。尺寸6"加班。``
        #     存下整段），term 里仍含着真话题；排掉是吃字造非词。宁可多，不可少。
        # ⚠️ 和上面 ASCII 那支用**同一条**判据。这里原先只挡 ``别別`` 两个字符，
        # 于是 ``不要提价格"预算.不要提收入"目标.`` 照样被并成一条（codex P2，
        # 简体回归）。两处都从 _ZH_DIRECTIVE_AHEAD 取，别再各写一份。
        unit = f"(?!{_ZH_DIRECTIVE_AHEAD}){unit}"
    # ⚠️ 长度必须**有界**：无界的 ``*`` 在每个开括号处都会扫到串尾去找收尾，
    # ``"《" * 8000`` 这种输入就是二次方——实测 2.6 秒，而 record_from_text 是在
    # 用户每条消息上同步跑的（codex P2）。上界取 _TERM_MAX_LEN：比它长的括号段
    # 无论如何都会被末尾的长度过滤丢掉，收紧不损失任何能存下来的 term。
    return f"{re.escape(lo)}(?:{unit}){{0,{_TERM_MAX_LEN}}}{re.escape(hi)}"


_ZH_BRACKET_RUN = "(?:" + "|".join(
    _zh_bracket_body(lo, hi) for lo, hi in _ZH_BRACKET_PAIRS
) + ")"

# 深度扫描用的两张表。⚠️ **对称**的那一对（只有 ASCII ``"``）单独一张：落单的双引号
# 是英寸号、颜文字 ``:(`` 这类普通字符，不能当「没写完的引文」——_zh_bracket_body 已经
# 这么决定过一次（"比把开括号排除出单字分支更好的地方"那段），这里再把它当硬边界就是
# 自相矛盾，``别再提5"屏幕好吗。`` 会因为那个英寸号留着 ``好吗``（codex P2）。
# 非对称的 ``《`` 落单时确实是没写完的引文，仍然算。
_ZH_CLOSE_FOR_OPEN = {lo: hi for lo, hi in _ZH_BRACKET_PAIRS if lo != hi}
_ZH_SYMMETRIC_DELIMS = frozenset(lo for lo, hi in _ZH_BRACKET_PAIRS if lo == hi)
# 非对称括号的「另一半」，两个方向都有。给 _strip_trail 判「这一半是不是落单的」用。
_ZH_COUNTERPART = {
    ch: other
    for lo, hi in _ZH_CLOSE_FOR_OPEN.items()
    for ch, other in ((lo, hi), (hi, lo))
}


def _zh_quoted_span_end(text: str) -> int:
    """Index past the last quoted run: a tail starting there is outside the quotes.

    ⚠️ ``_trim_term`` 拿它判「这个语气词是句子的还是作品名的一部分」。用「前缀是不是
    正好以收尾括号结尾」当代理判据是不够的——``電影《你好》續集好嗎`` 中间隔了一个
    普通修饰词就判错（codex P2）。
    """  # noqa: DOCSTRING_CJK
    # ⚠️ 按**深度**扫，不能拿括号段正则去找「最后一段完整引文」：正则会把
    # 外层的开括号跟**内层**的收尾配成一对，同种括号嵌套时就记错了收尾位置——
    # ``《电影《你好吗》续集好吗》`` 会被当成到内层 ``》`` 为止，末尾的 ``好吗`` 被
    # 当句子级语气剥掉（codex P2）。
    # ⚠️ 落单的对称引号（出现奇数次）整个忽略：它是英寸号 / 颜文字，不是引文。
    # 不忽略的话它会把**后面所有**括号遮蔽掉——``5"屏幕《你好吗》`` 里那个英寸号一开
    # 引号，后面的 ``《…》`` 就再也进不了扫描，``好吗`` 被当句子级语气剥掉、连
    # ``《你`` 都被削（codex P2）。
    paired_symmetric = {
        ch for ch in _ZH_SYMMETRIC_DELIMS if text.count(ch) % 2 == 0
    }
    end, stack = _zh_scan_quoted(text, paired_symmetric, frozenset())
    # ⚠️ 落单的 ASCII 开括号不只是"最后别当成引文"——它压在栈底，会让**后面**每一段
    # 合法引文都记不上收尾位置（``别再提价格<预算《你好吗》。`` 里那个 ``<`` 一开，
    # ``《你好吗》`` 就白闭合了，``好吗`` 被当句子级语气剥掉、书名腰斩成 ``《你``，
    # parent 是完整的，codex P2）。所以要把它们当**普通字符**重扫一遍，而不是扫完
    # 再丢掉栈——那时候位置信息已经没了。
    if stack and all(_is_ascii_delim(ch) for ch, _pos in stack):
        end, stack = _zh_scan_quoted(
            text, paired_symmetric, frozenset(pos for _ch, pos in stack)
        )
    # 还剩没闭合的**非对称**开括号 = 有一段引文一直延伸到末尾（``电影《好不好``）。
    # ⚠️ 落单的对称引号不算——它是英寸号 / 颜文字，见 _ZH_SYMMETRIC_DELIMS 的注释。
    # ⚠️ 落单的 ASCII 开括号也不算：``（ 「 《 【`` 这些全角括号在中文里只用来引起一段
    # 引文，落单＝标题被截断；而 ``< { ( [`` 在中文行文里常常是比较号 / 代码片段，本来
    # 就不配对。判据是「开括号是不是 ASCII」，闭集、不用枚举运算符。
    return len(text) if stack else end


def _zh_scan_quoted(
    text: str, paired_symmetric: set, ignored: frozenset
) -> Tuple[int, List[Tuple[str, int]]]:
    """One depth-aware pass; ``ignored`` holds indices to treat as plain text."""
    end = 0
    stack: List[Tuple[str, int]] = []
    symmetric_open: str | None = None
    for index, char in enumerate(text):
        if index in ignored:
            continue
        if symmetric_open is not None:
            if char == symmetric_open:
                symmetric_open = None
                if not stack:
                    end = index + 1
            continue
        if stack and char == _ZH_CLOSE_FOR_OPEN[stack[-1][0]]:
            stack.pop()
            if not stack:
                end = index + 1
        elif char in _ZH_CLOSE_FOR_OPEN:
            stack.append((char, index))
        elif char in paired_symmetric:
            symmetric_open = char
    return end, stack


_ZH_PLAIN_CHAR = r"[^，。！？；,.!?;" + _ZH_LINE_SEP + r"]"
# ⚠️ 只有模板 2 的**前置**话题 temper 掉裸的 ``关于/關於``：``关于 X 就别提了`` 归
# 模板 4 管，前缀能逐字吃过它的话，"我觉得关于股票就别再讲了" 会多产出一条
# ``我觉得关于股票就``。
#
# ⚠️ 这条**不能**放进共用的单字分支。放进去就变成"话题里任何位置都不许出现关于"，
# 把动宾结构的宾语一起毙了：``别再提关于公司的传闻。`` 从 ``关于公司的传闻`` 变成
# 完全不命中（codex P2）。前置话题与动词后宾语是两种结构，守卫只属于前者。
_ZH_PLAIN_CHAR_NO_GUANYU = r"(?!关于|關於)" + _ZH_PLAIN_CHAR

# ⚠️ ``，`` 曾经短暂地也放进前置话题的字符类里（想让 ``工作，还是别提了。`` 退化成长
# span ``工作，还是`` 而不是纯功能词 ``还是``）。撤了：那等于把**上一轮已经定过**的
# 「前置话题不跨小句」推翻——``算了，工作别提了。`` 会存成 ``算了，工作``。两个方向
# 结构上完全同形（``X，Y别提了``），差别纯粹是词汇性的（哪一半是话题），没有结构判据；
# 既然已经定了取右半边，就不在同一个 PR 里来回翻。
# 停顿后的 ``就``（_ZH_PAUSE_THEN_JIU）和这条正交，不受影响。

# ⚠️ 停顿之后只收**横向**空白。``\s`` 连换行一起吃，于是
# ``别再提，\n工作正常。`` 会把**下一行**当成宾语存进去（parent 整条不命中，
# codex P2）。和主语间隔那条同一个理由：一条指令不跨行。
# ⚠️ 判据是「**除换行外的任何空白**」，不是手点几个空白字符。手点的话 NBSP /
# U+202F / U+2009 这些从网页、手机输入法粘进来的空白全被挡在外面——``别<NBSP>再
# <NBSP>提工作。`` 整条 0 命中，而 parent 的 ``\\s*`` 是认的（codex P2）。
# 换行是唯一要排掉的那一类（一条指令不跨行），所以写成否定式。
# ⚠️ 排掉的是**所有**行分隔符，不只是 CR/LF。Python 的 ``\\s`` 还认
# U+2028 / U+2029 / U+0085 / \\v / \\f 和 U+001C~U+001F——只排 CR/LF 的话
# ``別再提<U+2028>案をお願いします。`` 照样跨两个视觉行（codex P2）。

# ⚠️⚠️ 整个"单位"必须是**原子**的，否则是一条 ReDoS。单字分支也能匹配 ``《``，于是
# ``《a》`` 既可以被括号分支整体吃掉、也可以被单字分支逐字吃掉——这个歧义放进
# ``{2,30}?`` 的重复里就是指数级回溯：``别提`` + 30 段 ``《a》`` 要跑 1.3 秒，而
# 这条路径是**每条用户消息**都会走的（codex P1）。
#
# 原子组 ``(?>…)``（Python 3.11+）让"这个位置选哪个分支"一旦定下就不再回头，歧义
# 消失。比"把开括号排除出单字分支"更好的地方：落单的 ``"`` / ``(`` （英寸号、颜
# 文字 ``:(``）仍然能被当成普通字吃进话题，不会变成硬边界。
# ⚠️ ASCII 的 ``.`` / ``,`` 夹在词字符中间时是**标识符内部**的，不是句读：
# ``Python 3.11别提了。`` / ``example.com别提了。`` / ``价格1,000元别提了。`` 上一版
# 分别只存下 ``11`` / ``com`` / ``000元``，base 三条都是完整的（codex P2）。
#
# ⚠️ 两侧的判据是**否定式**（不是空白、不是句读），不是「列出哪些字算词字符」。
# 列举法修了一轮又一轮都还有漏：``[0-9A-Za-z]`` 漏掉 ``café.com`` / ``Дом.ру`` /
# 全角 ``１,０００`` / IDN ``例子.测试``；换成 ``\w`` 之后**组合符号**仍然漏——NFD
# 分解形的 ``café.com``（e + U+0301）和天城文 ``देवनागरी.com`` 照样被截成 ``com``，
# 因为 Python 的 ``\w`` 不含 Mn/Mc 类（codex P2 第三轮）。
#
# 否定式一次覆盖全部：``.`` / ``,`` 只要**两边都不是空白也不是句读**，它就在
# 词内部。全角逗号另配一条更紧的规则，见下面。⚠️ 全角句号 ``。`` 完全不收——没有
# 标识符用它，而它是最常见的句子终结符。
# 句尾的 ``.`` 后面是空白或串尾，右侧那条前视要求必须有字符，所以不满足。
#
# ⚠️ 放宽到 ``\w`` **不会**把两条指令并成一条：``别提工作.别提加班.`` 仍然是两条。
# 宾语是 lazy 的，而终结符分支里本来就有 ASCII ``.``——引擎先试短的那条，`.` 照样
# 结束一条指令；只有当短的凑不出合法匹配时才会把 ``.`` 吃进话题。实测确认过。
# ⚠️ ASCII 逗号右边允许**空白**：``Hello, World`` 是最常见的写法，右侧前视原样要求
# 「不是空白」的话这个逗号就不算词内字符，匹配从它之后重起——``Hello, World别提了。``
# 只存下 ``World``、``关于Hello, World就别提了。`` 存下非词 ``World就``（parent 两条
# 都完整；codex P2）。不加空白这一格就是内部不对称：同一句写成 ``Hello,World`` 反而
# 是好的。左侧不放空白（`` , x`` 那种是分句停顿，不是标识符）。
# ⚠️ 这**不是**在推翻「前置话题不跨小句」那条决定——那条管的是全角 ``，``，而 ASCII
# ``,`` 在 parent 和现状里本来就跨（``算了,工作别提了。`` → ``算了,工作``）。
# ⚠️ 用**有界**的 ``[ \t]?`` 而不是 ``\s*``：这里虽然是零宽前视、不可能参与空白瓜分，
# 但模板 2/4 的空白护栏是按字面找 ``\s*`` 的，写成无界形态等于把那道护栏的判据搞浑。
# 一个空格覆盖 ``Hello, World`` 这类写法就够了，多打的空格退回原行为。
# ⚠️ 空白**只**给逗号，句号不给。``. `` 是英文标准句界，放行它 ``That was bad. Work
# 别提了。`` 会把前面整句话吃进话题。这跟同一轮里把 ``。！？`` 挡在 _ZH_PAUSE_CHARS
# 外面是同一条理由（它们结束的是**句子**）——一开始图省事套在 ``[.,]`` 整个字符类
# 上，是我自己制造的不一致（coderabbit）。``v1.2`` 这种不带空白的照旧由原判据覆盖。
# 缩写词能有多少个字母。⚠️ 原先是**写死的三条**分支（1~3 个字母），于是四个字母的
# ``Prof.`` / ``Capt.`` 落在外面：``Prof. X别提了。`` 从 parent 的 ``Prof. X`` 变成
# **整条 0 命中**（截成 ``X`` 一个字，撞长度下限被丢弃），``关于Capt. America就别提了。``
# 存下非词 ``America就``（codex P2，简体也回归）。
# ⚠️ 这一维只能靠**定长分支枚举**——Python 的 lookbehind 不支持变长——但枚举的是
# 「字母个数」这个有界的量，不是「有哪些缩写词」那个开集。6 覆盖到 ``Approx.``；
# 再往上抬只会让句末的大写词（``别再提Winter. Snow来了。``）更容易被并进话题，而
# 那一侧的代价方向是已经定过的：多留几个字符 term 里仍完整含着真话题，少留一个字
# 存进去的是非词。
# 标识符内部标点右边允许的空白。⚠️ 原先只认**一个**空格，注释还写着「一个空格
# 覆盖 ``Hello, World`` 这类写法就够了」——量下来不够：从网页 / PDF 粘来的标题常带
# 两个空格，``Dr.  Who别提了。`` 退回 ``Who``、``Prof.  X别提了。`` **整条丢**、
# ``关于Hello,  World就别提了。`` 存下非词 ``World就``（codex P2，三条都是简体回归）。
# ⚠️ 仍然**有界**（``{1,8}``）：这是每条用户消息的热路径，而且模板 2/4 的空白护栏
# 按字面找无界形态，写成 ``*`` / ``+`` 会把那道护栏的判据搞浑。
_ZH_IDENT_GAP = _ZH_HSPACE_ONE + "{1,8}"
# ⚠️ 逗号那支的空白是**可选**的（``Hello,World`` 不带空格也要认），必须写成
# ``{0,8}`` 而不是在 _ZH_IDENT_GAP 后面加 ``?``——``{1,8}?`` 是**惰性量词**，
# 不是「零或一个 {1,8}」，那样 ``Hello,World`` 整族直接红（改完一跑就抓到）。
_ZH_IDENT_GAP_OPT = _ZH_HSPACE_ONE + "{0,8}"
_ZH_ABBREV_MAX_LETTERS = 6
_ZH_ABBREV_PERIOD = "".join(
    f"|(?<![A-Za-z](?-i:[A-Z])(?-i:[a-z]{{{tail}}}))"
    f"(?<=(?-i:[A-Z])(?-i:[a-z]{{{tail}}}))"
    + r"\.(?=" + _ZH_IDENT_GAP + r"(?-i:[A-Z]))"
    for tail in range(_ZH_ABBREV_MAX_LETTERS)
)
_ZH_IDENT_PUNCT = (
    r"(?<=[^\s，。！？；,.!?;])\.(?=[^\s，。！？；,.!?;])"
    # ⚠️ 缩写里的句点后面**跟得了空格**：``Dr. Who`` / ``Mr. Robot`` / ``U.S. Army``
    # 会被当成句界，话题从句点之后重起，只存下 ``Who``（parent 是完整的；codex P2）。
    # 判据要同时挡住真句界（``That was bad. Work别提了。`` 该存 ``Work``，coderabbit
    # 提过），所以判的是**缩写词的形状**——词首大写、字母数有界，见 _ZH_ABBREV_PERIOD：
    #   ``U.``（1）/ ``Dr.`` ``Mr.``（2）/ ``Mrs.`` ``Ave.``（3）命中；
    #   ``bad.`` 词首小写、``ABad.`` 前面还连着字母，都不命中。
    # ⚠️ 三个定长分支拆开写：Python 的 lookbehind 不支持变长。
    # ⚠️ 词首用「前面不是 ASCII 字母」而不是 ``\b``：``关于Dr. Who`` 里 ``于`` 和
    #   ``D`` 都是 Unicode 词字符，中间没有词边界，用 ``\b`` 这一条就废了。
    # ⚠️ ``(?-i:...)`` 不能省——模板整个是 IGNORECASE 编译的，裸 ``[A-Z]`` 连小写
    #   一起匹配，判据会退化成「任何句点后跟空格加字母」（自测抓到的）。
    # ⚠️ 空格用 _ZH_HSPACE_ONE 而不是 ``[ \t]``：``Dr.<NBSP>Who`` 这类从网页粘来的
    #   写法同样要认，判据和别处的横向空白同源（codex P2）。
    + _ZH_ABBREV_PERIOD
    + r"|(?<=[^\s，。！？；,.!?;]),(?=" + _ZH_IDENT_GAP_OPT + r"[^\s，。！？；,.!?;])"
    # ⚠️ 全角逗号只在**数字之间**放行。它是中文最常见的分句符，无条件当词内字符
    # 会让前置话题跨小句（``算了，别提工作。`` 存成 ``算了，工作``）；而它真正的
    # 标识符用途就是千分位 ``价格1，000元``——限定两侧是数字就够，不需要更宽。
    r"|(?<=[0-9０-９])，(?=[0-9０-９])"
)

_ZH_TOPIC_CHAR = f"(?>{_ZH_BRACKET_RUN}|{_ZH_IDENT_PUNCT}|{_ZH_PLAIN_CHAR})"
_ZH_TOPIC_CHAR_NO_GUANYU = (
    f"(?>{_ZH_BRACKET_RUN}|{_ZH_IDENT_PUNCT}|{_ZH_PLAIN_CHAR_NO_GUANYU})"
)


def _zh_topic(minimum: int, maximum: int, *, block_guanyu: bool = False) -> str:
    """Topic capture body: ``minimum`` units, except a single bracketed run counts.

    ⚠️ 一整段括起来的内容算**一个**单位，所以 ``{2,30}`` 会把独立成句的
    ``《你好，李焕英》别提了。`` 卡掉（只有 1 个单位）。但它本身就 ≥3 个字符，
    长度闸根本不会丢它——所以单独放行"以一段括号开头"的形态（codex P2）。

    ``block_guanyu`` 只给模板 2 的前置话题用，见 _ZH_PLAIN_CHAR_NO_GUANYU。
    ⚠️ 它只 temper **第一个**单位，不是每一个。套在每个单位上的话，话题**内部**
    含 ``关于`` 的真话题会被腰斩甚至整条丢掉——``这部关于爱的电影别提了。`` 只存下
    ``爱的电影``、``电影关于爱别提了。`` 整条 0 命中（parent 两条都完整；codex P2）。
    这道守卫要防的是「前缀逐字吃过句首的 ``关于``」，那只可能发生在**起点**。
    """  # noqa: DOCSTRING_CJK
    unit = _ZH_TOPIC_CHAR
    first = _ZH_TOPIC_CHAR_NO_GUANYU if block_guanyu else unit
    return (
        f"(?:{_ZH_BRACKET_RUN}{unit}{{0,{maximum - 1}}}?"
        f"|{first}{unit}{{{minimum - 1},{maximum - 1}}}?)"
    )

# 模板 1 里 term 与终结符之间允许出现的句末助词。与 ``_TRIM_TRAIL_TOKENS`` 的 zh
# 段成对：这里放行、那里剥掉，少一边 term 就带着助词存进去。
# ⚠️ ``唄`` 整个不收（正则和 trim 都不收）。它是 ``呗`` 的繁体，但在日文里是"歌"
# （子守唄 / 花の唄）。留在 trim 表里会削掉 ja 模板的 term，只留在这里也一样会削掉
# zh 模板的 term——"别再提花の唄。" 存成 "花の"（codex P2 两轮）。而 ``呗`` 本来
# 就是北方口语词、台湾并不说 ``唄``，为它承担这个代价不划算。``囉`` / ``啰`` 同理
# 整个不收，理由见 _TRIM_TRAIL_TOKENS_BY_LOCALE 那段（嘍囉 / 喽啰）。
# ⚠️ 这张表和 trim 表是**两份**手抄清单，正是最容易漂的形态。它俩不必相等（这里漏放
# 行的助词会被吃进 term、由 trim 再剥掉，结果一样），但**这里有、trim 没有**一定是
# bug——那样 term 里的助词永远剥不掉。test_directive_tables 里有一条子集守卫钉住。
# parent 就有的那一批。⚠️ 单列出来是给 _ZH_OBJECTLESS_AHEAD 用的，见那里的注释：
# 无宾语判据只认这一批，认了本 PR 新加的那批就会把 ``别再提好咧。`` 一起毙掉。
# ⚠️ 助词本身只写**一份**字符串，正则和「中文独有证据」两处都从它派生。这两处
# 一度是各抄一份的手写清单，抄着抄着就漂了：正则吃掉 ``啊 齁 欸 誒``、证据表里
# 却没有，于是 ``別提君の名は啊。`` 整条 0 命中，而 ``吧`` 那个变体是好的
# （codex P2）。见 _ZH_ZH_ONLY_FINAL_PARTICLES。
_ZH_BASE_FINAL_PARTICLE_CHARS = "了啊呀嘛哦呗吧啦呢"
_ZH_EXTRA_FINAL_PARTICLE_CHARS = "喔唷齁欸誒咧喲"
_ZH_FINAL_PARTICLE_CHARS = (
    _ZH_BASE_FINAL_PARTICLE_CHARS + _ZH_EXTRA_FINAL_PARTICLE_CHARS
)
# ⚠️ 助词之前的空白只收**横向**。写成 ``\s*`` 的话触发词齐了之后能跨过换行去够下
# 一行的助词，把上一行整条存下来——``別再提工作`` 换行 ``吧。`` 存下 ``工作``
#（codex P2）。这是「一条指令不跨行」的第八格，和另外七处同一条判据。
_ZH_BASE_FINAL_PARTICLES = (
    "(?:" + _ZH_HSPACE + "[" + _ZH_BASE_FINAL_PARTICLE_CHARS + "])"
)
_ZH_FINAL_PARTICLES = "(?:" + _ZH_HSPACE + "[" + _ZH_FINAL_PARTICLE_CHARS + "])"

# 动词和宾语之间的停顿标点：打字和 ASR 都会产生（``别再提，工作。`` / ``别叫我，"笨蛋"。``）。
# parent 靠 ``.{1,40}?`` 把它吃进话题、再由 _trim_term 剥掉；本 PR 把句读排除出话题
# 单位之后，这类指令整条 0 命中（codex P2）。放在**捕获组之外**，不进 term。
# ⚠️ 必须排在 _ZH_OBJECTLESS_AHEAD **之前**：那道前视认「动词之后直接是句读」
# ＝没有宾语，分隔符没先吃掉的话 ``别再提，工作。`` 会被它整条否掉。
# ⚠️ ``、 ：`` 也要收。一度以为它们不用收——它们确实不在话题字符类的排除表里，会被当
# 普通字吃进话题再由 trim 剥掉，``别再提、工作。`` 靠这条也能出 ``工作``。但话题很短
# 且以**有歧义的尾字**结尾时就不成立了：``别提：好咧。`` 的话题变成 ``：好``（标点凑
# 满了两个单位的下限），``咧`` 被可选助词组吃掉，trim 完只剩一个字 ``好`` 被丢弃——
# parent 存的是 ``好咧``（codex P2）。把它们挡在捕获组外面，就不会去凑下限。
# ⚠️ 里面的 ``\s*`` 也要原子化：它夹在两个 ``(?>\s*)`` 中间，不原子化就又能跟它们
# 瓜分同一串空白（模板 2/4 的空白护栏会直接红）。
# ⚠️ 分号也要收。它在话题字符类里是被排除的（本模块把它当指令终结符），而这里原先
# 不收，于是 ``别再提；工作。`` / ``工作；别提了。`` 整条 0 命中——parent 存的是
# ``工作``（codex P2）。这一维本来就是闭的：能出现在这个位置的只有本模块自己那张
# 终结符表里的分句标点，句号 / 问号 / 感叹号刻意不收（它们结束的是**句子**，收了就
# 等于让指令跨句绑定）。
# ⚠️ 两个常量从同一个字符串派生，别再各抄一份——同类两张表迟早漂。
_ZH_PAUSE_CHARS = "，、：；,:;"
_ZH_TOPIC_SEPARATOR = f"(?:[{_ZH_PAUSE_CHARS}]" + f"(?>{_ZH_HSPACE}))?"
# 模板 2 专用：只有在**显式停顿之后**才允许吃掉 ``就``。
# ⚠️ 模板 2 覆盖全部 "X别提了" 句子，``成就 / 迁就 / 功成名就`` 都住在这里，所以它
# 不能像模板 4 那样无条件带 ``(?:就)?``。但停顿标点是**硬词边界**——``工作，就别提
# 了。`` 里的 ``就`` 不可能是前一个词的末字。把 ``就`` 关进分隔符分支里，两头都保住：
# 没有停顿时一个字都不吃，有停顿时不再整条 0 命中（撤掉 ``(?:就)?`` 时带出来的）。
# ⚠️ ``就`` **之后**也会再停顿一次（``工作，这件事，就，别提了。``）。只收前面那个
# 的话，正则从第一个逗号之后重新起匹配、存下填充词 ``这件事``——parent 存的是
# ``工作，这件事，就``，里面至少含着真话题（codex P2）。
_ZH_PAUSE_THEN_JIU = (
    f"(?:[{_ZH_PAUSE_CHARS}]" + f"(?>{_ZH_HSPACE})"
    + r"(?:就)?" + f"(?>{_ZH_HSPACE})"
    + f"(?:[{_ZH_PAUSE_CHARS}]" + f"(?>{_ZH_HSPACE}))?)?"
)

# 无宾语指令的前视：动词之后到句读之间，如果只剩**一个字 + 句末助词**，那个字是
# 结果补语（说完 / 提上 / 聊死）而不是宾语，整条不该抽——本模块的 docstring 明确说
# 不抽无宾语的指令。
#
# ⚠️ 这条是**独立于宾语下限**的一道闸，不能靠把下限降到 1 来代替。下限 1 会让
# lazy 宾语 + 可选助词组优先把话题末字当助词：``别提钱的事。`` 的宾语退化成 ``钱``、
# 撞长度下限后整条消失，``别再提好咧。`` 同理（这两条正是本 PR 修掉的 base 缺陷）。
# 下限 2 保住它们，这条前视单独负责 ``别说完了。`` 不被造出 ``完了`` 这种宾语。
#
# ⚠️ 判据是**「一个字」这个数量**，不是「哪些字是补语」。一度只列了 到/起/及，
# 于是 ``别说完了。`` → ``完了``、``别提上了。`` → ``上了``、``别聊死了。`` →
# ``死了``（codex P2 报了 完，实测 光/够/死/上 一样中招）。汉语的结果补语是个开集，
# 枚不干净；而「宾语只有一个字」这件事 parent 本来就一律丢弃（长度过滤），所以按
# 数量判既覆盖全、又与 parent 完全等价。
#
# ⚠️ 助词只认 parent 就有的那批（_ZH_BASE_FINAL_PARTICLES）。认上本 PR 新加的
# 台湾口语助词，``别再提好咧。`` 会被判成「好 + 咧」而整条毙掉——而 parent 存的是
# ``好咧``。那批字同时也是常见词尾字，正是当初决定「宁可多一个字」的那批。
#
# ⚠️ 也不能把 ``的事`` 放进来：``别提钱的事。`` 会变成「钱 + 的事」被毙掉。
_ZH_OBJECTLESS_AHEAD = (
    "(?!"
    + r"\s*" + _ZH_PLAIN_CHAR + "?"
    + _ZH_BASE_FINAL_PARTICLES + "{0,3}"
    + r"\s*(?:[，。！？；,.!?;]|$)"
    + ")"
)

# (2) 假名。⚠️ 不能简单地"命中区间有假名就丢"——被 ban 的**对象本身**经常是日文
# 专有名词（"别再提ドラえもん"、"別叫我お兄ちゃん"），那种句子结构是中文的，假名
# 只是话题名，丢掉等于把用户明确说过的偏好扔了（codex P2）。
# 所以要求三个条件同时成立才判为"这是日文句子"：
_KANA_RE = re.compile(r"[぀-ゟ゠-ヿｦ-ﾟ]")
# 汉字（含中日共用区）。⚠️ 和 _JA_GRAMMAR_RE 里那几条 ``(?<=[一-鿿])`` 是同一个
# 区间，只写一份。
_HAN_RE = re.compile(r"[一-鿿]")

# (2a) 中文的正面证据：这些字形/组合在现代日文里不存在——简体字形、与日文新字体
# 分道的繁体字形（說/説、這、關/関、沒/没、稱/称）、以及日文不会出现的组合
# （叫我 / 不想 …）。命中区间里出现任意一个，这句就是中文，假名是话题名。
# ⚠️ 不收 ``不要``：它本身是日文词（ふよう），"不要提出書類" 这种会被当成中文证据。
# ⚠️ 收字之前必须逐个核对日文新字体：``没`` 和 ``称`` 曾经被误收——它们**就是**
# 日文的标准字形（没収 / 名称），而不是 ``沒`` / ``稱`` 的简体专用形。收错的代价
# 是把守卫整个短路掉：``地域別講座の名称を確認します。`` 因为含 ``称`` 被判成中文，
# 助词判据根本没机会跑（codex P2）。下面每个字都对应一个不同的日文字形：
# 别→別 说→説 讲→講 谈→談 讨→討 论→論 关→関 话→話 题→題 愿→願 懒→懶 许→許
# 为→為；這 / 甭 日文没有；說 / 關 / 沒 / 稱 是与日文分道的繁体形。
#
# ⚠️ 光靠单字覆盖不到 _ZH_NEG 的全部否定词：``不准 / 莫 / 休 / 不要`` 一个字都不在
# 上面的字类里（``不許`` 的繁体 ``許`` 也不在），于是 ``不准提君の名は。`` 被判成日文
# 句子、整条丢掉——parent 上它是好的（codex P2；``不许`` 侥幸活着只因为 ``许`` 恰好
# 在字类里，纯属巧合）。
#
# 补法是**结构**而不是再往字类里塞共用汉字：「否定词 + 可选的再 + 言说动词」这两维
# 都是闭集，且两者相邻这件事本身就是中文句法——日文不会出现 ``不准提`` 这种相邻。
# 往字类里加 ``准 / 莫 / 休`` 反而会短路守卫（``没`` / ``称`` 就是这么错过两轮的）。
#
# ⚠️⚠️ 两道边界都要，这是**实测踩到的**（本文件的 ja 语料直接抓出来的）：
#
# (a) 左界：``別`` 在日文里是后缀「按…分」——地域別 / 部門別 / 商品別 / 年齢別 /
#     性別，后面接名词。``地域別提案をお願いします。`` 于是满足「否定 + 言说动词」，
#     整句被判成中文、存下 ``案をお願いします``。``地域別談話でも可。`` 同理。
#     一度以为只有 ``講`` 会撞（休講 / 特別講座），语料证明 ``提`` / ``談`` 一样撞。
#     ⚠️「哪些名词后面能接 別」是开集（_BIE_COMPOUND_LEFT 那张表拦不住 地域別），
#     但**日文里 別 永远贴在汉字后面**，而中文的否定词前面是句首、代词或标点。
#     所以判据取「前一个字符不是汉字、也不是假名」——闭集，且不依赖枚举名词。
#     ⚠️ 假名那一半是漏过一轮才补的：``カテゴリ別提案書。`` / ``テーマ別討論スレ``
#     的 ``別`` 前面是片假名不是汉字，只挡汉字的话照样漏。
#
# (b) 右界：``休講``（＝停课）是词首也成立的日文，单独排掉，与 _ZH_XIU 的
#     ``(?!講)`` 对齐。
#
# 剩下的残余：日文以 ``不要提案…`` 开头（不要＝ふよう）仍会误判。日文一般写
# ``不要な提案``，且要同时撞上 zh 模板的其余结构，代价上接受。
# ⚠️ 左界**只给 别/別**：日文的 ``〜別`` 后缀歧义是这一个字形独有的，``莫 / 休 / 甭``
# 都不是日文的名词后缀。套给全族的话，正常的中文主语会把它们一起挡掉——
# ``我莫再提君の名は。`` / ``她莫再提地域の話。`` 在 parent 上是好的，套上左界之后整条
# 被日文守卫吞掉（codex P2）。
_ZH_NEG_JA_AMBIGUOUS = ("别", "別")
# 日文 ``〜別`` 后缀的左邻是什么？——**别枚举**。这一维我已经栽过两次：先只挡汉字，
# 漏了片假名；补上假名之后漏了拉丁字母 / 数字 / 收尾括号；补上那些之后又漏了 ``β``
# （希腊字母，``モデルβ別提案``）。字符集是开的，枚举永远差一格（codex P2 三轮）。
#
# 换成**否定式**：中文指令里的否定词总是**起一个小句**，所以它的左邻只可能是串首、
# 空白、或分句标点。凡是别的（任何文字的字母、数字、收尾括号……）都说明 ``別`` 挂在
# 一个词后面——那就是日文的 ``〜別`` 后缀。
#
# ⚠️ 收尾括号刻意**不**算「可以起小句」：``「地域」別提案`` 是日文标签。**开**括号
# 反过来一定能起小句（``「別提君の名は。」`` / ``（別提君の名は。）``——日文的 ``〜別``
# 不可能紧跟在一个开括号后面），所以它们要放行。不放行的话繁中用户只要把话写在引号里
# 整条就失效，而同一句简体 ``“别提…”`` 是好的（``别`` 有单字证据兜底）——繁简不对称
# （codex P2）。
# ⚠️ 只收**非对称**开括号，从 _ZH_CLOSE_FOR_OPEN 派生（那张表已经分好开合）。对称的
# ``" ' `` 同一个字形两用，``"地域"別提案`` 里它是收尾，放行就等于把守卫拆了。
# ⚠️ 代词（``你 妳 您 咱 请 們``）不在这里放行，它们走 _ZH_SUBJECT_BEFORE_NEG 那条。
_ZH_CLAUSE_START_LEFT = r"\s，。！？；、：,.!?;" + re.escape(
    "".join(sorted(_ZH_CLOSE_FOR_OPEN))
)
_ZH_NEG_UNAMBIGUOUS = tuple(
    "休(?!講)" if neg == "休" else neg
    for neg in _ZH_NEG_SINGLES
    if neg not in _ZH_NEG_JA_AMBIGUOUS
)
# 结构证据分三支，判据是**动词的歧义性**，不是否定词出现在什么位置。
#
# ⚠️ 这一维我按位置切过三轮，每挡一格 codex 就举出下一格：先只挡「左邻是汉字」，
# 漏了假名 / 拉丁 / β；换成「左邻要能起小句」，漏了串首的 ``別提案``；串首单独要求
# ``再`` 之后，又漏了 ``今回は、別提案…`` 和 ``「別提案…」``——分句边界后面同样能起
# 一个日文复合名词。位置这一维根本不是判据。
#
# 真正的判据：``別`` 能不能和后面那个动词组成**日文复合名词**。能的只有
# _ZH_SAY_VERBS_JA_SHARED 那四个（別提案 / 別講座 / 別談話 / 別討論），其余动词
# （聊 扯 说 說 讲 谈）日文里根本组不出来。于是：
#   (a) 无歧义否定词（莫 / 休 / 甭）：日文里不是名词后缀，动词也不限；
#   (b) 有歧义否定词 ``别 別`` + **日文没有的**动词：动词自己消歧，不设任何限制
#       （``請別扯君の名は。`` 里 ``請`` 不能进主语白名单——它是日文汉字——所以
#       靠左界这条路走不通，只能靠动词，codex P2）；
#   (c) 有歧义否定词 + **共用**动词：要同时「起一个小句」和带 ``再``。
#       ``別再提案`` 在日文里不成立（要写 ``別の再提案``），``地域別再提案`` 则被
#       左界挡住。
#
# ⚠️ (c) 的代价：``算了，別提君の名は。`` / ``「別提君の名は。」`` 回到 0 命中。
# 这正是 _is_japanese_sentence_match 的 docstring 里一直写着的那条 residual——
# 繁体触发词 + 共用动词 + 本身带日文助词的标题，局部无规则可分。parent 在这几句上
# 也是 0，所以是「这一格不新增覆盖」，不是回归。
_ZH_ZH_ONLY_VERBS = tuple(
    v for v in _ZH_SAY_COMPOUNDS + _ZH_SAY_VERBS if v not in _ZH_SAY_VERBS_JA_SHARED
)
# 单字否定词前面允许出现的主语 / 敬语。⚠️ 这张字类是闭集，有相等断言钉着：往里加
# 任何一个日文汉字就是在守卫的左界上开洞（``俺`` 是北方口语主语、同时是日文常用汉字，
# 加进来 ``俺別提案をお願いします。`` 就会被当成中文指令存下来）。
# ⚠️ ``們`` 是繁体复数后缀（我們 / 你們 / 咱們），日文既不用 ``們`` 也不用 ``们``，
# 所以它后面的 ``別`` 同样不可能是日文的 ``〜別`` 后缀。不收的话繁中用户说
# ``我們別再提君の名は。`` 会被整条丢掉，而简体 ``我们别…`` 因为 ``们`` 在别处有
# 证据而正常（codex P2）。
_ZH_SUBJECT_CHARS = "你妳您咱请們"

# 有歧义动词那一支的左界额外放行**主语 / 敬语**。``再`` 已经把歧义解掉了（``別再提``
# 在日文里不成立），剩下这道左界只是为了挡 ``地域別再提案`` 那种 ``〜別`` 后缀，
# 而主语和敬语后面不可能是那个后缀。不放行的话 ``請別再提君の名は。`` 整条 0 命中，
# 而同句简体 ``请别再提…`` 是好的（codex P2）。
# ⚠️ ``請`` 只在这里放行，**不能**进 _ZH_SUBJECT_CHARS：它是日文汉字，进了那张表
# 就等于给无 ``再`` 的 ``請別提案をお願いします。`` 开洞。
_ZH_POLITE_BEFORE_NEG = _ZH_SUBJECT_CHARS + "請"
# 「``別`` 左边那个字起不了一个小句」——即日文 ``〜別`` 后缀的左界形状。和上面那条
# lookbehind 用的是**同一张**表，只是取了反、给 Python 侧的守卫用（见
# _is_japanese_sentence_match 里的 ``〜別 + 复合名词 + 光杆假名`` 那一格）。
# ⚠️ 不含 _ZH_POLITE_BEFORE_NEG：主语 / 敬语那一格在证据这一步就已经放行返回了。
_ZH_NON_CLAUSE_START_RE = re.compile(f"[^{_ZH_CLAUSE_START_LEFT}]")
_ZH_NEG_VERB_EVIDENCE = (
    "(?:"
    + "(?:"
    + "|".join(_ZH_NEG_UNAMBIGUOUS)
    + f"){_ZH_HSPACE}(?:再)?{_ZH_HSPACE}(?:"
    + "|".join(_ZH_SAY_COMPOUNDS + _ZH_SAY_VERBS)
    + ")"
    + "|(?:"
    + "|".join(_ZH_NEG_JA_AMBIGUOUS)
    + f"){_ZH_HSPACE}(?:再)?{_ZH_HSPACE}(?:"
    + "|".join(_ZH_ZH_ONLY_VERBS)
    + ")"
    + f"|(?<![^{_ZH_CLAUSE_START_LEFT}{_ZH_POLITE_BEFORE_NEG}])(?:"
    + "|".join(_ZH_NEG_JA_AMBIGUOUS)
    + f"){_ZH_HSPACE}再{_ZH_HSPACE}(?:"
    + "|".join(_ZH_SAY_VERBS_JA_SHARED)
    + ")"
    + ")"
)
# 多字否定词不需要左界：``不要 / 不许 / 不許 / 不准`` 都不可能是日文的名词后缀，
# 上面那条左界只为单字的 ``別`` 而设。带上左界反而把正常的中文主语挡在外面——
# ``我不要提君の名は。`` / ``你不准提君の名は。`` 在 parent 上都是好的（codex P2）。
_ZH_MULTI_NEG_EVIDENCE = (
    "(?:" + "|".join(_ZH_NEG_MULTIS) + f"){_ZH_HSPACE}(?:再)?{_ZH_HSPACE}(?:"
    + "|".join(_ZH_SAY_COMPOUNDS + _ZH_SAY_VERBS)
    + ")"
)
# ⚠️ 只收**日文里根本没有的**汉字：``你 妳 您
# 咱 请`` 都不是日文汉字，所以它们后面的 ``別`` 不可能是日文的 ``〜別`` 后缀。
# ``我 / 他 / 請`` 刻意不收——它们是日文汉字，``他別提案をお願いします。`` 这类句子
# 会被放行进来（实测过）。宁可漏判一次，不可把日文残片存进指令表。
_ZH_SUBJECT_BEFORE_NEG = (
    "(?<=[" + _ZH_SUBJECT_CHARS + "])(?:"
    + "|".join(_ZH_NEG_SINGLES)
    + f"){_ZH_HSPACE}(?:再)?{_ZH_HSPACE}(?:"
    + "|".join(_ZH_SAY_COMPOUNDS + _ZH_SAY_VERBS)
    + ")"
)
# ⚠️ 主语和否定词之间会有空格（``你 別提君の名は。``——中英混打时很常见）。上面那条
# 是 lookbehind，只能看紧邻的一个字符，而调用方也只切一个字符的左文，于是空格一进来
# 整条就没了，繁中用户拿不到指令；同一句简体因为 ``别`` 在 _ZH_EVIDENCE_CHARS 里有
# 单字证据，照样是好的——又一处繁简不对称（codex P2）。
# ⚠️ 单开一条**消耗式**的（lookbehind 是定长的，塞不进 ``\s*``），只给它加宽左文。
# 字类证据仍然只看紧邻那一个字符：加宽了的话前一句话的中文字会漏进来，把守卫短路掉。
# ⚠️ 空白有界（``{1,4}``）：这是每条用户消息的热路径。
# ⚠️ 只收**横向**空白。``\s`` 连换行一起吃，于是多行消息里上一行末尾恰好是白名单
# 里的字，就能把下一行整句日文的守卫关掉——``你\n別提案をお願いします。`` 会存下
# ``案をお願いします``（codex P2）。主语和它管的谓语不可能跨行。
_ZH_SUBJECT_LEFT_MAX = 5
_ZH_SUBJECT_GAP = _ZH_HSPACE_ONE + "{1,4}"
_ZH_SUBJECT_ACROSS_SPACE = re.compile(
    "[" + _ZH_SUBJECT_CHARS + "]" + _ZH_SUBJECT_GAP + "(?:"
    + "|".join(_ZH_NEG_SINGLES)
    + f"){_ZH_HSPACE}(?:再)?{_ZH_HSPACE}(?:"
    + "|".join(_ZH_SAY_COMPOUNDS + _ZH_SAY_VERBS)
    + ")"
)
# ⚠️ ``没心情`` 要整条收：``没`` 是日文标准字形（没収），不能进上面的字类，但三个字
# 连在一起是中文独有的。不收的话 ``我没心情聊君の名は。`` 整条被吞，而繁体的
# ``沒心情`` 因为 ``沒`` 在字类里侥幸活着——同一模板内的行为不对称（codex P2）。
# ⚠️ 中文**独有**的句末助词也算证据。它们落在捕获组之外（``別提君の名は吧。`` 的
# ``吧``），而话题本身带日文助词时它们是唯一能证明这是中文指令的东西——不收的话
# 繁中整条 0 命中，同句简体因为 ``别`` 有单字证据而正常（codex P2）。
# ⚠️ 这几个字日文里根本不用，进字类不会像 ``没`` / ``称`` 那样短路守卫。
# ⚠️ 从 _ZH_FINAL_PARTICLE_CHARS **派生**，不再手抄。原先是两份手写清单，正则那份
# 有 ``啊 齁 欸 誒`` 这份没有，于是 ``別提君の名は啊。`` / ``別提君の名は齁。`` 整条
# 0 命中，同句简体和 ``吧`` 变体却是好的（codex P2）。有相等断言钉着两边同步。
# ⚠️ ``了`` 单独去掉：它是日文常用汉字（終了 / 了解），进证据字类就等于给
# ``別提案終了。`` 这类整句日文开洞。``了`` 作为中文证据由 _ZH_EVIDENCE_WORDS 里
# 的「动词 + 了」负责——那是两个字的组合，日文写不出来。
# ⚠️ ``囉 啰`` 是**额外**的：它们因为嘍囉 / 喽啰 而不进正则的助词表（见那里的注释），
# 于是只会落在 term 里面，但作为中文证据同样成立。
_ZH_ZH_ONLY_FINAL_PARTICLES = "".join(
    c for c in _ZH_FINAL_PARTICLE_CHARS if c != "了"
) + "囉啰"

_ZH_EVIDENCE_CHARS = "别说讲谈讨论关这话题愿懒许为甭說這關沒稱" + _ZH_ZH_ONLY_FINAL_PARTICLES
_ZH_EVIDENCE_WORDS = ("叫我", "喊我", "管我叫", "不想", "懶得", "不願", "没心情",
    # ⚠️ 模板 1 也收 _ZH_ADDRESS_VERBS，但只有 叫我 / 喊我 / 管我叫 在上面这批里。
    # ``称`` 是日文标准字形（名称）不能进字类，但 ``称呼我`` 三个字连在一起是中文
    # 独有的。不收的话 ``不要称呼我「君の名は」。`` 整条被吞（codex P2）。
    "称呼我", "稱呼我")
# 落在**句末**的 ``了``：做语气助词时日文不这么用。不带这条的话
# ``別提君の名は了。`` 整条 0 命中，同句简体因为 ``别`` 有单字证据而正常（codex P2）。
#
# ⚠️ 判据要**两边都卡**，只卡右边（句末）会捅出一个比它救回来的大得多的洞：
#   · **右边锚句末**——``了`` 做语气助词时永远在句末。
#   · **左边不许是汉字**——``終了 / 完了 / 修了`` 里的 ``了`` 是词的一部分，是日文
#     最常见的汉语词。只锚右边的话 ``地域別提案は終了。`` 存下非词 ``案は終``、
#     ``別提案の受付は終了。`` 存下 ``案の受付は終``（量下来 8 条日文里错 7 条，
#     换回来的只有 1 条中文——先做的正是这个版本，量完撤了）。
# ⚠️ 因此这条**打在完整命中区间上**，不是挖空后的指令部分：左邻那个字符属于载荷。
# 只读**一个**字符，和 _ZH_NEG_VERB_EVIDENCE 那条左界同样是有界窥视，不是「拿载荷
# 里的中文当证据」——后者是两轮 P2 定死不能做的。
_ZH_TERMINAL_LE_EVIDENCE = r"(?<![一-鿿])了(?=[，。！？；,.!?;]|\s*$)"
_ZH_TERMINAL_LE_RE = re.compile(_ZH_TERMINAL_LE_EVIDENCE)
# 「言说动词 + 了」两个字连在一起是中文，日文写不出来（日文的 ``了`` 只出现在
# 終了 / 了解 这类汉语词里，不做体标记）。``君の名は別提了。`` 就是靠它活下来的。
# ⚠️ 从三张动词表**派生**，不再手抄。原先只手写了 ``提了 說了 讲了 講了``，于是
# 前置话题模板放行的 ``提起 / 提及`` 在证据这一侧不存在——``君の名は別提起了。``
# 整条 0 命中，同句简体正常（codex P2）。派生之后再往任何一张动词表加词，证据
# 自动跟上。
_ZH_VERB_LE_EVIDENCE = tuple(
    verb + "了"
    for verb in dict.fromkeys(
        _ZH_SAY_COMPOUNDS + _ZH_SAY_VERBS + _ZH_PREPOSED_SAY_VERBS
    )
)
_ZH_EVIDENCE_RE = re.compile(
    "|".join(
        (f"[{_ZH_EVIDENCE_CHARS}]",)
        + _ZH_EVIDENCE_WORDS
        + _ZH_VERB_LE_EVIDENCE
        + (_ZH_NEG_VERB_EVIDENCE, _ZH_MULTI_NEG_EVIDENCE, _ZH_SUBJECT_BEFORE_NEG)
    )
)

# (2b) 日文的句法证据：助词 / 助动词 / 敬体词尾。日文**句子**几乎必然出现，而一个
# 被 ban 的专有名词基本不会（ドラえもん / お兄ちゃん 都不含）。判据打在 term 上而
# 不是整段——触发词那一侧本来就是中日共用汉字，看它没有信息量。
# 日文的功能词是**闭集**（不像中文复合词那一维），所以这里按格助词 / 系助词 /
# 助动词 / 接续助词分类列全，不是照着手头语料凑几个（codex P2：只列
# ``のにをはがでと`` 时 "個別提案ください。"、"地域別講座へ申込。" 还是会漏）。
# ⚠️ 唯独不收 ``も``：它出现在 ``ドラえもん`` 里，收了就把上面刚救回来的用例
# 又打回去。``から`` 同类风险（からくりサーカス），但它作为句中助词太常见，留下。
# 日文句末的右界。⚠️ 不只是句读——引号 / 括号里的整句日文同样是句子，收尾括号
# 也算句末：``「別提案あり」`` / ``（別提案なし）`` / ``「別提案だ」`` 里 ``あり``
# 后面直接就是收尾括号，只认句读的话这三条全漏（codex P2）。收括号从
# _ZH_CLOSE_FOR_OPEN 派生，对称引号一并收——它们在这个位置只可能是收尾。
_JA_SENTENCE_END = "[" + re.escape(
    "。、！？" + "".join(sorted(set(_ZH_CLOSE_FOR_OPEN.values()) | _ZH_SYMMETRIC_DELIMS))
) + r"\s]|$"
# 日文的句末终助词。⚠️ 单独放进上面那个单字助词类会打死 ``ドラえもん`` 一族（那正是
# ``も`` 当初被排除的理由），但加上「汉字词干 + 句末」两道界之后就安全了。
# ⚠️ 抽成常量是因为**サ変词尾之后也能再挂一个**：``別提案してね。`` / ``別提案するな。``
# 里 ``して`` / ``する`` 后面跟的就是这批（codex P2）。两处必须同源，各抄一份必漂。
_JA_FINAL_PARTICLES = "か|も|ね|よ|わ|な|さ|ぞ|ぜ|じゃん|かな|かも|だろ"
_JA_GRAMMAR_RE = re.compile(
    "|".join((
        # 助动词 / 敬体词尾
        # ⚠️ ``ください`` / ``できる`` 这类词的**汉字写法**同样常见。表里只写假名形态
        # 等于漏了一半：``別提案下さい。`` / ``別提案出来る。`` 照样被存三天
        #（codex P2，简体同样）。这不是放宽判据，是把已有表项的正字法补齐——
        # ⚠️ 只补**带送假名**的活用形（下さい / 出来る / 出来ない / 出来た），裸的
        # ``出来`` 不能加：``出来高``（日文名词）和中文的 ``提出来`` 都会被误伤。
        "です", "でした", "ます", "ました", "ましょう", "ません",
        "ください", "下さい",
        "である", "らしい", "そうです",
        # 接续 / 复合助词
        "について", "に関して", "という", "ながら", "ので", "けど", "たら",
        # 口语系 copula / 终助词。⚠️ 只收**多字**形式：裸 ``だ`` / ``ちゃ`` 会出现
        # 在专有名词里（お兄ちゃん），收了就把上面救回来的用例又打回去。
        "だね", "だよ", "だな", "だっけ", "でしょ", "かな", "かも", "じゃない",
        "らしい", "みたい",
        # 过去 / 义务 / 被动 / 进行 等谓语形式（codex P2）
        "だった", "だって", "だろう", "すべき", "される", "された", "している",
        "します", "しない", "できる", "出来る", "出来ない", "出来た",
        "しよう", "ている", "ておく", "てある",
        # 格助词・系助词・副助词（多字优先，单字放最后的字符组里）
        "から", "まで", "より", "など", "だけ", "でも", "しか", "ばかり",
        "[のにをはがでとへ]",
        # 存在谓语。日文的电报体（``別提案あり。`` / ``別提案なし。``）整句只有这
        # 两个假名，上面每一条都够不着，于是 ``案あり`` 被当中文存下来（codex P2）。
        # ⚠️ 两侧都要卡死，缺一边就误伤：
        #   · **右边锚在句末**——``あり`` 是 ``ありがとう`` 的前缀，不锚的话
        #     ``别再提ありがとう。`` 整条被吞。日文里它们做谓语时永远在句末。
        #   · **左边要求汉字**——做谓语时前面是汉语词干（``案なし`` / ``案あり``）；
        #     假名接着它就是词的一部分（``おもてなし``），只锚右边的话繁中的
        #     ``別提おもてなし。`` 整条 0 命中，而同一句简体是好的（codex P2）。
        f"(?<=[一-鿿])(?:あり|なし)(?={_JA_SENTENCE_END})",
        # 裸系动词 ``だ``。上面那批口语 copula 只收多字形式，理由是 ``だんご三兄弟``
        # 这类专有名词；但**锚在句末**之后那条顾虑就不成立了（``だんご`` 的 ``だ``
        # 后面是 ``ん``）。不收的话 ``別提案だ。`` 存下 ``案だ``（codex P2）。
        # ⚠️ 和 ``あり|なし`` 一样要**汉字词干**。日文复合句里 ``だ`` 前面是汉语词干
        # （案だ / 座だ / 話だ）；假名接着它就是词的一部分——只锚右边的话繁中的
        # ``別提ただ。`` / ``別提まだ。`` 整条 0 命中，而同句简体是好的（codex P2）。
        f"(?<=[一-鿿])だ(?={_JA_SENTENCE_END})",
        # サ変动词的**基本形 / 过去形**。上面收了 ``します`` / ``しない`` /
        # ``しよう``，却漏了最常用的 ``する`` / ``した``——``別提案した。`` 存下
        # ``案した``（codex P2）。左右两界和 ``あり|なし|だ`` 完全一样：右边锚句末，
        # 左边要汉字词干（``あした`` / ``きのうした`` 这类假名词才不会被误伤）。
        # ⚠️ サ変动词的常用词尾一并收：``して`` / ``したい`` / ``しろ`` / ``せよ``
        # 也是句末谓语（``別提案して。`` 存下 ``案して``；codex P2）。左右两界同上，
        # ``別提そして。`` 里 ``そ`` 是假名、够不着左界，照旧存 ``そして``。
        f"(?<=[一-鿿])(?:する|した|して|したい|しろ|せよ)"
        f"(?:{_JA_FINAL_PARTICLES})?(?={_JA_SENTENCE_END})",
        # 句末终助词。⚠️ ``も`` / ``か`` / ``ね`` 这些**单独**放进上面那个单字助词类会
        # 打死 ``ドラえもん`` 一族（那正是 ``も`` 当初被排除的理由），但加上「汉字词干 +
        # 句末」两道界之后就安全了：``ドラえもん`` 的 ``も`` 前面是假名、也不在句末。
        # 不收的话 ``別提案か？`` / ``今回は、別提案も。`` / ``「別講座じゃん。」`` 会被
        # 存成 ``案か`` / ``案も`` / ``座じゃん``（codex P2）。
        f"(?<=[一-鿿])(?:{_JA_FINAL_PARTICLES})(?={_JA_SENTENCE_END})",
        # 样态 / 传闻的 ``そう``。⚠️ 原先写成字面量 ``そう？``——把**标点**写进了
        # 标记里，而句读永远落在捕获组**之外**（它就是这条指令的终结符），于是那条
        # 分支对 zh 模板是死的，``別提案そう？`` 照样存下 ``案そう``（自动发现守卫先
        # 认出它不可达，codex 随后给出了正解）。改成和上面四条同一形状：右边锚句末，
        # 左边要汉字词干——``別提ドラえもんそう？`` 的 ``そ`` 前面是假名，照旧保留。
        f"(?<=[一-鿿])そう(?={_JA_SENTENCE_END})",
    ))
)


def _is_japanese_sentence_match(
    span: str, term: str, before: str = "", directive: str | None = None,
    stem: str = "", compound_verb: bool = False,
) -> bool:
    """Is this zh-template hit actually a Japanese sentence caught by shared kanji?

    ``別`` is the same codepoint in Japanese and ``提 / 講 / 談 / 討論`` are shared
    kanji, so "個別提案をお願いします。" is structurally a zh hit whose "topic" is
    just the tail of a cut-in-half Japanese sentence. Suppressing those is worth a
    little recall — a bogus term sits in the user's directive store for three days
    and gets injected into every system prompt.

    What must NOT be suppressed is a Chinese sentence whose *ban target* happens to
    be Japanese ("别再提ドラえもん"). Hence the three-way test; see the comments on
    the regexes above.

    Residual: a Traditional trigger + a shared verb + a title that itself carries
    Japanese particles ("別提君の名は。") is indistinguishable from Japanese by any
    local rule and stays suppressed.
    """  # noqa: DOCSTRING_CJK
    # ⚠️ 把左边一个字符接上再搜：``_ZH_NEG_VERB_EVIDENCE`` 的判据是「否定词前面不是
    # 汉字」，而 span 恰好**从否定词开头**，只搜 span 的话那条 lookbehind 永远落空，
    # ``地域別提案をお願いします。`` 会被当成中文（本文件的 ja 语料直接抓出来的）。
    #
    # ⚠️ 中文证据只在**指令部分**搜，不看载荷：日文句子里出现中文片名是正常的，
    # ``世代別講座で中国映画這就是愛について話します。`` 里的 ``這`` 会把整条守卫短路
    # 掉，把日文句子的残片存进指令表（codex P2 两轮——先是加了引号的，后是没加的）。
    # 调用方传进来的 ``span`` 已经把捕获组等长挖空。
    # ⚠️ 必须**等长**挖空：直接删掉的话载荷两侧的字会被拼到一起，凭空造出多字证据
    # （叫+我 / 不+想 / 懶+得 / 不+願 / 喊+我 都实测过）。
    payload = span if directive is None else directive
    if _ZH_EVIDENCE_RE.search(before[-1:] + payload):
        return False
    # 句末的 ``了``：判据打在**完整命中区间**上，见 _ZH_TERMINAL_LE_EVIDENCE。
    if _ZH_TERMINAL_LE_RE.search(span):
        return False
    # 主语和否定词之间隔着空格的那一格，见 _ZH_SUBJECT_ACROSS_SPACE。
    if _ZH_SUBJECT_ACROSS_SPACE.search(before + payload):
        return False
    # 命中区间**左边紧挨着**假名 → 触发词是日文能产的 ``〜別`` 后缀（カテゴリ別 /
    # ジャンル別 / テーマ別），不是中文的祈使 ``別``。中文句子里 ``別`` 前面不会
    # 直接贴假名。这一条打的是 span 之外的字符，所以 term 本身不含助词也拦得住
    # （"ジャンル別討論スレ" 的 term 是 ``スレ``，(2b) 够不着）。
    if before and _KANA_RE.search(before[-1]):
        return True
    # 快速退出：没假名就不可能是日文句子。(2b) 的判据本身全是假名、且 term 是 span
    # 的子串，所以这一行不是独立条件，只是省掉一次 regex——热路径每条用户消息
    # 每条模板都会走到。
    if not _KANA_RE.search(span):
        return False
    # 日文的 ``〜別 + 复合名词 + 光杆假名名词`` 这一格：``地域別提案スレ`` /
    # ``A別講座スレ``。触发词吃掉 ``別提``，term 只剩 ``案スレ``——``案`` 是补全日文
    # ``提案`` 的那半个词，``スレ`` 是个光杆名词，一个语法助词都没有，下面每一条都
    # 够不着，于是非词被当中文存三天（codex P2）。parent 没有这一格，是本 PR 放行
    # 繁体 ``別`` 带出来的。
    #
    # 判据是**左界 + term 的形状**两条一起：
    #   · 左边有字且不是小句边界——``〜別`` 是后缀，前面必然挂着标签词（地域 / A /
    #     カテゴリ）。走到这里已经意味着「否定词有歧义、动词是共用的、整段没有中文
    #     证据」，所以不必再判动词。
    #   · term 是**恰好一个汉字**接着假名。日文那一侧的复合名词是开集（提案 / 提出 /
    #     提供 / 提示 / 提携 / 講座 / 講義…），枚举不干净；而「一个汉字 + 假名」这个
    #     形状是闭的，指的就是「复合词的后半 + 假名尾巴」。
    # ⚠️ 代价只落在**单字汉字 + 假名**的标题上（``動漫別提蘭ちゃん。``）。
    # ``動畫別提ドラえもん。``（term 直接以假名开头）、``遊戲別提初音ミク。`` /
    # ``別提美少女戰士セーラームーン。``（两个字以上的汉字词头）都照旧保留——这三条
    # 正是这道守卫从第一轮起就在保的东西。
    # ⚠️ 动词是**双字**共用复合词（討論）时，日文那半个词已经整个进了触发词，
    # term 直接以假名开头——``地域別討論スレ。`` 的 term 是 ``スレ``，形状这一条
    # 永远不成立，非词照存（codex P2）。把词干接回来再判形状。
    # ⚠️ **只**在双字动词时接。单字动词（提 / 講 / 談）接了的话
    # ``動畫別提ドラえもん。`` 的 ``提ドラえもん`` 也成了「一个汉字接假名」，
    # 把这道守卫从第一轮起就在保的东西打死（试过，实测回归）。
    shaped = stem + term if compound_verb else term
    if (
        before
        and _ZH_NON_CLAUSE_START_RE.match(before[-1])
        and _HAN_RE.match(shaped[:1])
        and _KANA_RE.match(shaped[1:2])
    ):
        return True
    # ⚠️ 判据要连上**被触发词吃掉的那个汉字**。``あり|なし`` / ``だ`` / ``する|した``
    # / 句末终助词这四条都要求左边是汉语词干，而词干正好是动词的最后一个字：
    # ``別討論あり。`` 里 ``討論`` 整个进了触发词，term 只剩 ``あり``，左界永远落空，
    # 于是 ``あり`` / ``なし`` / ``した`` 被当中文存三天，而结构完全一样的
    # ``別提案あり。``（term = ``案あり``）是拦住的（codex P2）。
    # ⚠️ 只接**一个**汉字，且只在它确实是汉字时接：多接等于把触发词整段塞进判据，
    # 而上面注释里写过，触发词那一侧是中日共用汉字、看它没有信息量。接一个字不会
    # 凭空造出假名匹配——四条判据要的都是「汉字紧跟假名」，而 ``別提ドラえもん。``
    # / ``別提おもてなし。`` 里假名前面还是假名，接完照样不匹配（实测）。
    return bool(_JA_GRAMMAR_RE.search(stem + term))

_PATTERNS_RAW: List[Tuple[str, str, str]] = [
    # ---------- zh ----------
    # 别/不要/不许/不准 + （再）+ 动词 + 对象
    # terminator 不放 ``\s``：zh 句子里中英混说时（"别叫我 John Smith"）lazy
    # ``(.{1,40}?)`` 会在第一个空格切断成 "John"。让终结符必须是标点 / EOL /
    # 句末助词，多词 NP 才能被完整捕获（codex P2）。
    # 繁体 ``不準`` 不收：它是"不准确"的意思，"測量不準說明有問題" 会被抓成
    # ban_topic；"不允许" 这个义项繁体本来就写 ``不准``，已在表内。
    ("zh", "ban_topic",
     _ZH_NEG + f"{_ZH_HSPACE}(?:再)?{_ZH_HSPACE}"
     # 动词表见 _zh_verb_alternation：复合动词必须排在单字前缀之前（模板 2/4 要求
     # 动词后紧跟终结符，失败会回溯，所以没这个问题）。
     # ⚠️ 动宾之间的空白也只收横向：触发词齐了但换了行的话，下一行会被当成宾语接上来
     # ——``別再提`` 换行 ``案をお願いします。`` 存下 ``案をお願いします``（codex P2）。
     # 和触发词内部、主语间隔、停顿之后同一条判据：一条指令不跨行。
     + _ZH_VERBS_WITH_ADDRESS + _ZH_HSPACE
     # ⚠️ 宾语下限是 2 不是 1：可选助词组 + lazy 宾语会让正则优先把话题的最后一个字
     # 当成助词（"别再提拿捏。" → 宾语 "拿"、助词 "捏"），削到 1 字后撞长度下限、
     # 整条指令消失。1 字宾语本来也只能产出 1 字 term 必被丢，抬下限只赚不亏。
     + _ZH_TOPIC_SEPARATOR
     + _ZH_OBJECTLESS_AHEAD
     + "(" + _zh_topic(2, 40) + r")" + _ZH_FINAL_PARTICLES + r"?(?:[，。！？；,.!?;]|\s*$)"),
    # X + 这个? + 别(再)+ 提
    # ``关于 X 就别提了`` 归模板 4 管。本模板不排掉它的话，同一句会同时产出这里的
    # "关于股票就" 和模板 4 的 "股票" 两条 term——前者是垃圾却照样占一个 active
    # 名额、往 system prompt 里注三天（codex P2；简繁两侧都有，既有缺陷）。
    # 两道排除缺一不可：前缀不能**以** 关于 开头，后一个 lookahead 挡住"从 关于 的
    # 第二个字起匹配"（否则退化成 "于股票"）。lookahead 里带 lookbehind 是为了只挡
    # ``关|于`` 这一个切点——写成 ``(?<![关關])`` 会把 "有关工作别提了" 一起打死。
    # ⚠️ 只挡开头，不是 tempered token 挡"前缀里任意位置含 关于"：书名 / 片名里带
    # 关于 是正常的（"电影《关于爱》别提了。"），挡整段会把整条指令打没（codex P2）。
    # 代价是 ``关于`` 前面还有别的字时（"我觉得关于股票就别再讲了"）仍会多产出一条
    # 长 term——那是既有行为，不是本 PR 引入的。
    # 尾部的 ``的 / 的事 / 这个 / 就`` 与模板 4 对齐，让前缀停在真正的话题上；``的``
    # 单独可选是因为 "減肥的這件事別再說了。" 这种自然说法里它和指示词是分开的。
    ("zh", "ban_topic",
     # ⚠️ 前缀下限是 2 不是 1：三个可选填充组 + lazy 前缀会让正则优先把话题的最后
     # 一个字塞进填充组，主语只有一个字时（"钱的事别提了。"）前缀被削成 1 字、撞上
     # ``2 <= len(term)`` 的下限，整条指令消失。下限提到 2 之后正则会改选更长的
     # 前缀；1 字前缀本来也只能产出 1 字 term、必然被丢，所以抬下限只赚不亏。
     # ``的`` 绑在指示词里、不单独可选：单独可选会把 "目的这个别提了。" 的 目的 切成
     # 目（对抗排查）。句尾的 ``就`` 不在正则里吃，见 _ZH_TRAILING_FILLERS。
     # ⚠️ 下面这一串 ``\s*`` 必须**原子化**：前置话题的单字分支也匹配空格，于是话题
     # 和后面每个 ``\s*`` 能任意瓜分同一串空白，把「在哪切」变成组合爆炸。实测
     # ``extract_directives(" " * 60)`` 要 0.42 秒，而这条路径是每条用户消息同步跑
     # 的——发一条纯空白消息就能卡住（codex P1）。原子化之后回到 parent 的量级。
     # ⚠️ 原来还有一处重复的 ``\s*\s*``，一并合掉。
     # ⚠️ 动词**之后**那个 ``\s*(?:了)?`` 不能原子化——它后面的终结符字符类里含
     # ``\s``，原子化会把本该当终结符的那个空格吃掉。
     #
     # ⚠️ 本模板**不吃** ``的事``：它没有 ``关于`` 那样的锚，``的事`` 就是话题本身的
     # 一部分——``我们的事别提了。`` 会被削成 ``我们``、``前女友的事别提了。`` 被削成
     # ``前女友``（base 两条都完整；codex P2）。存下 ``我们`` 意味着让模型回避用户
     # 本人而不是那件事，代价方向完全反了。模板 4 保留它，那里由句首的 ``关于`` 锚定。
     r"(?!(?<=关)于)(?!(?<=關)於)("
     # ⚠️ 话题和触发词之间也只收横向空白：上一行会被当成前置话题接下来——
     # ``工作正常`` 换行 ``別提了。`` 存下 ``工作正常``（codex P2）。和另外四处
     # 同一条判据：一条指令不跨行。
     + _zh_topic(2, 30, block_guanyu=True) + r")" + f"(?>{_ZH_HSPACE})"
     # ⚠️ 前置话题和触发词之间也会有停顿标点（``工作，別提了。``）。话题字符类排掉了
     # ``，``，而这里原先只允许空白，于是整条 0 命中——parent 存的是 ``工作``
     # （codex P2）。和动词后宾语那一侧同一个常量，外加停顿后的 ``就``。
     + _ZH_PAUSE_THEN_JIU
     + r"(?:的?(?:这个|這個|这事|這事|这话题|這話題|这件事|這件事))?"
     + f"(?>{_ZH_HSPACE})"
     # ⚠️ 填充词**之后**也会再停顿一次：``工作，这件事，别提了。`` 是很自然的说法，
     # 只在填充词前面收停顿标点的话，正则会从第一个逗号之后重新起匹配、存下
     # ``这件事``（codex P2）。``就`` 同理要在两个位置都收。
     + _ZH_PAUSE_THEN_JIU
     + f"[别別](?>{_ZH_HSPACE})(?:再)?(?>{_ZH_HSPACE})"
     # ⚠️ 终结符里的空白也只收横向：触发词落在行末时，换行本身会被当成「这条指令
     # 说完了」，于是上一行被绑成话题——``工作正常別提`` 换行 ``下一句。`` 存下
     # ``工作正常``（codex P2）。同一行的空格照旧算终结（``工作别提 然后…``）。
     # ⚠️ 触发词表见 _ZH_PREPOSED_SAY_VERBS：它和中文证据表是派生关系，别写死在这里。
     # 复合词必须排在单字前缀之前（提起 / 提及 在 提 之前），常量本身已经是这个顺序。
     + r"(?:提了|" + "|".join(_ZH_PREPOSED_SAY_VERBS) + r")"
     # ⚠️ ``了`` 之前的空白同样只收横向，且横向空白当终结符时后面不许再跟行分隔符
     # ——``工作正常別提 `` 换行 ``下一句。`` 靠那个尾随空格照样跨了行（codex P2）。
     + _ZH_HSPACE + r"(?:了)?(?:[，。！？；,.!?;]|"
     + _ZH_HSPACE_ONE + f"(?!{_ZH_HSPACE}[{_ZH_LINE_SEP}])" + r"|$)"),
    # 不想/不愿 + 聊/讨论 + X — 同上：terminator 不要 \s，否则多词 NP 被切
    ("zh", "ban_topic",
     r"(?:我)?" + _ZH_HSPACE
     + r"(?:" + "|".join(_ZH_RELUCTANCE) + r")"
     + _ZH_HSPACE + r"(?:再)?" + _ZH_HSPACE
     + _ZH_VERBS_PLAIN
     + _ZH_HSPACE
     + _ZH_TOPIC_SEPARATOR
     # ⚠️ 本模板也**不吃** ``的事``（模板 1/2/4 已经各撤过一次，同一个理由）：它是
     # 领属加名物化，可以是名字本身的一部分——``我沒心情聊我們的事。`` 会存成
     # ``我們``，让模型回避用户本人而不是那件事（codex P2）。``了`` 保留，它是纯语气。
     + _ZH_OBJECTLESS_AHEAD
     # ⚠️ ``了`` 之前的空白也只收横向：和句末助词那一格同一条判据（一条指令不跨行）。
     # 这一格不是 codex 报的，是把结构守卫从「捕获组之前」放宽到**整条模板**之后
     # 自己冒出来的第十格。
     + r"(" + _zh_topic(2, 40) + r")(?:" + _ZH_HSPACE + r"了)?"
     + r"(?:[，。！？；,.!?;]|\s*$)"),
    # 关于 X + 别(再)+ 说
    ("zh", "ban_topic",
     # ⚠️ 只有本模板保留 ``(?:就)?``：它由句首的 ``关于`` 锚定，"关于 X 就别…" 的
     # 结构是显式的，被腰斩的风险仅限于 ``关于`` + 就尾词（"关于功成名就别提了"，
     # 极罕见）。模板 2 没有这个锚，覆盖的是全部 "X别提了" 句子——成就 / 迁就 /
     # 功成名就 都住在那里，所以那边一个字都不吃，交给 _drop_filler_suffixed_terms。
     # ⚠️ 触发词之前的每个 ``\s*`` 都要原子化，理由同模板 2：话题的单字分支也匹配
     # 空格，能和后面每个 ``\s*`` 任意瓜分同一串空白。上一轮只改了模板 2、漏了这条，
     # ``"关于" + " " * 80`` 要 3 秒（codex P1 第二轮）。
     # ⚠️ 本模板也**不吃** ``的事``（模板 2 已经撤过一次，同一个理由）：``的事`` 是
     # 领属加名物化，可以是名字本身的一部分——``关于我们的事别提了。`` 存成 ``我们``、
     # ``关于我前女友的事就别提了。`` 存成 ``我前女友``（codex P2）。更糟的是这个短
     # term 会让 _drop_filler_suffixed_terms 把模板 2 抽到的**正确** term ``我们的事``
     # 当成「它 + 一个填充词」删掉，于是只剩那个被截短的。
     #
     # ⚠️ ``这个 / 这话题 / 这件事`` 那组**保留**：它们是指示词，无歧义地是填充
     # （``关于减肥这话题就别说了。`` 的话题就是 ``减肥``）。``(?:就)?`` 也保留：
     # 删掉它 ``关于股票就别提了。`` / ``关于工作就别提了。`` 会退成带 ``就`` 的垃圾
     # （实测比较过两个方案）。
     r"(?:关于|關於)" + f"(?>{_ZH_HSPACE})"
     # ⚠️ 话题引导词**之后**也会停顿一次（``关于，工作，就别提了。``——口述转写和
     # 打字对话里很常见）。本模板是 ``就`` 的唯一去处（模板 2 的 ``(?:就)?`` 已经
     # 因为 ``成就 / 迁就`` 撤掉了），所以这里不收停顿就等于整句 0 命中，连模板 2
     # 都接不住——parent 靠模板 2 的 ``(?:就)?`` 存下 ``工作``（codex P2）。
     + _ZH_TOPIC_SEPARATOR
     # ⚠️ 同上：``關於工作`` 换行 ``別提了。`` 也不该拼成一条。
     + r"(" + _zh_topic(2, 30) + r")" + f"(?>{_ZH_HSPACE})"
     # ⚠️ 同模板 2：``關於工作，就別提了。`` 里的停顿标点要挡在捕获组外面。
     + _ZH_TOPIC_SEPARATOR
     + r"(?:的?(?:这个|這個|这事|這事|这话题|這話題|这件事|這件事))?"
     + f"(?>{_ZH_HSPACE})"
     # ⚠️ 同模板 2：填充词之后、``就`` 之后都可能再停顿一次
     + _ZH_TOPIC_SEPARATOR
     + r"(?:就)?" + f"(?>{_ZH_HSPACE})"
     + _ZH_TOPIC_SEPARATOR
     + f"[别別](?>{_ZH_HSPACE})(?:再)?(?>{_ZH_HSPACE})"
     # ⚠️ 同模板 2：终结符里的空白只收横向。
     # ⚠️ 触发词表也和模板 2 **同源**。这里原先还写死着，于是 ``關於工作就別提起了。``
     # 走不进这条专用模板、退回通用前置话题模板，把填充词一起存下来（``工作就``），
     # 而 ``關於工作就別提了。`` 是好的（codex P2，简体同样）。
     # ⚠️ 这一格是我自己那条「触发词表不许写死」的守卫**漏掉的**——它只查了模板 2，
     # 典型的清单式漏项。守卫已改成遍历所有模板。
     + r"(?:" + "|".join(_ZH_PREPOSED_SAY_VERBS) + r")"
     + _ZH_HSPACE + r"(?:了)?(?:[，。！？；,.!?;]|"
     + _ZH_HSPACE_ONE + f"(?!{_ZH_HSPACE}[{_ZH_LINE_SEP}])" + r"|$)"),

    # ---------- en ----------
    # stop/don't/quit + verb + (about|saying) + X
    # ``X`` 是英文 NP，常带空格（"my ex"、"the weather"）。terminator 用
    # filler-word / 标点 / 句尾，避免 lazy ``.{1,40}?`` 在 X 内的第一个空格就
    # 切断成 "my"。
    ("en", "ban_topic",
     r"(?:please\s+)?(?:stop|quit|don'?t|do\s+not|no\s+more)\s+"
     r"(?:talking\s+about|talk\s+about|saying|say|mentioning|mention|"
     r"bringing\s+up|bring\s+up|going\s+on\s+about|"
     r"calling\s+me\s+a|calling\s+me|call\s+me\s+a|call\s+me)\s+"
     r"(.{1,40}?)"
     r"(?:\s+(?:again|anymore|any\s+more|please|ever|already|now|"
     r"forever|today|tonight|right\s+now|in\s+(?:front|public))"
     r"|[,.!?;]|$)"),
    # X + is off limits / off the table / not a topic
    ("en", "ban_topic",
     r"(.{1,30}?)\s+is\s+(?:off[\s\-]?limits|off\s+the\s+table|a\s+(?:no[\s\-]?go|forbidden)\s+topic)"
     r"(?:[\s,.!?;]|$)"),
    # I don't want to talk/hear about X
    # X 是 NP 可能含空格（"my ex girlfriend"）。terminator 用 filler-word /
    # 标点 / 句尾，否则 lazy ``.{1,40}?`` 在第一个空格就切断成 "my"（codex P1）。
    ("en", "ban_topic",
     r"i\s+(?:don'?t|do\s+not|really\s+don'?t)\s+(?:want\s+to|wanna)\s+"
     r"(?:talk|hear|discuss|think)\s+(?:about|of)\s+(.{1,40}?)"
     r"(?:\s+(?:anymore|any\s+more|again|ever|already|right\s+now|today|tonight|please)"
     r"|[,.!?;]|$)"),
    # drop the X / leave X alone (subject)
    ("en", "ban_topic",
     r"(?:drop|leave\s+alone)\s+(?:the\s+|that\s+)?(.{1,30}?)\s+"
     r"(?:topic|subject|thing|stuff|already)(?:[\s,.!?;]|$)"),

    # ---------- ja ----------
    # X + のこと/について + は + もう + 言わないで/やめて/しないで
    ("ja", "ban_topic",
     r"(.{1,40}?)\s*(?:のこと|の話|について|に関して|っていう話)\s*"
     r"(?:は)?\s*(?:もう|二度と|これ以上)?\s*"
     r"(?:言わないで|話さないで|しないで|やめて|止めて|よして|聞きたくない|触れないで)"),
    # もう + X + (の話) + (は) + 嫌だ/聞きたくない
    ("ja", "ban_topic",
     r"もう\s*(.{1,40}?)\s*(?:のこと|の話)?\s*(?:は)?\s*"
     r"(?:嫌|いや|聞きたくない|話したくない|やめて)"),
    # X + って + 呼ばないで / 言わないで
    ("ja", "ban_topic",
     r"(.{1,30}?)\s*(?:って|とは|なんて)\s*"
     r"(?:呼ばないで|言わないで|呼ぶな|言うな)"),

    # ---------- ko ----------
    # X + (에 대해|얘기|이야기) + (는)? + 그만 / 하지 마 / 꺼내지 마
    ("ko", "ban_topic",
     r"(.{1,40}?)\s*(?:에\s*대해서?|얘기|이야기|소리|말)\s*(?:는|은)?\s*"
     r"(?:그만|하지\s*마(?:세요|십시오)?|꺼내지\s*마(?:세요)?|관두|치워)"),
    # 다시는 + X + 말하지 마 / 꺼내지 마
    ("ko", "ban_topic",
     r"(?:다시는|두\s*번\s*다시|이제)\s*(.{1,40}?)\s*"
     r"(?:말하지|꺼내지|언급하지)\s*마(?:세요|십시오)?"),
    # X + (이|가)? + 듣기 싫다 / 짜증나
    ("ko", "ban_topic",
     r"(.{1,30}?)\s*(?:이|가)?\s*(?:듣기\s*싫|말하기\s*싫|짜증나|지긋지긋)"),

    # ---------- ru ----------
    # не говори / хватит про / прекрати + (preposition)? + X
    # 介词 "про / о / об / обо" 出现在动词后 + term 前，必须先 consume 才能
    # 让 (.{1,40}?) 捕获到实际话题；否则贪心地把介词当 term。
    # term 用 en 同款 filler-word terminator，支持 "моей бывшей" 这类多词短语。
    ("ru", "ban_topic",
     r"(?:не\s+(?:говори|упоминай|повторяй|произноси|обсуждай|называй\s+меня)|"
     r"хватит\s+(?:говорить|обсуждать|упоминать)|"
     r"перестань\s+(?:говорить|обсуждать|упоминать|называть\s+меня)|"
     r"прекрати\s+(?:говорить|обсуждать|упоминать|называть\s+меня))\s+"
     r"(?:про\s+|обо?\s+|о\s+)?"  # 可选介词
     r"(.{1,40}?)"
     r"(?:\s+(?:больше|никогда|пожалуйста|снова|опять|вообще|сегодня)"
     r"|[,.!?;]|$)"),
    # о X + больше + не говори
    ("ru", "ban_topic",
     r"(?:обо|об|о)\s+(.{1,30}?)\s+больше\s+не\s+(?:говори|упоминай)"),
    # я не хочу + (говорить|слышать) + о X — 同 en 的 filler-word terminator，
    # 支持 "моей бывшей" 这种多词短语。
    ("ru", "ban_topic",
     r"я\s+не\s+хочу\s+(?:говорить|слышать|обсуждать)\s+(?:обо|об|о)\s+(.{1,40}?)"
     r"(?:\s+(?:больше|никогда|пожалуйста|снова|опять|вообще|сегодня)"
     r"|[,.!?;]|$)"),

    # ---------- es ----------
    # no hables / no menciones / deja de hablar + (de|sobre) + X
    ("es", "ban_topic",
     r"(?:no\s+(?:hables|menciones|digas|sigas\s+hablando|me\s+llames)|"
     r"deja\s+de\s+(?:hablar|mencionar|llamarme)|"
     r"para\s+de\s+(?:hablar|mencionar))\s+"
     r"(?:de|sobre|acerca\s+de)?\s*(.{1,40}?)"
     r"(?:\s+(?:más|nunca|jamás|otra\s+vez|de\s+nuevo|por\s+favor|porfa|hoy|ahora)"
     r"|[,.!?;]|$)"),
    # no quiero + (oír|hablar|saber) + (de|nada de) + X — 同 en/ru
    ("es", "ban_topic",
     r"no\s+quiero\s+(?:oír|hablar|saber|escuchar)\s+(?:nada\s+)?(?:de|sobre)\s+"
     r"(.{1,40}?)"
     r"(?:\s+(?:más|nunca|jamás|otra\s+vez|de\s+nuevo|por\s+favor|porfa|hoy|ahora)"
     r"|[,.!?;]|$)"),

    # ---------- pt ----------
    # não fale / não mencione / pare de falar + (de|sobre) + X
    ("pt", "ban_topic",
     r"(?:não\s+(?:fale|mencione|diga|continue\s+falando|me\s+chame)|"
     r"pare\s+de\s+(?:falar|mencionar|me\s+chamar)|"
     r"deix[ea]\s+de\s+(?:falar|mencionar))\s+"  # deixe de / deixa de（codex P2）
     r"(?:de|sobre|a\s+respeito\s+de)?\s*(.{1,40}?)"
     r"(?:\s+(?:mais|nunca|jamais|de\s+novo|outra\s+vez|por\s+favor|hoje|agora)"
     r"|[,.!?;]|$)"),
    # não quero + (ouvir|falar|saber) + (de|sobre|nada de) + X — 同 en/ru
    ("pt", "ban_topic",
     r"não\s+quero\s+(?:ouvir|falar|saber|escutar)\s+(?:nada\s+)?(?:de|sobre)\s+"
     r"(.{1,40}?)"
     r"(?:\s+(?:mais|nunca|jamais|de\s+novo|outra\s+vez|por\s+favor|hoje|agora)"
     r"|[,.!?;]|$)"),
]


# 惰性 compile，不在 import 时做。
#
# 这 21 条模板里有 4 条各约 51 KB 正则源码，合计 209 KB；模块级 compile 实测
# 294-298 ms。而这个模块坐在 memory_server 的 eager 导入链上
# （app/__init__.py -> app/runtime_bindings.py -> memory.user_directives），
# memory_server 又是 merged 模式下第一个被 import 的 app 模块。uvicorn 先
# await lifespan.startup() 再 create_server()，所以这段时间全花在**端口还不存在**
# 的阶段——用户那边是 connection-refused，不是"慢"。
#
# 真正需要它的是用户开口之后的指令抽取。改成首次访问时才编译，并在
# utils/module_warmup.py 的预热表里登记，服务 ready 之后由后台线程提前编好，
# 首次真实抽取也不用等——与那几个 LLM SDK 的处理同构。
_DIRECTIVE_PATTERNS_CACHE: List[Tuple[str, str, "re.Pattern[str]"]] | None = None
_DIRECTIVE_PATTERNS_LOCK = threading.Lock()


def _directive_patterns() -> List[Tuple[str, str, "re.Pattern[str]"]]:
    global _DIRECTIVE_PATTERNS_CACHE

    cached = _DIRECTIVE_PATTERNS_CACHE
    if cached is None:
        with _DIRECTIVE_PATTERNS_LOCK:
            if _DIRECTIVE_PATTERNS_CACHE is None:
                # 整列表建好再赋值：别的线程要么看到 None、要么看到完整的一份，
                # 不会读到编译到一半的列表。
                _DIRECTIVE_PATTERNS_CACHE = [
                    (locale, kind, re.compile(raw, re.IGNORECASE | re.UNICODE))
                    for locale, kind, raw in _PATTERNS_RAW
                ]
            cached = _DIRECTIVE_PATTERNS_CACHE
    return cached


def __getattr__(name: str) -> object:
    # DIRECTIVE_PATTERNS 是这个模块的公开名字（测试和外部都按名字取），保持可用；
    # 只是取它的那一刻才付编译代价。PEP 562 对 `from ... import X` 同样生效。
    if name == "DIRECTIVE_PATTERNS":
        return _directive_patterns()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def extract_directives(text: str) -> List[Tuple[str, str, str]]:
    """Run every locale × kind template over a user text; returns ``[(locale, kind, term)]``.

    - All templates are tried **in parallel**, with no upfront language detection
    - On a hit the term is cleaned by ``_trim_term``; its length must be ∈ [2, 40]
    - Each ``(kind, term_lower)`` is kept only once in the result list (keeping the
      first matching locale; duplicate storage is deduped again by
      ``UserDirectivesManager.record``)

    The repetition is deliberate: with upstream mixed-language input one sentence may
    hit patterns from multiple locales; deduping here avoids one sentence producing 5
    records, while **different** terms from the same sentence ("别提小明和小红") are
    still each recorded — provided the template can split out two matches.
    """  # noqa: DOCSTRING_CJK
    if not text:
        return []
    # ⚠️ 去重必须放在 _drop_filler_suffixed_terms **之后**：填充词过滤靠命中区间
    # 认「同一条指令的两种切法」，而去重会把重复 term 连同它的区间一起扔掉。
    # "股票别提了。关于股票就别提了。" 里第二条指令的 ``股票`` 因为和第一条同名被去掉，
    # 过滤器就只看得到第一条那个**不重叠**的区间，于是 ``股票就`` 逃过一劫（codex P2）。
    out: List[Tuple[str, str, str]] = []
    spans: List[Tuple[int, int]] = []
    for locale, kind, pat in _directive_patterns():
        # 同上：不手写 startswith("zh")，走公共的 fallback-family 判定。
        zh_family = prompt_locale_fallback_key(locale) == "zh"
        # ⚠️ 不能直接 finditer：日文守卫否掉一条命中之后，那整段区间已经被消费掉了，
        # 藏在里面的**真指令**再也扫不到——``地域別提案をお願いします 别再提工作。``
        # 整条 0 命中，而 parent 还能抓到后面那句 ``工作``（codex P2）。所以手动推进
        # 游标：正常命中跳到区间末尾，被守卫否掉的只跳一个字符，从起点之后重扫。
        # ⚠️ 只对被**否掉**的那条回退，正常命中照旧整段跳过——否则同一条指令会被
        # 反复抽出来，而且是 O(n²)。
        pos = 0
        while pos <= len(text):
            m = pat.search(text, pos)
            if m is None:
                break
            pos = m.end() if m.end() > m.start() else m.start() + 1
            try:
                term_raw = m.group(1)
            except IndexError:
                continue
            term = _trim_term(term_raw, locale)
            if not (_TERM_MIN_LEN <= len(term) <= _TERM_MAX_LEN):
                continue
            # zh 模板与日文共用 別/提/講/談/討論 这些汉字，日文句子会被抓成
            # ban_topic（见 _is_japanese_sentence_match）。只对 zh 生效——ja 模板
            # 本身要求假名，套上去会把自己全部否掉。
            # ⚠️ 只切**一个**字符：守卫读的就是 before[-1:]，切整段前缀等于每条命中
            # 复制一次全文——一条消息里几万条指令时是二次方（60000 条 2.5 秒，base
            # 1.2 秒；codex P2）。
            # ⚠️ 判据要看**未 trim** 的捕获：假名助词表现在对所有 locale 生效，
            # ``地域別講座だね。`` 的 ``だね`` 会在 trim 里被剥掉，等守卫拿到 term 时
            # 日文语法标记已经没了，整句反被判成中文（补假名回落时踩到的）。
            # 指令部分 = 命中区间去掉被捕获的话题（等长空格填充，保住相对位置、也
            # 避免两侧的字被拼到一起）。载荷里的中文不该当中文证据。
            span = m.group(0)
            payload_lo = m.start(1) - m.start()
            payload_hi = m.end(1) - m.start()
            directive_only = (
                span[:payload_lo] + " " * (payload_hi - payload_lo) + span[payload_hi:]
            )
            # ⚠️ 只有**证据**看指令部分；假名和日文语法那两条判据仍看完整命中区间。
            # 传挖空后的串进去会把假名一起挖掉，``地域別提案をお願いします。`` 会因为
            # 「没有假名」被判成中文（补这条时踩到的）。
            # 捕获组**左边紧邻**的那个字符（在命中区间之内，即触发词的末字）。日文的
            # 汉字词干判据要靠它，见 _is_japanese_sentence_match 结尾。
            stem = span[payload_lo - 1: payload_lo] if payload_lo else ""
            if zh_family and _is_japanese_sentence_match(
                span, m.group(1),
                text[max(0, m.start() - _ZH_SUBJECT_LEFT_MAX): m.start()],
                directive=directive_only,
                stem=stem if _HAN_RE.match(stem) else "",
                compound_verb=span[:payload_lo].endswith(
                    _ZH_COMPOUND_JA_SHARED_VERBS
                ),
            ):
                # 从命中**起点之后**重扫，把藏在这段里的真指令捞回来。
                pos = m.start() + 1
                continue
            out.append((locale, kind, term))
            spans.append((m.start(), m.end()))
    seen: set[tuple[str, str]] = set()
    deduped: List[Tuple[str, str, str]] = []
    for locale, kind, term in _drop_filler_suffixed_terms(out, spans):
        key = (kind, term.casefold())
        if key in seen:
            continue
        seen.add(key)
        deduped.append((locale, kind, term))
    return deduped


def _drop_filler_suffixed_terms(
    hits: List[Tuple[str, str, str]],
    spans: List[Tuple[int, int]] | None = None,
) -> List[Tuple[str, str, str]]:
    """Drop a term that is just another extracted term plus a trailing filler word.

    "關於股票就別再講了" matches two templates: the generic one stops at ``股票就``
    (the adverb ``就`` belongs to the sentence, not the topic) and the dedicated
    ``关于`` one yields ``股票``. Both would be persisted and injected for three days.

    Swallowing the filler inside the regex is the obvious fix and it is wrong: the
    prefix is lazy, so the engine prefers to feed it the topic's **last character**
    instead — "他的成就别提了。" comes out as ``他的成``. A left-edge character
    blocklist cannot rescue that either, because words ending in ``就`` are an open
    set (成就 / 迁就 / 功成名就 / 一蹴而就 / 練就 …) and one omission truncates a real
    topic.

    Comparing after the fact needs no word-boundary guess at all: ``股票就`` goes
    because ``股票`` was also extracted from the same message, while ``功成名就``
    stays because ``功成名`` never was.

    ⚠️ The comparison is restricted to **overlapping** matches, i.e. the two
    templates firing on the *same* directive. Without that,
    "功成名就别提了，功成名别提了。" — two separate directives that happen to differ
    by a ``就`` — would lose the first one (codex P2). ``spans`` carries that
    provenance positionally alongside ``hits``; omit it and nothing is suppressed,
    which is the safe direction.
    """  # noqa: DOCSTRING_CJK
    if len(hits) < 2 or not spans or len(spans) != len(hits):
        return hits
    # 只有"末尾正好是一个填充词"的 term 才可能被抑制。绝大多数消息里一条都没有，
    # 先筛一遍就把 O(n²) 的重叠扫描降到 O(n)——n 是同一条消息里的命中数，粘贴一大段
    # 聊天记录时可以到几百（codex P2）。
    suspects = [
        index
        for index, (_locale, _kind, term) in enumerate(hits)
        if any(term.endswith(filler) for filler in _ZH_TRAILING_FILLERS)
    ]
    if not suspects:
        return hits
    suspect_set = set(suspects)

    # ⚠️ 光筛 suspects 不够：话题本身就以填充词结尾时（``成就别提了。`` 重复几千遍）
    # 每条命中都是 suspect，逐条再扫全表又变回 O(n²)——4000 条要 1.25 秒，而这条
    # 路径是同步跑在用户消息上的（codex P2）。
    #
    # 命中区间的长度有上界（模板本身有 {,40} 之类的限制），所以按起点分桶之后，
    # 可能与某条命中重叠的邻居只会落在它自己和左右几个桶里，查找变成 O(1)。
    _BUCKET = 128
    _span_buckets: dict[int, List[int]] = {}
    for other_index, (start, end) in enumerate(spans):
        for bucket in range(start // _BUCKET, end // _BUCKET + 1):
            _span_buckets.setdefault(bucket, []).append(other_index)

    def _overlaps(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
        return a[0] < b[1] and b[0] < a[1]

    def _is_redundant(index: int) -> bool:
        locale, kind, term = hits[index]
        start, end = spans[index]
        neighbours = {
            other_index
            for bucket in range(start // _BUCKET, end // _BUCKET + 1)
            for other_index in _span_buckets.get(bucket, ())
        }
        # 只跟"命中区间和自己重叠"的同类 term 比 —— 那才是同一条指令的两种切法。
        rivals = {
            hits[other_index][2]
            for other_index in neighbours
            if other_index != index
            and hits[other_index][1] == kind
            and _overlaps(spans[index], spans[other_index])
        }
        if not rivals:
            return False
        # 填充词会叠（"前女友的事就"），所以逐层剥，任何一层撞上对手就丢。
        seen_forms = {term}
        frontier = [term]
        while frontier:
            current = frontier.pop()
            for filler in _ZH_TRAILING_FILLERS:
                if not current.endswith(filler) or len(current) <= len(filler):
                    continue
                shorter = current[: -len(filler)]
                # ⚠️ 剥完要把两端的括号 / 标点也归一化再比：填充词前面常常正好是一个
                # 收尾括号。``关于《你好，李焕英》就别提了。`` 的通用切法是
                # ``你好，李焕英》就``，剥掉 ``就`` 得到 ``你好，李焕英》``——多一个
                # ``》`` 就跟专用切法的 ``你好，李焕英`` 对不上，畸形的那条照样存三天
                # （codex P2）。归一化用的就是 term 落库前走的同一套 _TRIM_TRAIL。
                # ⚠️ 剥完要走**同一套 _trim_term** 再比，不能只剥标点：对手那条
                # 是落库前 trim 过的，而中间形态还带着助词。``關於전남친은就別提了。``
                # 里剥掉 ``就`` 得到 ``전남친은``，跟已经 trim 成 ``전남친`` 的对手
                # 对不上，畸形的那条照样存三天（codex P2）。
                forms = {shorter, _trim_term(shorter, locale)}
                for form in forms:
                    if form in rivals:
                        return True
                    if form and form not in seen_forms:
                        seen_forms.add(form)
                        frontier.append(form)
        return False

    return [
        hit
        for index, hit in enumerate(hits)
        if index not in suspect_set or not _is_redundant(index)
    ]


# ---------------------------------------------------------------------------
# 下一轮会话注入用的 system prompt 片段
# ---------------------------------------------------------------------------
# 历史的"用户最近表示不想聊"列表会被拼成 ``- {term1}\n- {term2}\n``，再用
# 各 locale 的模板包一层 header / footer。两个槽位：
#   {items}     —— bullet list
#   {n}         —— 条数（少数语言语法需要单复数）
#
# 渲染层：UserDirectivesManager.render_prompt_block(lanlan_name, lang)。

USER_DIRECTIVES_PROMPT_BLOCK = {
    'zh': (
        "\n\n[用户最近明确表示过不想聊或不喜欢被提到以下内容（共{n}项）]\n"
        "{items}\n"
        "请在本次会话里主动避开这些话题或称呼，除非用户自己重新提起。"
    ),
    'zh-TW': (
        "\n\n[使用者最近明確表示過不想聊、或不喜歡被提到以下內容（共{n}項）]\n"
        "{items}\n"
        "請在這次的對話裡主動避開這些話題或稱呼，除非使用者自己重新提起。"
    ),
    'en': (
        "\n\n[The user recently asked not to discuss or be referred to as the "
        "following ({n} item(s))]\n"
        "{items}\n"
        "Please actively steer clear of these topics or labels in this session, "
        "unless the user brings them up again."
    ),
    'ja': (
        "\n\n[最近、ユーザーが話したくない・呼ばれたくないと明示した内容（{n}件）]\n"
        "{items}\n"
        "今回のセッションでは、ユーザー自身が再び話題にしない限り、"
        "これらの話題や呼び方を能動的に避けてください。"
    ),
    'ko': (
        "\n\n[사용자가 최근에 언급하지 말거나 그렇게 부르지 말라고 명확히 요청한 항목 ({n}개)]\n"
        "{items}\n"
        "이번 세션에서는 사용자가 직접 다시 꺼내지 않는 한, "
        "이러한 화제나 호칭을 적극적으로 피해 주세요."
    ),
    'ru': (
        "\n\n[Пользователь недавно явно просил не обсуждать или не называть "
        "следующее ({n} шт.)]\n"
        "{items}\n"
        "В этой сессии активно избегайте этих тем и обращений, "
        "если пользователь сам к ним не вернётся."
    ),
    'es': (
        "\n\n[El usuario pidió explícitamente no hablar de o no ser llamado/a "
        "con lo siguiente ({n} elemento(s))]\n"
        "{items}\n"
        "Evita activamente estos temas o etiquetas en esta sesión, "
        "salvo que el propio usuario los vuelva a sacar."
    ),
    'pt': (
        "\n\n[O usuário pediu explicitamente para não falar sobre ou ser "
        "chamado(a) pelo seguinte ({n} item(ns))]\n"
        "{items}\n"
        "Evite ativamente esses tópicos ou rótulos nesta sessão, "
        "a menos que o próprio usuário volte a mencioná-los."
    ),
}


def render_directives_block(terms: List[str], lang: str) -> str:
    """Render the active term list into a system-prompt block (with leading newlines).

    Empty list → returns "" (callers concat directly, no emptiness check needed).
    ``lang`` accepts full locales (``zh-CN`` etc.), normalized internally to a short code.
    """
    if not terms:
        return ""
    short = _norm_lang(lang)
    template = USER_DIRECTIVES_PROMPT_BLOCK.get(short) or USER_DIRECTIVES_PROMPT_BLOCK['en']
    items = "\n".join(f"- {t}" for t in terms)
    return template.format(items=items, n=len(terms))


# ---------------------------------------------------------------------------
# 防复读（anti-repeat）— 注入"最近高频 topic 词"提示
# ---------------------------------------------------------------------------
# 来源：``memory.anti_repeat.AntiRepeatCorpus.top_recent_topics``。注入位置同
# ``USER_DIRECTIVES_PROMPT_BLOCK`` —— ``_build_initial_prompt`` 末尾、ban list
# 之后。proactive 与 regular reply 共用：proactive 还会被 BM25 总分阈值
# 拦截（regen / drop），regular 只靠这段 prompt 软约束。
#
# 这段的语气和 ban list 不一样：ban list 是"用户明确说过别提"，必须强约束；
# 这里只是"你最近聊过这些，换些角度更好"，建议性的，不要太重，否则把 LLM
# 引导成话题切换疯子。

RECENT_TOPIC_HINT_PROMPT_BLOCK = {
    'zh': (
        "\n\n[最近几轮你已经聊过的话题（{n}项）]\n"
        "{items}\n"
        "如果还没必要，尽量换个角度或换个话题，避免连续围绕同一主题打转。"
    ),
    'zh-TW': (
        "\n\n[最近幾輪你已經聊過的話題（{n}項）]\n"
        "{items}\n"
        "如果還沒必要，盡量換個角度或換個話題，避免一直繞著同一個主題打轉。"
    ),
    'en': (
        "\n\n[Topics you've already touched on in the last few turns ({n})]\n"
        "{items}\n"
        "Unless still relevant, try a fresh angle or a new topic rather than "
        "circling back to the same one."
    ),
    'ja': (
        "\n\n[最近のターンで既に触れた話題（{n}件）]\n"
        "{items}\n"
        "まだ必要でなければ、同じ話題を繰り返さず、別の切り口や新しい話題に"
        "切り替えてみてください。"
    ),
    'ko': (
        "\n\n[최근 몇 턴 동안 이미 다룬 화제 ({n}개)]\n"
        "{items}\n"
        "꼭 필요하지 않다면 같은 주제를 맴돌지 말고 다른 각도나 새로운 화제로"
        "전환해 보세요."
    ),
    'ru': (
        "\n\n[Темы, которые вы уже затронули за последние ходы ({n} шт.)]\n"
        "{items}\n"
        "Если в этом нет необходимости, попробуйте новый ракурс или другую "
        "тему, не кружите вокруг одной и той же."
    ),
    'es': (
        "\n\n[Temas que ya tocaste en los últimos turnos ({n} elemento(s))]\n"
        "{items}\n"
        "Salvo que sea necesario, prueba un ángulo distinto o un tema nuevo "
        "en lugar de volver al mismo."
    ),
    'pt': (
        "\n\n[Tópicos que você já abordou nos últimos turnos ({n} item(ns))]\n"
        "{items}\n"
        "A menos que ainda seja relevante, tente um ângulo novo ou outro "
        "tópico em vez de voltar ao mesmo."
    ),
}


def render_recent_topics_block(terms: List[str], lang: str) -> str:
    """Render the "recent topic terms" list into a system-prompt fragment; empty list → ""."""
    if not terms:
        return ""
    short = _norm_lang(lang)
    template = RECENT_TOPIC_HINT_PROMPT_BLOCK.get(short) or RECENT_TOPIC_HINT_PROMPT_BLOCK['en']
    items = "\n".join(f"- {t}" for t in terms)
    return template.format(items=items, n=len(terms))


# ---------------------------------------------------------------------------
# Proactive regen 指令 — 给重 sample 用
# ---------------------------------------------------------------------------
# 当 BM25 总分超 REGEN_THRESHOLD 时，``main_routers/system_router`` 在第二次
# Phase 2 LLM 调用前注入这段，告诉 LLM 哪些 term 必须避开。
#
# ⚠️ 措辞刻意做成"结构化指令 + 显式反复述约束"：早期版本是一句散文式祈使
# （"换一个完全不同的角度或主题"），弱模型在超长上下文末尾收到后，容易把指令
# 原文/规划脚手架当成正文吐出来（线上见过 "完全不同的角度或主题"、"括号、Emoji"
# 这类泄漏）。现在每条都：(1) 用方括号小标题标明这是改写要求而非对话；(2) 末尾
# 明确"不要复述/解释本要求、不要输出标签化回复以外的任何东西"。注入侧还会把
# BEGIN 触发句放在最后（见 system_router），使指令本身不是模型看到的最后一句。
# 占位符：{terms} 要避开的词；{master_name} 搭话对象。

PROACTIVE_REGEN_AVOID_INSTRUCTION = {
    'zh': (
        "【改写要求】这些词和话题最近已经聊得太多，本次必须避开：{terms}。"
        "换个角度或换个话题，直接写一句全新的搭话。"
        "输出严格遵守上面的格式：第一行写来源标签，第二行起只写要对{master_name}说的原话；"
        "如果想不出新角度，就只输出 [PASS]。"
        "不要复述或解释本要求，不要输出任何思考过程、清单或标签化回复以外的内容。"
    ),
    'zh-TW': (
        "【改寫要求】這些詞和話題最近已經聊得太多，這次必須避開：{terms}。"
        "換個角度或換個話題，直接寫一句全新的搭話。"
        "輸出嚴格遵守上面的格式：第一行寫來源標籤，第二行起只寫要對{master_name}說的原話；"
        "如果想不出新角度，就只輸出 [PASS]。"
        "不要複述或解釋這項要求，不要輸出任何思考過程、清單或標籤化回覆以外的內容。"
    ),
    'en': (
        "[Rewrite] These words and topics have been used too much recently and MUST be "
        "avoided: {terms}. Pick a different angle or topic and write one brand-new line. "
        "Keep strictly to the format above: the first line is the source tag, then write "
        "only the actual words you'd say to {master_name}; if you have no fresh angle, output "
        "only [PASS]. Do NOT restate or explain this instruction, and do NOT output any "
        "reasoning, lists, or anything other than the tagged reply."
    ),
    'ja': (
        "【書き直し】次の語と話題は最近使いすぎているので必ず避けてください：{terms}。"
        "切り口か話題を変えて、新しい一言を書いてください。"
        "出力は上の形式を厳守：1行目に来源タグ、その後は{master_name}に実際に言う言葉だけ。"
        "新しい切り口が思いつかなければ [PASS] だけを出力。"
        "この指示を復唱・説明せず、思考過程やリスト、タグ付き発言以外のものを出力しないこと。"
    ),
    'ko': (
        "【다시 쓰기】다음 단어와 화제는 최근에 너무 많이 다뤘으니 반드시 피하세요: {terms}. "
        "관점이나 화제를 바꿔 완전히 새로운 한마디를 쓰세요. "
        "출력은 위 형식을 엄격히 따르세요: 첫 줄은 출처 태그, 이후에는 {master_name}에게 실제로 "
        "할 말만 쓰세요; 새 관점이 없으면 [PASS]만 출력하세요. 이 지시를 되풀이하거나 설명하지 "
        "말고, 사고 과정·목록·태그 외의 어떤 것도 출력하지 마세요."
    ),
    'ru': (
        "[Перепиши] Эти слова и темы в последнее время используются слишком часто, их "
        "обязательно нужно избегать: {terms}. Выбери другой угол или тему и напиши одну "
        "совершенно новую реплику. Строго соблюдай формат выше: первая строка — тег источника, "
        "далее — только сами слова, которые ты скажешь {master_name}; если нового угла нет, "
        "выведи только [PASS]. Не пересказывай и не объясняй эту инструкцию, не выводи "
        "рассуждения, списки или что-либо кроме реплики с тегом."
    ),
    'es': (
        "[Reescribe] Estas palabras y temas se han usado demasiado últimamente y DEBES "
        "evitarlos: {terms}. Elige otro ángulo o tema y escribe una frase totalmente nueva. "
        "Respeta estrictamente el formato de arriba: la primera línea es la etiqueta de "
        "fuente, luego escribe solo lo que le dirías a {master_name}; si no tienes un ángulo "
        "nuevo, responde solo [PASS]. No repitas ni expliques esta instrucción, y no muestres "
        "razonamientos, listas ni nada que no sea la respuesta con etiqueta."
    ),
    'pt': (
        "[Reescreva] Estas palavras e temas foram usados demais recentemente e você DEVE "
        "evitá-los: {terms}. Escolha outro ângulo ou tema e escreva uma fala totalmente nova. "
        "Siga estritamente o formato acima: a primeira linha é a etiqueta de fonte, depois "
        "escreva apenas o que você diria a {master_name}; se não tiver um ângulo novo, "
        "responda apenas [PASS]. Não repita nem explique esta instrução, e não exiba "
        "raciocínio, listas ou qualquer coisa além da resposta com etiqueta."
    ),
}


# render_regen_avoid_instruction 缺省称呼（master_name 未传时的中性占位）。
# 不用"主人/master"等物化称呼（见项目约定）。
_DEFAULT_ADDRESSEE = {
    "zh": "对方",
    "zh-TW": "對方",
    "en": "them",
    "ja": "相手",
    "ko": "상대",
    "ru": "собеседника",
    "es": "la otra persona",
    "pt": "a outra pessoa",
}


def render_regen_avoid_instruction(terms: List[str], lang: str, master_name: str = "") -> str:
    """Render the "avoid X / Y" instruction used for regen. Empty list → "".

    ``master_name`` writes "who this is said to" into the instruction; when missing,
    degrades to a neutral placeholder to avoid KeyError.
    """
    if not terms:
        return ""
    short = _norm_lang(lang)
    template = PROACTIVE_REGEN_AVOID_INSTRUCTION.get(short) or PROACTIVE_REGEN_AVOID_INSTRUCTION['en']
    # 每个词单独括起来，让模型清楚哪些是要避开的离散词（CJK 用「」，其余用双引号），
    # 再用各 locale 的列表分隔符拼接。
    lq, rq = ("「", "」") if short in ("zh", "ja") else ('"', '"')
    sep = "、" if short in ("zh", "ja") else ", "
    quoted_terms = sep.join(f"{lq}{t}{rq}" for t in terms)
    return template.format(
        terms=quoted_terms,
        master_name=master_name or _DEFAULT_ADDRESSEE.get(short, "them"),
    )


# ---------------------------------------------------------------------------
# Proactive 格式纠正指令 — 初稿没按格式输出时自救用
# ---------------------------------------------------------------------------
# 初稿没解析到合法来源标签时（弱化模型常把人设 Format/约束块当正文吐出来，
# 如 "No Markdown: Yes."），system_router 注入这段再生成一次，把模型拽回
# "第一行写来源标签、其后正文" 的格式；与 BEGIN 触发句一起放进 Human turn
# （末尾仍是中性触发句）。占位符：{master_name} 搭话对象。

PROACTIVE_FORMAT_FIX_INSTRUCTION = {
    'zh': (
        "【格式纠正】上一次的输出没有按规定格式，把格式要求当成正文吐了出来。"
        "请重写：第一行只写一个来源标签（按上面输出格式段列出的来源标签选，"
        "如 [CHAT]、[WEB]、[MUSIC]、[MEME]），第二行起只写要对{master_name}说的话本身；"
        "没什么新鲜的可说就只输出 [PASS]。"
        "不要复述或解释任何规则，不要输出清单或思考过程，标签和正文以外的内容一律不要输出。"
    ),
    'zh-TW': (
        "【格式修正】上一次的輸出沒有照規定的格式，把格式要求當成正文吐了出來。"
        "請重寫：第一行只寫一個來源標籤（照上面輸出格式那段列出的來源標籤挑，"
        "例如 [CHAT]、[WEB]、[MUSIC]、[MEME]），第二行起只寫要對{master_name}說的話本身；"
        "沒什麼新鮮的可以講就只輸出 [PASS]。"
        "不要複述或解釋任何規則，不要輸出清單或思考過程，標籤和正文以外的內容一律不要輸出。"
    ),
    'en': (
        "[Format fix] Your last output didn't follow the required format — it spat out the "
        "rules as if they were the message. Rewrite it: the first line is a single source tag "
        "(choose from the source tags listed in the output-format section above, e.g. [CHAT], "
        "[WEB], [MUSIC], [MEME]), then from the next line write only the actual words you'd say "
        "to {master_name}; if you have nothing fresh to say, output only [PASS]. Do NOT restate "
        "or explain any rule, do NOT output lists or reasoning, and output nothing other than "
        "the tag and the message."
    ),
    'ja': (
        "【書式修正】前回の出力は指定の書式に従わず、ルールをそのまま本文として出してしまいました。"
        "書き直してください：1行目に来源タグを1つだけ（上の出力形式に挙げられたタグから選ぶ。"
        "例：[CHAT]・[WEB]・[MUSIC]・[MEME]）、2行目以降は{master_name}に実際に言う言葉だけ。"
        "新しく言うことがなければ [PASS] だけを出力。"
        "ルールを復唱・説明せず、リストや思考過程を出さず、タグと本文以外は何も出力しないこと。"
    ),
    'ko': (
        "【형식 교정】지난 출력이 규정된 형식을 따르지 않고 규칙을 본문처럼 뱉어냈습니다. "
        "다시 쓰세요: 첫 줄에는 출처 태그 하나만(위 출력 형식에 나열된 태그 중 선택, 예: [CHAT]·"
        "[WEB]·[MUSIC]·[MEME]), 이후 줄부터는 {master_name}에게 실제로 할 말만. 새로 할 말이 "
        "없으면 [PASS]만 출력. 규칙을 되풀이하거나 설명하지 말고, 목록·사고 과정을 출력하지 "
        "말며, 태그와 본문 외에는 아무것도 출력하지 마세요."
    ),
    'ru': (
        "[Исправь формат] Прошлый вывод не соответствовал формату — ты выдал правила, как "
        "будто это сообщение. Перепиши: первая строка — один тег источника (выбери из тегов, "
        "перечисленных в разделе формата вывода выше, напр. [CHAT], [WEB], [MUSIC], [MEME]), "
        "далее со следующей строки — только сами слова, которые ты скажешь {master_name}; если "
        "нового сказать нечего, выведи только [PASS]. Не пересказывай и не объясняй правила, не "
        "выводи списки или рассуждения и не выводи ничего, кроме тега и сообщения."
    ),
    'es': (
        "[Corrige el formato] Tu última salida no siguió el formato requerido: soltó las reglas "
        "como si fueran el mensaje. Reescríbela: la primera línea es una sola etiqueta de fuente "
        "(elige entre las etiquetas listadas en la sección de formato de salida de arriba, p. ej. "
        "[CHAT], [WEB], [MUSIC], [MEME]), luego desde la línea siguiente escribe solo lo que le "
        "dirías a {master_name}; si no tienes nada nuevo que decir, responde solo [PASS]. No "
        "repitas ni expliques ninguna regla, no muestres listas ni razonamientos, y no muestres "
        "nada más que la etiqueta y el mensaje."
    ),
    'pt': (
        "[Corrija o formato] Sua última saída não seguiu o formato exigido — cuspiu as regras "
        "como se fossem a mensagem. Reescreva: a primeira linha é uma única etiqueta de fonte "
        "(escolha entre as etiquetas listadas na seção de formato de saída acima, p. ex. [CHAT], "
        "[WEB], [MUSIC], [MEME]), depois, a partir da linha seguinte, escreva apenas o que você "
        "diria a {master_name}; se não tiver nada novo a dizer, responda apenas [PASS]. Não "
        "repita nem explique nenhuma regra, não exiba listas ou raciocínio, e não exiba nada "
        "além da etiqueta e da mensagem."
    ),
}


def render_format_fix_instruction(lang: str, master_name: str = "") -> str:
    """Render the "format fix" self-rescue instruction. ``master_name`` defaults to a neutral placeholder."""
    short = _norm_lang(lang)
    template = PROACTIVE_FORMAT_FIX_INSTRUCTION.get(short) or PROACTIVE_FORMAT_FIX_INSTRUCTION['en']
    return template.format(master_name=master_name or _DEFAULT_ADDRESSEE.get(short, "them"))


# =====================================================================
# ======= Negative-keyword target check (RFC §3.4.5 Layer 2) ==========
# =====================================================================
# 职责：用户说"别提了 / 换个话题"这类话命中本地关键词后，派一次小 LLM 调
# 用决定"用户到底是在说哪条？还是只是泛化情绪？"。水印："======以上为".
#
# 历史位置：从 ``prompts_memory.py`` 迁过来——negative-intent prompt + 关键词
# 与本模块的 ban-topic regex/抽取 是同一类输入（"用户的负面 / 回避指令"），
# 集中在一处便于以后维护词表 / prompt 一致性。
# evidence 系统的接入点保持原样（``app/memory_server._amaybe_trigger_negative_keyword_hook``）。

NEGATIVE_TARGET_CHECK_PROMPT = {
    "zh": """你是一个用户回避意图判定专家。

======以下为用户最近消息======
{USER_MESSAGES}
======以上为用户最近消息======

======以下为系统正在维护的观察列表======
{OBSERVATIONS}
======以上为观察列表======

用户消息里，"别提了 / 不想聊 / 换个话题 / 别再说"这类表达到底指上述哪一条？可能多条、也可能一条都没有（用户只是泛化情绪）。

只能从"观察列表"里选 target_id，不要凭空生成。
target_type 必须是字符串 "reflection" 或 "persona" 之一。

返回合法 JSON（如果用户只是泛化情绪，无明确 target，返回 {"targets": []}）：
{"targets": [{"target_type": "reflection",
              "target_id": "...",
              "reason": "简短理由"}]}""",
    # 分隔符水印在每一条 locale 里都是同一串简体字面量（与其余 prompt 表同理），
    # 繁中跟着走、不做转换——它是给模型认边界用的锚点，不是给用户看的文案。
    "zh-TW": """你是一個使用者迴避意圖判定專家。

======以下为用户最近消息======
{USER_MESSAGES}
======以上为用户最近消息======

======以下为系统正在维护的观察列表======
{OBSERVATIONS}
======以上为观察列表======

使用者訊息裡，「別提了 / 不想聊 / 換個話題 / 別再說」這類表達到底指上述哪一條？可能多條、也可能一條都沒有（使用者只是泛化情緒）。

只能從「觀察列表」裡選 target_id，不要憑空產生。
target_type 必須是字串 "reflection" 或 "persona" 其中之一。

回傳合法 JSON（如果使用者只是泛化情緒，無明確 target，回傳 {"targets": []}）：
{"targets": [{"target_type": "reflection",
              "target_id": "...",
              "reason": "簡短理由"}]}""",
    "en": """You are a user pushback target analyst.

======以下为用户最近消息======
{USER_MESSAGES}
======以上为用户最近消息======

======以下为系统正在维护的观察列表======
{OBSERVATIONS}
======以上为观察列表======

In the user's messages, when they say things like "don't mention / change the topic / stop talking about", which observation(s) above are they referring to? Could be several, or none at all (just a vague mood).

target_id MUST come from "observations" above — do not invent IDs.
target_type MUST be the literal string "reflection" or "persona".

Return valid JSON. If the user is just venting without a specific target, return an object with an empty `targets` array: {"targets": []}. Otherwise:
{"targets": [{"target_type": "reflection",
              "target_id": "...",
              "reason": "short rationale"}]}""",
    "ja": """あなたはユーザーの拒否反応が何を指しているかを判定する専門家です。

======以下为用户最近消息======
{USER_MESSAGES}
======以上为用户最近消息======

======以下为系统正在维护的观察列表======
{OBSERVATIONS}
======以上为观察列表======

ユーザーが「その話はいい／話題を変えて／やめて」などと言ったのは、上の観察のうちどれを指していますか？複数の場合もあれば、一つも該当しない場合もあります（単なるムード）。

target_id は必ず上の "観察" から選ぶこと。
target_type は文字列 "reflection" または "persona" のいずれかでなければならない。

有効な JSON で返す。該当なしの場合は targets を空配列に: {"targets": []}。
それ以外:
{"targets": [{"target_type": "reflection",
              "target_id": "...",
              "reason": "短い理由"}]}""",
    "ko": """당신은 사용자의 거부 표현이 무엇을 가리키는지 판정하는 전문가입니다.

======以下为用户最近消息======
{USER_MESSAGES}
======以上为用户最近消息======

======以下为系统正在维护的观察列表======
{OBSERVATIONS}
======以上为观察列表======

사용자가 "그 얘기는 그만 / 다른 이야기하자" 같은 표현을 쓸 때, 위 관찰 중 어떤 것을 가리킵니까? 여러 개일 수도, 전혀 없을 수도 있습니다.

target_id는 반드시 위 "관찰"에서 가져오세요.
target_type은 문자열 "reflection" 또는 "persona" 중 하나여야 합니다.

유효한 JSON으로 반환하세요. 해당 없음이면 targets를 빈 배열로: {"targets": []}.
그 외:
{"targets": [{"target_type": "reflection",
              "target_id": "...",
              "reason": "짧은 이유"}]}""",
    "ru": """Вы эксперт по определению цели пользовательского отказа от темы.

======以下为用户最近消息======
{USER_MESSAGES}
======以上为用户最近消息======

======以下为系统正在维护的观察列表======
{OBSERVATIONS}
======以上为观察列表======

Когда пользователь говорит "хватит об этом / сменим тему / не надо об этом", к каким из перечисленных наблюдений это относится? Может быть несколько или ни одного (просто эмоция).

target_id ДОЛЖЕН быть из "наблюдений" выше.
target_type ДОЛЖЕН быть строкой "reflection" или "persona".

Верните валидный JSON. Если конкретной цели нет — объект с пустым массивом `targets`: {"targets": []}. В противном случае:
{"targets": [{"target_type": "reflection",
              "target_id": "...",
              "reason": "короткое обоснование"}]}""",
    "es": """Eres especialista en determinar el objetivo de una reacción de rechazo del usuario.

======以下为用户最近消息======
{USER_MESSAGES}
======以上为用户最近消息======

======以下为系统正在维护的观察列表======
{OBSERVATIONS}
======以上为观察列表======

Cuando el usuario dice cosas como "no lo menciones / cambia de tema / deja de hablar de eso", ¿a cuál(es) de las observaciones de arriba se refiere? Puede ser varias o ninguna (solo un estado de ánimo general).

target_id DEBE venir de la "lista de observaciones" de arriba; no inventes IDs.
target_type DEBE ser literalmente "reflection" o "persona".

Devuelve JSON válido. Si no hay objetivo específico, devuelve {"targets": []}. Si lo hay:
{"targets": [{"target_type": "reflection",
              "target_id": "...",
              "reason": "razón breve"}]}""",
    "pt": """Você é especialista em determinar o alvo de uma reação de recusa do usuário.

======以下为用户最近消息======
{USER_MESSAGES}
======以上为用户最近消息======

======以下为系统正在维护的观察列表======
{OBSERVATIONS}
======以上为观察列表======

Quando o usuário diz coisas como "não mencione / muda de assunto / pare de falar disso", a qual(is) observação(ões) acima ele se refere? Pode ser várias ou nenhuma (apenas um humor geral).

target_id DEVE vir da "lista de observações" acima; não invente IDs.
target_type DEVE ser literalmente "reflection" ou "persona".

Retorne JSON válido. Se não houver alvo específico, retorne {"targets": []}. Caso contrário:
{"targets": [{"target_type": "reflection",
              "target_id": "...",
              "reason": "motivo breve"}]}""",
}


def get_negative_target_check_prompt(lang: str = "zh") -> str:
    return _loc(NEGATIVE_TARGET_CHECK_PROMPT, lang)


# =====================================================================
# ======= Negative-keyword scanning (RFC §3.4.5 Layer 1) ==============
# =====================================================================
# 本地确定性 frozenset 扫描；命中后异步派发 Layer 2 LLM 判定。
# 目标语义：用户希望 AI 闭嘴 / 回避特定话题（包含"嫌烦"族，因为这类词用在
# 话题语境时基本都意味着"想结束这个话题"）。**不收纯情绪词**（焦虑/崩溃/
# 难受/失望/痛苦…）——它们经常单独出现而无回避意图，会触发无用 LLM 调用。
# 单字也避免（"烦"会被"麻烦你"/"麻烦了"误命中），双字以上更稳。
#
# zh 与 zh-TW 是**两块独立词表**，不是同一份的两种写法：这里拿词条去撞用户实际
# 打出来的字，繁简是不同码位，简体词条对繁中输入是 0 命中。两块逐条对应（含繁
# 简同形的那几条，照抄以便一侧改动时对照）。scan_negative_keywords 对整个 zh 系
# 扫两块的并集——见该函数的 docstring。
NEGATIVE_KEYWORDS_I18N: dict[str, frozenset[str]] = {
    "zh": frozenset(
        [
            # 显式回避型
            "别说了",
            "别再说",
            "不要再说",
            "不要说",
            "别提了",
            "别提",
            "别再提",
            "不要再提",
            "不想提",
            "不想再提",
            "不想说",
            "不想说了",
            "不想再说",
            "别讲",
            "别再讲",
            "不要讲",
            "不要再讲",
            "别聊",
            "别聊这个",
            "不要聊",
            "不想聊",
            "换个话题",
            "换话题",
            "聊点别的",
            "说点别的",
            "这个不用说了",
            "闭嘴",
            "别问了",
            "不要问了",
            # 嫌烦型（暗含"想结束此话题"）
            "烦死",
            "烦人",
            "好烦",
            "真烦",
            "烦透",
            "心烦",
            "讨厌",
            "真讨厌",
            "受不了",
            "无语",
            "真无语",
        ]
    ),
    "zh-TW": frozenset(
        [
            # 顯式迴避型
            "別說了",
            "別再說",
            "不要再說",
            "不要說",
            "別提了",
            "別提",
            "別再提",
            "不要再提",
            "不想提",
            "不想再提",
            "不想說",
            "不想說了",
            "不想再說",
            "別講",
            "別再講",
            "不要講",
            "不要再講",
            "別聊",
            "別聊這個",
            "不要聊",
            "不想聊",
            "換個話題",
            "換話題",
            "聊點別的",
            "說點別的",
            "這個不用說了",
            "閉嘴",
            "別問了",
            "不要問了",
            # 嫌煩型（暗含「想結束此話題」）
            "煩死",
            "煩人",
            "好煩",
            "真煩",
            "煩透",
            "心煩",
            "討厭",
            "真討厭",
            "受不了",
            "無語",
            "真無語",
        ]
    ),
    "en": frozenset(
        [
            # Explicit avoidance
            "stop talking about",
            "don't mention",
            "do not mention",
            "change the topic",
            "change the subject",
            "let's not discuss",
            "let's not talk about",
            "drop the subject",
            "drop it",
            "not this again",
            "shut up",
            "let it go",
            "move on",
            "enough of this",
            # Annoyance (implies "end this topic")
            # `hate` must stay multi-word — bare "hate" is a substring of common
            # words like "whatever" and would fire false positives every turn.
            "i hate",
            "hate this",
            "hate that",
            "hate it",
            "hate when",
            "annoying",
            "annoyed",
            "frustrating",
            "frustrated",
            "sick of",
        ]
    ),
    "ja": frozenset(
        [
            # 明示的な回避
            "その話は",
            "その話はもう",
            "その話やめ",
            "やめて",
            "話題を変えて",
            "別の話",
            "他の話",
            "言わないで",
            "黙って",
            # うんざり系（話題を終わらせたい含意）
            "もう嫌",
            "イライラ",
            "うざい",
            "しつこい",
        ]
    ),
    "ko": frozenset(
        [
            # 명시적 회피
            "그만하자",
            "그 얘기는 그만",
            "다른 이야기",
            "다른 얘기",
            "다른 얘기 하자",
            "말하지 마",
            "닥쳐",
            # 짜증 계열 (화제 종료 함의)
            "짜증",
            "싫어",
            "지긋지긋",
        ]
    ),
    "ru": frozenset(
        [
            # Явное избегание
            "хватит об этом",
            "сменим тему",
            "не говори об этом",
            "другая тема",
            "не надо об этом",
            "замолчи",
            "отстань",
            "хватит",
            # Раздражение (подразумевает «закроем тему»)
            "раздражает",
            "надоело",
            "достало",
        ]
    ),
    "es": frozenset(
        [
            "no hables",
            "no quiero hablar",
            "no quiero hablar de eso",
            "cambia de tema",
            "hablemos de otra cosa",
            "déjalo",
            "basta",
            "no lo menciones",
            "no sigas",
        ]
    ),
    "pt": frozenset(
        [
            "não fale",
            "não quero falar",
            "não quero falar disso",
            "mude de assunto",
            "vamos falar de outra coisa",
            "deixa pra lá",
            "chega",
            "não mencione isso",
            "não continue",
        ]
    ),
}


# 扫描侧的中文并集：预算一次存成常量。scan_negative_keywords 是每条用户消息都
# 跑的热路径（post_turn 每轮 × user_msgs 条数），写成函数里现 union 会每条消息
# 重建一个 80 元素 frozenset。
_ZH_SCAN_KEYWORDS: frozenset[str] = (
    NEGATIVE_KEYWORDS_I18N["zh"] | NEGATIVE_KEYWORDS_I18N["zh-TW"]
)


def scan_negative_keywords(message: str, lang: str = "zh") -> bool:
    """Fast path: case-insensitive substring scan against NEGATIVE_KEYWORDS_I18N.

    Returns True if the message contains any negation keyword for the given
    language; if lang is unknown, falls back to the Chinese union.

    ⚠️ Does NOT go through ``_norm_lang`` — that helper serves i18n template
    rendering, where unknown languages map to ``en`` (English is the lingua franca;
    defaulting template rendering to English is reasonable). This function's
    contract is "treat unrecognizable language as a Chinese user" (codex P2 /
    scan-only policy), a different policy from the render path. So only minimal
    normalization happens here: strip the region suffix (``en-US`` → ``en`` /
    ``zh-CN`` → ``zh``) and leave unrecognized short codes to the Chinese fallback.

    ⚠️ The whole Chinese family scans Simplified plus Traditional, not one script.
    Stripping the region suffix is what makes that necessary: by the time ``short``
    is computed below, ``zh-TW`` is indistinguishable from ``zh-CN``, so there is no
    ``zh-TW`` key left to look up and a per-locale lookup would leave that table as
    unreachable data. Scanning both is also right on its own terms — users mix
    scripts (pasting Simplified content into a Traditional UI, typing Traditional
    with a Simplified IME) — and this module prefers false positives: a miss means
    the model keeps stepping on the same landmine, while a false hit costs one
    cheap-tier background LLM call that comes back with no target.
    """
    if not message:
        return False
    # 只剥 region 后缀（zh-CN/zh_CN/en-US/pt-BR ...），保留契约："未知 → zh"。
    # 同时 strip 前后空白 + lower 大小写——上游若传 ``EN-US`` 或 ``" en-US "``，
    # split 后是 ``EN`` / `` en``，dict key 都是小写无空白会 miss → 错落 zh
    # 兜底（CodeRabbit Minor）。
    short = (lang or "").strip().lower().split('-', 1)[0].split('_', 1)[0]
    kws = NEGATIVE_KEYWORDS_I18N.get(short)
    # 判据必须是**归一化之后**的 short == "zh"：上一行已经把 region 剥掉了，
    # 任何在这里去看原始 lang 有没有 "tw" 的写法都是恒假分支。zh / zh-CN /
    # zh-TW / zh-Hant 以及全部未知语言都走并集，等于把"未知 → 当中文用户"
    # 这条既有契约原样扩到两套字形上。
    if kws is None or short == "zh":
        kws = _ZH_SCAN_KEYWORDS
    lower = message.lower()
    for kw in kws:
        if kw.lower() in lower:
            return True
    return False


# `from ... import *` 不经过 __getattr__。没有 __all__ 时 Python 直接枚举模块全局量，
# 于是 DIRECTIVE_PATTERNS 改成惰性之后会从通配导入里**静默消失**，下游再用就是
# NameError（codex）。声明 __all__ 把它显式列回去：有 __all__ 时 import * 逐名
# getattr，惰性访问器照常触发。
#
# 其余名字按"此刻实际存在的公开全局量"原样算出来，而不是手写一张清单——这个模块
# 没有 __all__ 时的历史行为就是"所有不以下划线开头的模块级名字"，手写会顺手收窄
# 通配面，而收窄了谁也不会发现。必须留在文件末尾，globals() 才是全的。
__all__ = sorted(
    {name for name in globals() if not name.startswith("_")} | {"DIRECTIVE_PATTERNS"}
)
