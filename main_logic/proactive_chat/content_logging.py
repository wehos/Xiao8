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

"""Loggers for proactive content payloads (news, video and feeds).

Owned by the proactive-chat domain layer.
"""

__all__ = [
    "_log_neko_community_content",
    "_log_news_content",
    "_log_personal_dynamics",
    "_log_trending_content",
    "_log_video_content",
    "_tieba_log_title",
]


def _tieba_log_title(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    for key in ("title", "topic_name", "word"):
        title = str(item.get(key, "") or "").strip()
        if title:
            return title
    return ""


def _log_news_content(lanlan_name: str, news_content: dict):
    """
    Log news content fetch details.
    """
    region = news_content.get("region", "china")
    news_data = news_content.get("news", {})
    if news_data.get("success"):
        trending_list = news_data.get("trending", [])
        words = [item.get("word", "") for item in trending_list[:5]]
        if words:
            source = "微博热议话题" if region == "china" else "Twitter热门话题"
            print(f"[{lanlan_name}] 成功获取{source}:")
            for word in words:
                print(f"  - {word}")
    xhh_data = news_content.get("xhh") or {}
    if xhh_data.get("success"):
        posts = xhh_data.get("posts") or []
        titles = [post.get("title", "") for post in posts[:5]]
        if titles:
            print(f"[{lanlan_name}] 成功获取小黑盒首页内容:")
            for title in titles:
                print(f"  - {title}")

    tieba_data = news_content.get("tieba", {}) or {}
    if tieba_data.get("success"):
        nested = tieba_data.get("tieba", {}) or {}
        posts = tieba_data.get("posts", []) or nested.get("posts", [])
        topics = tieba_data.get("topics", []) or nested.get("topics", [])
        tieba_items = list(posts or []) + list(topics or [])
        titles = [
            title
            for item in tieba_items
            if (title := _tieba_log_title(item))
        ][:5]
        if titles:
            print(f"[{lanlan_name}] 成功获取贴吧资源池: {len(tieba_items)} 条")
            for title in titles:
                print(f"  - {title}")


def _log_neko_community_content(lanlan_name: str, community_content: dict):
    """Log cards fetched through the independent N.E.K.O community source."""
    posts = community_content.get("posts") or []
    titles = [post.get("title", "") for post in posts[:5]]
    if titles:
        print(f"[{lanlan_name}] 成功获取喵宇宙社区内容:")
        for title in titles:
            print(f"  - {title}")


def _log_video_content(lanlan_name: str, video_content: dict):
    """
    Log video content fetch details.
    """
    region = video_content.get("region", "china")
    video_data = video_content.get("video", {})
    if video_data.get("success"):
        videos = video_data.get("videos", [])
        titles = [video.get("title", "") for video in videos[:5]]
        if titles:
            if region == "china":
                source = "B站视频"
            elif video_data.get("source") == "mixed":
                source = "Twitch 与 YouTube 视频"
            elif video_data.get("source") == "twitch":
                source = "Twitch 直播"
            else:
                source = "YouTube视频"
            print(f"[{lanlan_name}] 成功获取{source}:")
            for title in titles:
                print(f"  - {title}")


def _log_trending_content(lanlan_name: str, trending_content: dict):
    """
    Log homepage recommendation content fetch details.
    """
    content_details = []

    bilibili_data = trending_content.get("bilibili", {})
    if bilibili_data.get("success"):
        videos = bilibili_data.get("videos", [])
        titles = [video.get("title", "") for video in videos[:5]]
        if titles:
            content_details.append("B站视频:")
            for title in titles:
                content_details.append(f"  - {title}")

    weibo_data = trending_content.get("weibo", {})
    if weibo_data.get("success"):
        trending_list = weibo_data.get("trending", [])
        words = [item.get("word", "") for item in trending_list[:5]]
        if words:
            content_details.append("微博话题:")
            for word in words:
                content_details.append(f"  - {word}")

    reddit_data = trending_content.get("reddit", {})
    if reddit_data.get("success"):
        posts = reddit_data.get("posts", [])
        titles = [post.get("title", "") for post in posts[:5]]
        if titles:
            content_details.append("Reddit热门帖子:")
            for title in titles:
                content_details.append(f"  - {title}")

    twitter_data = trending_content.get("twitter", {})
    if twitter_data.get("success"):
        trending_list = twitter_data.get("trending", [])
        words = [item.get("word", "") for item in trending_list[:5]]
        if words:
            content_details.append("Twitter热门话题:")
            for word in words:
                content_details.append(f"  - {word}")

    if content_details:
        print(f"[{lanlan_name}] 成功获取首页推荐:")
        for detail in content_details:
            print(detail)
    else:
        print(f"[{lanlan_name}] 成功获取首页推荐 - 但未获取到具体内容")


def _log_personal_dynamics(lanlan_name: str, personal_content: dict):
    """
    Log personal feed content fetch details.
    """
    content_details = []

    bilibili_dynamic = personal_content.get("bilibili_dynamic", {})
    if bilibili_dynamic.get("success"):
        dynamics = bilibili_dynamic.get("dynamics", [])
        bilibili_contents = [
            dynamic.get("content", dynamic.get("title", "")) for dynamic in dynamics[:5]
        ]
        if bilibili_contents:
            content_details.append("B站动态:")
            for content in bilibili_contents:
                content_details.append(f"  - {content}")

    weibo_dynamic = personal_content.get("weibo_dynamic", {})
    if weibo_dynamic.get("success"):
        dynamics = weibo_dynamic.get("statuses", [])
        weibo_contents = [dynamic.get("content", "") for dynamic in dynamics[:5]]
        if weibo_contents:
            content_details.append("微博动态:")
            for content in weibo_contents:
                content_details.append(f"  - {content}")

    if content_details:
        print(f"[{lanlan_name}] 成功获取个人动态:")
        for detail in content_details:
            print(detail)
    else:
        print(f"[{lanlan_name}] 成功获取个人动态 - 但未获取到具体内容")
