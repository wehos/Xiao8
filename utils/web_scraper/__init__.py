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
"""Compatibility facade for the web-scraper package.

The former monolithic ``utils.web_scraper`` module is split by domain while
retaining every historical top-level import and symbol.
"""
# ruff: noqa: F401

from __future__ import annotations

import asyncio
import httpx
from utils.external_http_client import get_external_http_client
import random
import re
import unicodedata
import platform
from typing import TYPE_CHECKING, Dict, List, Any, Optional, Union
from urllib.parse import quote
from utils.logger_config import get_module_logger
from utils.token_tracker import set_call_type
from utils.llm_client import SystemMessage, HumanMessage, create_chat_llm_async
import os

# bs4 惰性 import（各解析函数内首用加载，utils.module_warmup 后台预热兜底）：本模块被
# system_router 顶层引用、坐在 main_server 启动 import 链上，顶层 bs4 会拖慢端口就绪。
if TYPE_CHECKING:
    from bs4 import BeautifulSoup
from pathlib import Path
import json
import sys
from utils.file_utils import atomic_write_json

import locale

from ._shared import (
    USER_AGENTS,
    _extract_llm_text_content,
    _fix_bilibili_api_env,
    get_random_user_agent,
    is_china_region,
    logger,
)
from .platform_helpers import _get_bilibili_credential, _get_platform_cookies
from .bilibili_content import (
    enrich_bilibili_video,
    fetch_bilibili_home,
    fetch_bilibili_hot,
    fetch_bilibili_radar,
    format_bilibili_phase2_context,
)
from .trending_content import (
    NEKO_COMMUNITY_FEED_PAGE_SIZE,
    _fetch_content_by_region,
    _fetch_twitter_trending_fallback,
    _fetch_weibo_trending_fallback,
    _format_bilibili_videos,
    _format_reddit_posts,
    _format_twitch_live_streams,
    _format_youtube_videos,
    _format_score,
    _format_twitter_trending,
    _format_weibo_trending,
    fetch_bilibili_trending,
    fetch_neko_community_feed,
    fetch_news_content,
    fetch_tieba_content,
    fetch_reddit_popular,
    fetch_trending_content,
    fetch_twitter_trending,
    fetch_video_content,
    fetch_weibo_trending,
    fetch_xhh_feed_content,
    format_neko_community_feed,
    format_news_content,
    format_tieba_content,
    format_trending_content,
    format_video_content,
    format_xhh_feed,
    normalize_neko_community_feed,
    normalize_xhh_feed,
)
from .twitch_feed import fetch_twitch_live_streams
from .youtube_feed import fetch_youtube_home_feed
from .window_context import (
    _SEARCH_TEXT_WS_RE,
    _sanitize_search_text,
    clean_window_title,
    fetch_window_context_content,
    format_baidu_search_results,
    format_search_results,
    format_window_context_content,
    generate_diverse_queries,
    get_active_window_title,
    parse_baidu_results,
    parse_duckduckgo_results,
    parse_google_results,
    search_baidu,
    search_duckduckgo,
    search_google,
)
from .personal_dynamics import (
    _fetch_twitter_personal_web_scraping,
    fetch_bilibili_personal_dynamic,
    fetch_douyin_personal_dynamic,
    fetch_kuaishou_personal_dynamic,
    fetch_personal_dynamics,
    fetch_reddit_personal_dynamic,
    fetch_twitter_personal_dynamic,
    fetch_weibo_personal_dynamic,
    format_personal_dynamics,
)
from .__main__ import main
