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

"""Acquire and normalize proactive-chat source payloads."""

import asyncio
from typing import Any

from config import PROACTIVE_PHASE1_FETCH_PER_SOURCE
from utils.logger_config import get_module_logger
from utils.screenshot_utils import (
    COMPRESS_JPEG_QUALITY,
    COMPRESS_TARGET_HEIGHT,
    decode_and_compress_screenshot_b64,
)
from utils.web_scraper import (
    NEKO_COMMUNITY_FEED_PAGE_SIZE,
    fetch_neko_community_feed,
    fetch_news_content,
    fetch_personal_dynamics,
    fetch_trending_content,
    fetch_video_content,
    fetch_window_context_content,
    format_neko_community_feed,
    format_news_content,
    format_personal_dynamics,
    format_trending_content,
    format_video_content,
    format_window_context_content,
)

from .content_logging import (
    _log_neko_community_content,
    _log_news_content,
    _log_personal_dynamics,
    _log_trending_content,
    _log_video_content,
)
from .contracts import ProactiveChatCommand


logger = get_module_logger(__name__, "Main")
# Link extraction used to live in generation.py. Keep its rare failure log on
# the established namespace so existing log filters do not change in a move-only
# refactor; source fetch failures use the injected service logger below.
_link_extraction_logger = get_module_logger(
    "main_logic.proactive_chat.generation",
    "Main",
)


def _interleave_link_groups(candidate_groups: list[list[dict]]) -> list[dict]:
    """Interleave non-empty link groups row by row until all are exhausted."""
    groups = [group for group in candidate_groups if group]
    links: list[dict] = []
    row = 0
    while any(row < len(group) for group in groups):
        for group in groups:
            if row < len(group):
                links.append(group[row])
        row += 1
    return links


def _link_from_item(
    item: dict,
    *,
    title: str,
    url: str,
    source: str,
) -> dict:
    # Candidate metadata stays internal until decisions.py emits a public link.
    # Only adapters that explicitly mark their platform opt into carrying
    # private metadata. Legacy sources such as YouTube must retain the old
    # title/url/source-only contract instead of leaking feed-only fields into
    # link extraction results.
    link = dict(item) if item.get("platform") else {}
    link.update({"title": title, "url": url, "source": source})
    return link


def _extract_links_from_raw(
    mode: str,
    raw_data: dict,
    *,
    log: Any = None,
) -> list[dict]:
    """Extract normalized link entries from a raw source payload."""
    links = []
    try:
        if mode == "news":
            news = raw_data.get("news", {})
            weibo_or_twitter: list[dict] = []
            for item in news.get("trending", []) or []:
                title = item.get("word", "") or item.get("name", "")
                url = item.get("url", "")
                if title and url:
                    weibo_or_twitter.append(
                        {
                            "title": title,
                            "url": url,
                            "source": "微博"
                            if raw_data.get("region", "china") == "china"
                            else "Twitter",
                        }
                    )
            xhh_links: list[dict] = []
            for post in raw_data.get("xhh", {}).get("posts", []) or []:
                title = post.get("title", "")
                url = post.get("url", "")
                if title and url:
                    xhh_links.append(
                        {"title": title, "url": url, "source": "小黑盒"}
                    )

            tieba_links: list[dict] = []
            tieba = raw_data.get("tieba", {}) or {}
            posts = tieba.get("posts", []) or (tieba.get("tieba", {}) or {}).get(
                "posts", []
            )
            topics = tieba.get("topics", []) or (tieba.get("tieba", {}) or {}).get(
                "topics", []
            )
            for item in list(posts or []) + list(topics or []):
                title = (
                    item.get("title", "")
                    or item.get("topic_name", "")
                    or item.get("word", "")
                )
                url = item.get("url", "")
                if title and url:
                    tieba_links.append(
                        {"title": title, "url": url, "source": "贴吧"}
                    )

            links.extend(
                _interleave_link_groups(
                    [weibo_or_twitter, xhh_links, tieba_links]
                )
            )

        elif mode == "community":
            for post in raw_data.get("posts", []) or []:
                title = post.get("title", "")
                url = post.get("url", "")
                if title and url:
                    link = {"title": title, "url": url, "source": "喵宇宙社区"}
                    post_id = str(post.get("id") or "").strip()
                    if post_id:
                        # The public feed does not always expose per-card permalinks.
                        # Keep card identity internal so discover-page fallback URLs do
                        # not collapse every community card into one history entry.
                        link["dedupe_key"] = f"neko-community:{post_id}"
                    else:
                        link["dedupe_key"] = (
                            f"neko-community:{url}|{str(title).strip().casefold()}"
                        )
                    content = post.get("content", "")
                    if content and content != title:
                        link["description_hint"] = content
                    if post.get("author"):
                        link["author"] = post["author"]
                    if post.get("tags"):
                        link["tags"] = post["tags"]
                    if post.get("created_at"):
                        link["published_at"] = post["created_at"]
                    links.append(link)

        elif mode == "video":
            video = raw_data.get("video", {})
            items = video.get("videos", []) or video.get("posts", [])
            for item in items:
                title = item.get("title", "")
                url = item.get("url", "")
                if title and url:
                    default_source = (
                        "B站"
                        if raw_data.get("region", "china") == "china"
                        else "YouTube"
                    )
                    links.append(
                        _link_from_item(
                            item,
                            title=title,
                            url=url,
                            source=item.get("source") or default_source,
                        )
                    )

        elif mode == "home":
            bilibili = raw_data.get("bilibili", {})
            for item in bilibili.get("videos", []) or []:
                if item.get("title") and item.get("url"):
                    links.append(
                        _link_from_item(
                            item,
                            title=item["title"],
                            url=item["url"],
                            source="B站",
                        )
                    )
            weibo = raw_data.get("weibo", {})
            for item in weibo.get("trending", []) or []:
                if item.get("word") and item.get("url"):
                    links.append(
                        {
                            "title": item["word"],
                            "url": item["url"],
                            "source": "微博",
                        }
                    )
            reddit = raw_data.get("reddit", {})
            for item in reddit.get("posts", []) or []:
                if item.get("title") and item.get("url"):
                    links.append(
                        {
                            "title": item["title"],
                            "url": item["url"],
                            "source": "Reddit",
                        }
                    )
            twitter = raw_data.get("twitter", {})
            for item in twitter.get("trending", []) or []:
                title = item.get("name", "") or item.get("word", "")
                if title and item.get("url"):
                    links.append(
                        {
                            "title": title,
                            "url": item["url"],
                            "source": "Twitter",
                        }
                    )

        elif mode == "personal":
            region = raw_data.get("region", "china")
            platform_specs = (
                [
                    ("bilibili_dynamic", "dynamics", ("content",), "B站"),
                    ("weibo_dynamic", "statuses", ("content",), "微博"),
                    ("douyin_dynamic", "dynamics", ("content",), "抖音"),
                    ("kuaishou_dynamic", "dynamics", ("content",), "快手"),
                ]
                if region == "china"
                else [
                    ("reddit_dynamic", "posts", ("title", "content"), "Reddit"),
                    ("twitter_dynamic", "tweets", ("content",), "Twitter"),
                ]
            )
            platform_links: list[list[dict]] = []
            for data_key, items_key, title_keys, source_name in platform_specs:
                group: list[dict] = []
                for item in raw_data.get(data_key, {}).get(items_key, []) or []:
                    title = item.get("title", "") or next(
                        (item.get(key, "") for key in title_keys if item.get(key)),
                        "",
                    )
                    url = item.get("url", "")
                    if title and url:
                        group.append(
                            _link_from_item(
                                item,
                                title=title,
                                url=url,
                                source=source_name,
                            )
                        )
                if group:
                    platform_links.append(group)
            links.extend(_interleave_link_groups(platform_links))

        elif mode == "music":
            for item in raw_data.get("data", []):
                title = item.get("name", "")
                artist = item.get("artist", "")
                url = item.get("url", "")
                if title and url:
                    links.append(
                        {
                            "title": f"{title} - {artist}",
                            "url": url,
                            "source": "音乐推荐",
                        }
                    )
    except Exception as exc:
        (log or _link_extraction_logger).warning(f"提取链接失败 [{mode}]: {exc}")
    return links


async def _fetch_source(
    mode: str,
    *,
    command: ProactiveChatCommand,
    lanlan_name: str,
    log: Any,
) -> tuple[str, dict]:
    """Fetch and normalize one enabled source using the established shape."""
    screenshot_data = command.screenshot_data
    has_screenshot = bool(screenshot_data) and isinstance(screenshot_data, str)

    if mode == "vision":
        if not has_screenshot:
            raise ValueError("无截图数据（screenshot_data 为空或类型不正确）")
        compressed_b64 = ""
        try:
            b64_raw = (
                screenshot_data.split(",", 1)[1]
                if "," in screenshot_data
                else screenshot_data
            )
            compressed_b64 = await asyncio.to_thread(
                decode_and_compress_screenshot_b64,
                b64_raw,
                COMPRESS_TARGET_HEIGHT,
                COMPRESS_JPEG_QUALITY,
            )
            if command.avatar_position and isinstance(
                command.avatar_position, dict
            ):
                try:
                    from utils.language_utils import get_global_language_full
                    from utils.screenshot_utils import overlay_avatar_annotation

                    compressed_b64 = await asyncio.to_thread(
                        overlay_avatar_annotation,
                        compressed_b64,
                        command.avatar_position,
                        lanlan_name,
                        get_global_language_full(),
                    )
                except Exception as ann_err:
                    log.warning(
                        f"[{lanlan_name}] Phase 1 avatar annotation failed: {ann_err}"
                    )
            jpg_size_kb = len(compressed_b64) * 3 // 4 // 1024
            print(
                f"[{lanlan_name}] Vision 通道: 截图压缩完成 {jpg_size_kb}KB "
                "(Phase 2 将直接分析)"
            )
        except Exception as compress_err:
            log.warning(
                f"[{lanlan_name}] 截图压缩失败（Phase 2 将无法使用截图）: "
                f"{compress_err}"
            )
        return mode, {
            "window_title": command.window_title,
            "screenshot_b64": compressed_b64,
        }

    if mode == "news":
        content = await fetch_news_content(
            limit=PROACTIVE_PHASE1_FETCH_PER_SOURCE
        )
        if not content["success"]:
            raise ValueError(f"获取新闻失败: {content.get('error')}")
        _log_news_content(lanlan_name, content)
        return mode, {
            "formatted_content": format_news_content(content),
            "raw_data": content,
            "links": _extract_links_from_raw(mode, content),
        }

    if mode == "community":
        content = await fetch_neko_community_feed(
            # Keep the API page available for cooldown filtering; Phase 1 still
            # caps the prompt with its cross-source total candidate budget.
            limit=NEKO_COMMUNITY_FEED_PAGE_SIZE
        )
        if not content["success"]:
            raise ValueError(f"获取喵宇宙社区失败: {content.get('error')}")
        _log_neko_community_content(lanlan_name, content)
        return mode, {
            "formatted_content": format_neko_community_feed(content.get("posts", [])),
            "raw_data": content,
            "links": _extract_links_from_raw(mode, content),
        }

    if mode == "video":
        content = await fetch_video_content(
            limit=PROACTIVE_PHASE1_FETCH_PER_SOURCE
        )
        if not content["success"]:
            raise ValueError(f"获取视频失败: {content.get('error')}")
        _log_video_content(lanlan_name, content)
        return mode, {
            "formatted_content": format_video_content(content),
            "raw_data": content,
            "links": _extract_links_from_raw(mode, content),
        }

    if mode == "window":
        content = await fetch_window_context_content(limit=5)
        if not content["success"]:
            raise ValueError(f"获取窗口上下文失败: {content.get('error')}")
        raw_title = content.get("window_title", "")
        sanitized_title = raw_title[:30] + "..." if len(raw_title) > 30 else raw_title
        print(f"[{lanlan_name}] 成功获取窗口上下文: {sanitized_title}")
        return mode, {
            "formatted_content": format_window_context_content(content),
            "raw_data": content,
            "links": [],
        }

    if mode == "home":
        content = await fetch_trending_content(
            bilibili_limit=PROACTIVE_PHASE1_FETCH_PER_SOURCE,
            weibo_limit=PROACTIVE_PHASE1_FETCH_PER_SOURCE,
        )
        if not content["success"]:
            raise ValueError(f"获取首页推荐失败: {content.get('error')}")
        _log_trending_content(lanlan_name, content)
        return mode, {
            "formatted_content": format_trending_content(content),
            "raw_data": content,
            "links": _extract_links_from_raw(mode, content),
        }

    if mode == "personal":
        content = await fetch_personal_dynamics(
            limit=PROACTIVE_PHASE1_FETCH_PER_SOURCE
        )
        if not content["success"]:
            raise ValueError(f"获取个人动态失败: {content.get('error')}")
        _log_personal_dynamics(lanlan_name, content)
        return mode, {
            "formatted_content": format_personal_dynamics(content),
            "raw_data": content,
            "links": _extract_links_from_raw(mode, content),
        }

    if mode == "music":
        return mode, {
            "placeholder": True,
            "note": "关键词将在 Phase 1 开始前生成",
        }

    if mode == "meme":
        return mode, {
            "placeholder": True,
            "note": "关键词将由合并 Phase 1 LLM 生成",
        }

    raise ValueError(f"未知模式: {mode}")


async def collect_proactive_sources(
    *,
    command: ProactiveChatCommand,
    enabled_modes: list[str],
    lanlan_name: str,
    log: Any,
) -> dict[str, dict]:
    """Fetch enabled sources concurrently and retain successful results."""
    fetch_results = await asyncio.gather(
        *(
            _fetch_source(
                mode,
                command=command,
                lanlan_name=lanlan_name,
                log=log,
            )
            for mode in enabled_modes
        ),
        return_exceptions=True,
    )

    sources: dict[str, dict] = {}
    for index, result in enumerate(fetch_results):
        if isinstance(result, Exception):
            failed_mode = enabled_modes[index]
            log.warning(
                f"[{lanlan_name}] 信息源 [{failed_mode}] 获取失败: {result}"
            )
            continue
        mode, content = result
        sources[mode] = content
    return sources
