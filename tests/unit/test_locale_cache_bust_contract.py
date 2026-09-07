"""Cache-bust contract for static/i18n-i18next.js.

New locale keys only reach an already-running client if the locale request URL
changes, because the bootstrap fetches static/locales/<lng>.json through a
long-lived cache (Electron and the packaged Docker image both hold on to it).
The single lever is LOCALE_VERSION, which both load sites append as ?v=. Ship
new keys without touching it and the cached client keeps serving the previous
file, so i18next finds no entry and renders the raw token - "errors.SOMETHING"
straight into the UI, which is exactly the class of bug the new keys were added
to fix.

What this module can and cannot enforce, stated plainly so nobody mistakes it
for more than it is. A static test reads the working tree, and the working tree
is entirely under the author's control, so no assertion here can force a human
to bump a version they have decided not to bump. What it can do is remove every
*silent* path to the bug:

  * change the locale key set and LOCALE_KEY_SIGNATURE goes red, which drags the
    author into this file and in front of the instruction to bump;
  * follow only half of that instruction - retire the current value without
    picking a new one - and RETIRED_LOCALE_VERSIONS goes red;
  * reuse or revert to any value that has already shipped and the same set goes
    red;
  * drop the ?v= from a load site, or add a third load site without it, and the
    load-site assertion goes red.

The hole left is an author who reads the failure message, refreshes the
signature, and deliberately leaves the version alone. That is a decision rather
than an oversight, and it is visible in the diff.

A git-based ratchet ("locales changed versus merge-base, therefore
LOCALE_VERSION must differ from merge-base") would close that hole, and it was
the first thing tried here. It cannot run: .github/workflows/unit-tests.yml
checks out with actions/checkout@v5 at its default depth of 1 and never fetches
origin/main, so the ratchet would have no base to diff against and would
degrade to a skip - a guard that always passes in the one environment that
matters.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "static" / "i18n-i18next.js"
LOCALES_DIR = ROOT / "static" / "locales"

# 版本串的形状：日期前缀 + 说明这次递增为什么发生的 slug。历史上出现过
# `2026-01-31-1` 这种纯序号后缀，也出现过带下划线的 `guide1_7-upstream-main-merge`，
# 所以下划线和数字都放行，只把日期前缀钉死 —— 日期前缀是别处（例如
# static/yui-guide-day1-systray-intro.test.cjs）用来做“不早于某次递增”比较的依据。
LOCALE_VERSION_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9._-]*$")

# 这里穷举的是 git 历史里所有出现过的 LOCALE_VERSION 取值（含只活在分支上的，
# 比如 `2026-08-14-language-preference-superseded`）。作用有两个：挡住“递增”被写成
# 一个早就发过的字符串，以及挡住 rebase/revert 把版本串带回旧值 —— 这两种情况下
# 客户端的缓存键都不会变，新 key 依旧吃不到。
#
# 递增 LOCALE_VERSION 时，把旧值追加到这里。
RETIRED_LOCALE_VERSIONS = frozenset(
    {
        "2026-01-31-1",
        "2026-02-04-1",
        "2026-02-23-1",
        "2026-03-16-1",
        "2026-03-18-1",
        "2026-04-09-1",
        "2026-04-20-1",
        "2026-04-22-1",
        "2026-04-28-1",
        "2026-05-03-1",
        "2026-06-06-1",
        "2026-06-09-basketball-2",
        "2026-06-12-1",
        "2026-06-13-pr1719-merge",
        "2026-06-14-guide1_7-upstream-main-merge",
        "2026-06-16-pr1852-gsv-tutorial",
        "2026-06-17-guide-split-main-merge",
        "2026-06-18-drawing-guess-polish",
        "2026-06-19-drawing-guess-demo",
        "2026-06-21-drawing-guess-model-loading-preview",
        "2026-06-22-drawing-guess-tutorial-i18n",
        "2026-06-22-tutorial-i18n-bubblemeta",
        "2026-06-22-vllm-omni-clone",
        "2026-06-23-drawing-guess-memory-two-options-i18n",
        "2026-06-23-drawing-guess-voice-takeover-only-i18n",
        "2026-06-27-drawing-guess-vllm-omni-i18n",
        "2026-06-28-drawing-guess-ai-feedback-i18n",
        "2026-06-29-drawing-guess-save-art-copy-i18n",
        "2026-06-29-drawing-guess-save-art-copy-i18n-topic-hint",
        "2026-06-29-drawing-guess-save-art-copy-i18n-topic-hint-voice-profile-i18n",
        "2026-06-29-drawing-guess-save-art-copy-i18n-topic-hint-voice-profile-i18n-review-fixes",
        "2026-06-29-drawing-guess-save-art-i18n",
        "2026-06-29-drawing-guess-tutorial-voice-i18n",
        "2026-06-29-topic-hint",
        "2026-06-29-topic-hint-voice-profile-i18n",
        "2026-06-29-voice-profile-i18n",
        "2026-07-06-day1-systray-intro-i18n",
        "2026-07-08-doubao-speaker-id-model-type-3d-label-i18n",
        "2026-07-08-model-type-3d-label-i18n",
        "2026-07-11-drawing-guess-merge-doubao-speaker-id-model-type-3d-label-i18n",
        "2026-07-13-screenshot-pin-remote-i18n",
        "2026-07-13-social-status-messages",
        "2026-07-15-social-status-screenshot-pin-remote-i18n",
        "2026-07-16-external-memory-import-i18n",
        "2026-07-17-external-import-daily-i18n",
        "2026-07-17-external-import-eta-i18n",
        "2026-07-19-social-oauth-prompt",
        "2026-07-20-external-import-social-oauth-prompt",
        "2026-07-22-window-pin-controls-i18n",
        "2026-07-24-credential-management-i18n",
        "2026-07-24-memory-browser-ui-refactor",
        "2026-07-24-social-controls-i18n",
        "2026-07-27-merged-main-i18n",
        "2026-07-27-youtube-instruction-format-i18n",
        "2026-08-04-social-unlock",
        "2026-08-05-voice-identity-a11y",
        "2026-08-07-credentials-console-guide",
        "2026-08-07-voice-identity-page-title",
        "2026-08-10-language-preferences-v6",
        "2026-08-14-language-preference-freshness",
        "2026-08-14-language-preference-superseded",
        "2026-08-16-voice-identity-one-click",
        "2026-08-29-turn-image-budget-notices",
        "2026-08-29-repetition-insights",
        "2026-08-31-openfang-removal",
        "2026-09-01-day1-avatar-zoom-hint",
        "2026-09-01-day2-tool-wheel-rotation",
        "2026-09-01-add-guide-main-merge",
        "2026-09-01-vllm-omni-clone-preflight",
        "2026-09-02-vllm-omni-clone-preflight",
    }
)

# 当前 8 个语言包的 key 结构指纹（只覆盖 key 路径，不覆盖译文本身）。
#
# 为什么只按 key 而不按整文件内容：缓存陈旧时，改字面量的后果是用户多看几天旧措辞，
# 而新增/改名 key 的后果是把 key 本身当文案渲染出来。只对后者敏感，等于把这条守卫的
# 红灯精准打在真正需要递增版本的那类改动上，同时让纯润色文案的 PR 不必来动它 ——
# 一条每个 PR 都要顺手改一下的守卫，很快就会被当成噪音机械地改绿。
#
# 数组也按下标展开，所以往 badminton.lines.* 这类台词数组里追加一条同样会打红：
# 陈旧缓存下那一条会取到 undefined，症状和缺 key 是一类。
LOCALE_KEY_SIGNATURE = "d9200cddce7c21d4fd1b6db19b4d5a56b9bc6116c1074a7c42d753e0e4f10fa2"

_BUMP_INSTRUCTIONS = (
    "static/locales 的 key 结构变了。请在 static/i18n-i18next.js 里把 LOCALE_VERSION "
    "递增成一个新的 YYYY-MM-DD-slug（旧值追加进本文件的 RETIRED_LOCALE_VERSIONS），"
    "再把这里的 LOCALE_KEY_SIGNATURE 更新成新指纹。只改指纹不递增版本的话，缓存住旧"
    "语言包的客户端会继续把新 key 当字面量渲染出来。"
)


def _read_bootstrap() -> str:
    return BOOTSTRAP.read_text(encoding="utf-8")


def _declared_locale_version(bootstrap: str) -> str:
    match = re.search(r"const\s+LOCALE_VERSION\s*=\s*'([^']*)'", bootstrap)
    assert match, "static/i18n-i18next.js must declare a LOCALE_VERSION constant"
    return match.group(1)


def _supported_languages(bootstrap: str) -> tuple[str, ...]:
    match = re.search(r"const\s+SUPPORTED_LANGUAGES\s*=\s*\[([^\]]*)\]", bootstrap)
    assert match, "static/i18n-i18next.js must declare SUPPORTED_LANGUAGES"
    languages = tuple(re.findall(r"'([^']+)'", match.group(1)))
    assert languages, "SUPPORTED_LANGUAGES must not be empty"
    return languages


def _key_paths(node: object, prefix: str = "") -> list[str]:
    # 叶子按点分路径展开，数组按 [i] 下标展开，得到一个与译文无关的纯结构视图。
    if isinstance(node, dict):
        collected: list[str] = []
        for key, value in node.items():
            collected.extend(_key_paths(value, f"{prefix}{key}."))
        return collected
    if isinstance(node, list):
        indexed: list[str] = []
        for index, value in enumerate(node):
            indexed.extend(_key_paths(value, f"{prefix}[{index}]."))
        return indexed
    return [prefix.rstrip(".")]


def _locale_key_signature(languages: tuple[str, ...]) -> str:
    # 指纹覆盖的是 SUPPORTED_LANGUAGES 声明的那一组语言，而不是目录里躺着的所有 json：
    # 引导脚本只会去拉这一组，所以新增一门语言同样必须递增版本才能被老客户端看见。
    digest = hashlib.sha256()
    for language in sorted(languages):
        path = LOCALES_DIR / f"{language}.json"
        assert path.exists(), (
            f"SUPPORTED_LANGUAGES lists {language} but {path} is missing"
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        digest.update(language.encode("utf-8"))
        digest.update(b"\0")
        digest.update("\n".join(sorted(_key_paths(payload))).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def test_locale_version_is_shaped_like_a_dated_slug() -> None:
    version = _declared_locale_version(_read_bootstrap())
    assert version.strip(), "LOCALE_VERSION must not be empty"
    assert LOCALE_VERSION_PATTERN.match(version), (
        f"LOCALE_VERSION {version!r} must look like YYYY-MM-DD-slug"
    )


def test_locale_version_is_not_a_value_that_already_shipped() -> None:
    version = _declared_locale_version(_read_bootstrap())
    assert version not in RETIRED_LOCALE_VERSIONS, (
        f"LOCALE_VERSION {version!r} has already shipped. Reusing or reverting to an "
        "old value leaves the cache key unchanged, so clients keep the stale locale "
        "file and render new keys as raw tokens. Pick a fresh YYYY-MM-DD-slug."
    )


def test_every_locale_load_site_carries_the_cache_bust_query() -> None:
    # 两个装载点（手动 fetch 与 i18next backend 的 loadPath）必须都带 ?v=。断言写成
    # “扫出所有 /static/locales/ 引用再逐个查”，而不是分别断言两条已知字符串：后者对
    # “又加了第三个装载点、忘了带 ?v=”是瞎的，而那正是最容易发生的漏法。
    bootstrap = _read_bootstrap()
    references = re.findall(r"/static/locales/[^\s'\"`]*", bootstrap)
    assert len(references) >= 2, (
        f"expected at least the two known locale load sites, found {references!r}"
    )
    for reference in references:
        assert "?v=${encodeURIComponent(LOCALE_VERSION)}" in reference, (
            f"locale load site {reference!r} does not carry the LOCALE_VERSION "
            "cache-bust query, so clients would serve it from cache forever"
        )


def test_locale_key_signature_pins_the_shipped_key_set() -> None:
    bootstrap = _read_bootstrap()
    signature = _locale_key_signature(_supported_languages(bootstrap))
    assert signature == LOCALE_KEY_SIGNATURE, _BUMP_INSTRUCTIONS


def test_bump_instructions_name_every_step_of_the_fix() -> None:
    # 上一条用例的红灯文案是这条守卫唯一的“教学面”—— 它是把作者从指纹失配引到递增
    # 版本的那根线。文案里漏掉任何一步，守卫就退化成“改个指纹就绿”。
    #
    # 同时把“文案确实接在那条断言上”一并钉住：只查字符串内容、不查调用点的话，
    # 别人把指纹用例的 message 换成一句就地写的短提示，这条依旧是绿的。
    signature_test_source = inspect.getsource(
        test_locale_key_signature_pins_the_shipped_key_set
    )
    assert "_BUMP_INSTRUCTIONS" in signature_test_source, (
        "the signature assertion must fail with _BUMP_INSTRUCTIONS as its message"
    )
    assert "LOCALE_VERSION" in _BUMP_INSTRUCTIONS
    assert "RETIRED_LOCALE_VERSIONS" in _BUMP_INSTRUCTIONS
    assert "LOCALE_KEY_SIGNATURE" in _BUMP_INSTRUCTIONS
