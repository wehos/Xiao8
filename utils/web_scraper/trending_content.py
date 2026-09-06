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
"""Trending-content fetchers and formatters."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from itertools import zip_longest
import httpx
from utils.cookies_login import load_cookies_from_file
from utils.external_http_client import get_external_http_client
from utils.social_base import social_base_url
import random
import re
import time
from typing import TYPE_CHECKING, Dict, List, Any
from urllib.parse import quote, urljoin, urlparse

# bs4 惰性 import（各解析函数内首用加载，utils.module_warmup 后台预热兜底）：本模块被
# system_router 顶层引用、坐在 main_server 启动 import 链上，顶层 bs4 会拖慢端口就绪。
if TYPE_CHECKING:
    from bs4 import BeautifulSoup

from ._shared import get_random_user_agent, is_china_region, logger
from .platform_helpers import (
    _get_platform_cookies,
    build_xhh_cookie_header,
    build_xhh_request_params,
)
from .youtube_feed import fetch_youtube_home_feed
from .twitch_feed import fetch_twitch_live_streams
from .bilibili_content import fetch_bilibili_radar


XHH_API_BASE = "https://api.xiaoheihe.cn"
XHH_FEEDS_PATH = "/bbs/app/feeds"
XHH_WEB_LINK = "https://www.xiaoheihe.cn/app/bbs/link/{link_id}"
XHH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
# The community API's discover feed is deliberately fetched as its first page
# of 60 cards. The caller's smaller ``limit`` is applied after normalization,
# so Phase 1 keeps its existing prompt budget while still getting a varied pool.
NEKO_COMMUNITY_FEED_PAGE_SIZE = 60


def _neko_community_urls() -> tuple[str, str]:
    """Return community feed and discover URLs for the configured social host."""

    base_url = social_base_url().rstrip("/")
    return f"{base_url}/api/feed", f"{base_url}/discover"


async def fetch_bilibili_trending(limit: int = 30) -> Dict[str, Any]:
    """Fetch Bilibili homepage and public hot feeds as one content radar."""
    return await fetch_bilibili_radar(limit=max(1, min(limit, 10)))

async def fetch_reddit_popular(limit: int = 10) -> Dict[str, Any]:
    """
    Fetch Reddit hot posts
    Uses Reddit's JSON API to fetch hot posts from r/popular
    
    Args:
        limit: maximum number of posts to return
    
    Returns:
        Dict with success status and post list
    """
    try:
        # Reddit的JSON API端点
        url = f"https://www.reddit.com/r/popular/hot.json?limit={limit}"
        
        headers = {
            'User-Agent': get_random_user_agent(),
            'Accept': 'application/json',
        }
        
        await asyncio.sleep(random.uniform(0.1, 0.5))
        
        client = get_external_http_client()
        response = await client.get(url, headers=headers, timeout=5.0)
        response.raise_for_status()
        data = response.json()

        posts = []
        children = data.get('data', {}).get('children', [])

        for item in children[:limit]:
            post_data = item.get('data', {})

            # 跳过NSFW内容
            if post_data.get('over_18'):
                continue

            subreddit = post_data.get('subreddit', '')
            title = post_data.get('title', '')
            score = post_data.get('score', 0)
            num_comments = post_data.get('num_comments', 0)
            permalink = post_data.get('permalink', '')

            posts.append({
                'title': title,
                'subreddit': f"r/{subreddit}",
                'score': _format_score(score),
                'comments': _format_score(num_comments),
            })
            if permalink:
                posts[-1]['url'] = f"https://www.reddit.com{permalink}"
            else:
                posts[-1]['url'] = ''

        if posts:
            logger.info(f"从Reddit获取到{len(posts)}条热门帖子")
            return {
                'success': True,
                'posts': posts
            }
        else:
            return {
                'success': False,
                'error': 'Reddit返回空数据',
                'posts': []
            }

    except httpx.TimeoutException:
        logger.exception("获取Reddit热门超时")
        return {
            'success': False,
            'error': '请求超时',
            'posts': []
        }
    except Exception as e:
        logger.exception(f"获取Reddit热门失败: {e}")
        return {
            'success': False,
            'error': str(e),
            'posts': []
        }

def _format_score(count: int) -> str:
    """Format Reddit scores/comment counts"""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    elif count >= 1_000:
        return f"{count / 1_000:.1f}K"
    elif count > 0:
        return str(count)
    return "0"

async def fetch_weibo_trending(limit: int = 10) -> Dict[str, Any]:
    """
    Fetch trending Weibo topics
    Prefers the s.weibo.com hot-search page (refreshes more often), which requires cookies
    Falls back to the public API on failure
    """
    try:
        # 动态获取平台 Cookie，拒绝硬编码
        weibo_cookies = await asyncio.to_thread(_get_platform_cookies, 'weibo')
        sub_cookie = weibo_cookies.get('SUB') or weibo_cookies.get('sub', '')
        if sub_cookie:
            cookie_header = f"SUB={sub_cookie}"
        else:
            cookie_header = ""
        
        # 优先使用s.weibo.com热搜页面（刷新频率更高）
        url = "https://s.weibo.com/top/summary?cate=realtimehot"
        
        headers = {
            'User-Agent': get_random_user_agent(),
            'Referer': 'https://s.weibo.com/',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        }
        if cookie_header:
            headers['Cookie'] = cookie_header
        
        # 添加随机延迟
        await asyncio.sleep(random.uniform(0.1, 0.5))
        
        client = get_external_http_client()
        response = await client.get(url, headers=headers, timeout=5.0)
        response.raise_for_status()

        # 检查是否重定向到登录页面
        if 'passport' in str(response.url):
            logger.warning("微博Cookie可能已过期，回退到公开API")
            return await _fetch_weibo_trending_fallback(limit)

        html = response.text
        # BS4 解析放线程池（与 Google/Baidu/Twitter 链路一致）
        from bs4 import BeautifulSoup
        soup = await asyncio.to_thread(BeautifulSoup, html, 'lxml')

        # 解析热搜列表 (td-02 class)
        td_items = soup.find_all('td', class_='td-02')

        if not td_items:
            logger.warning("未找到热搜数据，回退到公开API")
            return await _fetch_weibo_trending_fallback(limit)

        trending_list = []
        for i, td in enumerate(td_items):
            if len(trending_list) >= limit:
                break

            a_tag = td.find('a')
            span = td.find('span')

            if a_tag:
                word = a_tag.get_text(strip=True)
                if not word:
                    continue

                # 获取链接
                href = a_tag.get('href', '')
                # 构建完整URL（相对链接需要加上域名）
                if href and not href.startswith('http'):
                    href = f"https://s.weibo.com{href}"

                # 解析热度值
                if span:
                    hot_text = span.get_text(strip=True)
                else:
                    hot_text = ''
                # 热度可能包含类型标签如"剧集 336075"，需要提取数字
                hot_match = re.search(r'(\d+)', hot_text)
                if hot_match:
                    raw_hot = int(hot_match.group(1))
                else:
                    raw_hot = 0

                # 提取标签（如"剧集"、"晚会"等）
                if hot_text:
                    note = re.sub(r'\d+', '', hot_text).strip()
                else:
                    note = ''

                trending_list.append({
                    'word': word,
                    'raw_hot': raw_hot,
                    'note': note,
                    'rank': i + 1,
                    'url': href
                })

        if trending_list:
            logger.info(f"成功从s.weibo.com获取{len(trending_list)}条热搜")
            return {
                'success': True,
                'trending': trending_list
            }
        else:
            return await _fetch_weibo_trending_fallback(limit)

    except Exception as e:
        logger.warning(f"s.weibo.com热搜获取失败: {e}，回退到公开API")
        return await _fetch_weibo_trending_fallback(limit)

async def _fetch_weibo_trending_fallback(limit: int = 10) -> Dict[str, Any]:
    """
    Weibo hot-search fallback — uses the public ajax API
    """
    try:
        url = "https://weibo.com/ajax/side/hotSearch"
        
        headers = {
            'User-Agent': get_random_user_agent(),
            'Referer': 'https://weibo.com',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'DNT': '1',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
        }
        
        await asyncio.sleep(random.uniform(0.1, 0.5))
        
        client = get_external_http_client()
        response = await client.get(url, headers=headers, timeout=5.0)
        response.raise_for_status()
        data = response.json()

        if data.get('ok') == 1:
            trending_list = []
            realtime_list = data.get('data', {}).get('realtime', [])

            for item in realtime_list[:limit]:
                if item.get('is_ad'):
                    continue

                word = item.get('word', '')
                # 构建搜索URL
                if word:
                    search_url = f"https://s.weibo.com/weibo?q={quote(word)}"
                else:
                    search_url = ''

                trending_list.append({
                    'word': word,
                    'raw_hot': item.get('raw_hot', 0),
                    'note': item.get('note', ''),
                    'rank': item.get('rank', 0),
                    'url': search_url
                })

            return {
                'success': True,
                'trending': trending_list[:limit]
            }
        else:
            logger.error("微博公开API返回错误")
            return {
                'success': False,
                'error': '微博API返回错误'
            }

    except httpx.TimeoutException:
        logger.exception("获取微博热议话题超时")
        return {
            'success': False,
            'error': '请求超时'
        }
    except Exception as e:
        logger.exception(f"获取微博热议话题失败: {e}")
        return {
            'success': False,
            'error': str(e)
        }

async def fetch_twitter_trending(limit: int = 10) -> Dict[str, Any]:
    """
    Fetch Twitter/X trending topics
    Uses Twitter's explore page to fetch trends
    
    Args:
        limit: maximum number of trends to return
    
    Returns:
        Dict with success status and trend list
    """
    try:
        # Twitter探索/热门页面
        url = "https://twitter.com/explore/tabs/trending"
        
        headers = {
            'User-Agent': get_random_user_agent(),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'DNT': '1',
        }
        
        await asyncio.sleep(random.uniform(0.1, 0.5))
        
        client = get_external_http_client()
        response = await client.get(url, headers=headers, timeout=5.0)
        response.raise_for_status()
        html_content = response.text

        # 从页面解析热门话题
        trending_list = []

        # 尝试从页面的JSON数据中提取热门话题
        trend_pattern = r'"trend":\{[^}]*"name":"([^"]+)"'
        tweet_count_pattern = r'"tweetCount":"([^"]+)"'

        trends = re.findall(trend_pattern, html_content)
        tweet_counts = re.findall(tweet_count_pattern, html_content)

        for i, trend in enumerate(trends[:limit]):
            if trend and not trend.startswith('#'):
                if not trend.startswith('@'):
                    trend = '#' + trend

            # 构建搜索URL
            if trend:
                search_url = f"https://twitter.com/search?q={quote(trend)}"
            else:
                search_url = ''

            trending_list.append({
                'word': trend,
            })
            if i < len(tweet_counts):
                trending_list[-1]['tweet_count'] = tweet_counts[i]
            else:
                trending_list[-1]['tweet_count'] = 'N/A'
            trending_list[-1]['note'] = ''
            trending_list[-1]['rank'] = i + 1
            trending_list[-1]['url'] = search_url

        if trending_list:
            return {
                'success': True,
                'trending': trending_list
            }
        else:
            return await _fetch_twitter_trending_fallback(limit)

    except httpx.TimeoutException:
        logger.exception("获取Twitter热门超时")
        return {
            'success': False,
            'error': '请求超时'
        }
    except Exception as e:
        logger.exception(f"获取Twitter热门失败: {e}")
        return await _fetch_twitter_trending_fallback(limit)

async def _fetch_twitter_trending_fallback(limit: int = 10) -> Dict[str, Any]:
    """
    Twitter trends fallback
    Uses third-party services to fetch trends, since Twitter's official API requires OAuth
    """
    
    def _parse_trends24(soup: BeautifulSoup, limit: int) -> List[Dict[str, Any]]:
        """Parse the Trends24 page"""
        trending_list = []
        trend_cards = soup.select('.trend-card__list li a')
        for i, item in enumerate(trend_cards[:limit]):
            trend_text = item.get_text(strip=True)
            if trend_text:
                search_url = f"https://twitter.com/search?q={quote(trend_text)}"
                trending_list.append({
                    'word': trend_text,
                    'tweet_count': 'N/A',
                    'note': '',
                    'rank': i + 1,
                    'url': search_url
                })
        return trending_list
    
    def _parse_getdaytrends(soup: BeautifulSoup, limit: int) -> List[Dict[str, Any]]:
        """Parse the GetDayTrends page"""
        trending_list = []
        trend_items = soup.select('table.table tr td a')
        for i, item in enumerate(trend_items[:limit]):
            trend_text = item.get_text(strip=True)
            if trend_text:
                search_url = f"https://twitter.com/search?q={quote(trend_text)}"
                trending_list.append({
                    'word': trend_text,
                    'tweet_count': 'N/A',
                    'note': '',
                    'rank': i + 1,
                    'url': search_url
                })
        return trending_list
    
    # 第三方热门话题源列表（按优先级排序）
    fallback_sources = [
        {
            'name': 'Trends24',
            'url': 'https://trends24.in/',
            'parser': _parse_trends24
        },
        {
            'name': 'GetDayTrends',
            'url': 'https://getdaytrends.com/',
            'parser': _parse_getdaytrends
        }
    ]
    
    headers = {
        'User-Agent': get_random_user_agent(),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    
    # 按优先级遍历所有数据源
    for source in fallback_sources:
        try:
            await asyncio.sleep(random.uniform(0.1, 0.3))
            
            client = get_external_http_client()
            response = await client.get(source['url'], headers=headers, timeout=5.0)

            if response.status_code == 200:
                # BS4 解析 + 解析器迭代统一放线程池，避免阻塞 event loop
                def _parse_in_thread(html: str, parser, _limit: int) -> List[Dict[str, Any]]:
                    from bs4 import BeautifulSoup
                    return parser(BeautifulSoup(html, 'lxml'), _limit)
                trending_list = await asyncio.to_thread(
                    _parse_in_thread, response.text, source['parser'], limit
                )

                if trending_list:
                    logger.info(f"从{source['name']}获取到{len(trending_list)}条Twitter热门")
                    return {
                        'success': True,
                        'trending': trending_list,
                        'source': source['name'].lower().replace(' ', '')
                    }
        except Exception as e:
            logger.warning(f"{source['name']}获取失败: {e}")
            continue
    
    # 所有第三方源都失败，返回提示信息
    logger.warning("所有Twitter热门数据源均不可用")
    return {
        'success': False,
        'error': 'Twitter热门数据暂时无法获取，请稍后重试或访问 twitter.com/explore',
        'trending': []
    }

async def fetch_trending_content(bilibili_limit: int = 10, weibo_limit: int = 10, 
                                  reddit_limit: int = 10, twitter_limit: int = 10) -> Dict[str, Any]:
    """
    Fetch trending content based on the user's region
    
    Chinese region: Bilibili videos and trending Weibo topics
    non-Chinese region: Reddit hot posts and Twitter trends
    
    Args:
        bilibili_limit: max Bilibili videos (Chinese region)
        weibo_limit: max Weibo topics (Chinese region)
        reddit_limit: max Reddit posts (non-Chinese region)
        twitter_limit: max Twitter trends (non-Chinese region)
    
    Returns:
        Dict with success status and trending content
        Chinese region: 'bilibili' and 'weibo' keys
        non-Chinese region: 'reddit' and 'twitter' keys
    """
    try:
        # 检测用户区域
        china_region = is_china_region()
        
        if china_region:
            # Chinese region: Use Bilibili and Weibo
            logger.info("检测到中文区域，获取B站和微博热门内容")
            
            bilibili_task = fetch_bilibili_trending(bilibili_limit)
            weibo_task = fetch_weibo_trending(weibo_limit)
            
            
            bilibili_result, weibo_result = await asyncio.gather(
                bilibili_task, 
                weibo_task,
                return_exceptions=True
            )

            # 处理异常
            if isinstance(bilibili_result, Exception):
                logger.error(f"B站爬取异常: {bilibili_result}")
                bilibili_result = {'success': False, 'error': str(bilibili_result)}
            
            if isinstance(weibo_result, Exception):
                logger.error(f"微博爬取异常: {weibo_result}")
                weibo_result = {'success': False, 'error': str(weibo_result)}
            
            # 检查是否至少有一个成功
            if not bilibili_result.get('success') and not weibo_result.get('success'):
                return {
                    'success': False,
                    'error': '无法获取任何热门内容',
                    'region': 'china',
                    'bilibili': bilibili_result,
                    'weibo': weibo_result
                }
            
            return {
                'success': True,
                'region': 'china',
                'bilibili': bilibili_result,
                'weibo': weibo_result
            }
        else:
            # 非中文区域：使用Reddit和Twitter
            logger.info("检测到非中文区域，获取Reddit和Twitter热门内容")
            
            reddit_task = fetch_reddit_popular(reddit_limit)
            twitter_task = fetch_twitter_trending(twitter_limit)
            
            reddit_result, twitter_result = await asyncio.gather(
                reddit_task,
                twitter_task,
                return_exceptions=True
            )
            
            # 处理异常
            if isinstance(reddit_result, Exception):
                logger.error(f"Reddit爬取异常: {reddit_result}")
                reddit_result = {'success': False, 'error': str(reddit_result)}
            
            if isinstance(twitter_result, Exception):
                logger.error(f"Twitter爬取异常: {twitter_result}")
                twitter_result = {'success': False, 'error': str(twitter_result)}
            
            # 检查是否至少有一个成功
            if not reddit_result.get('success') and not twitter_result.get('success'):
                return {
                    'success': False,
                    'error': '无法获取任何热门内容',
                    'region': 'non-china',
                    'reddit': reddit_result,
                    'twitter': twitter_result
                }
            
            return {
                'success': True,
                'region': 'non-china',
                'reddit': reddit_result,
                'twitter': twitter_result
            }
        
    except Exception as e:
        logger.error(f"获取热门内容失败: {e}")
        return {
            'success': False,
            'error': str(e)
        }

async def _fetch_content_by_region(
    china_fetch_func,
    non_china_fetch_func,
    limit: int,
    content_key: str,
    china_log_msg: str,
    non_china_log_msg: str
) -> Dict[str, Any]:
    """
    Generic helper for fetching content based on the user's region
    
    Args:
        china_fetch_func: async fetch function used in the Chinese region
        non_china_fetch_func: async fetch function used in the non-Chinese region
        limit: maximum amount of content
        content_key: content key in the result ('video' or 'news')
        china_log_msg: log message for the Chinese region
        non_china_log_msg: log message for the non-Chinese region
    
    Returns:
        Dict with success status and content
    """
    china_region = is_china_region()
    if china_region:
        region = 'china'
    else:
        region = 'non-china'
    
    try:
        if china_region:
            logger.info(china_log_msg)
            result = await china_fetch_func(limit)
            response = {
                'success': result.get('success', False),
                'region': region,
                content_key: result
            }
        else:
            logger.info(non_china_log_msg)
            result = await non_china_fetch_func(limit)
            response = {
                'success': result.get('success', False),
                'region': region,
                content_key: result
            }
        
        if not result.get('success'):
            source = result.get('source') or content_key
            response['error'] = result.get('error') or f'{source} 获取失败（无错误详情）'
        return response
            
    except Exception as e:
        logger.error(f"获取内容失败: content_key={content_key} region={region} error={e}")
        return {
            'success': False,
            'error': str(e)
        }

async def fetch_video_content(limit: int = 10) -> Dict[str, Any]:
    """
    Fetch video content based on the user's region
    
    Chinese region: Bilibili homepage videos
    non-Chinese region: followed Twitch live streams and YouTube recommendations in parallel
    
    Args:
        limit: maximum amount of content
    
    Returns:
        Dict with success status and video content
    """
    if is_china_region():
        return await _fetch_content_by_region(
            china_fetch_func=fetch_bilibili_trending,
            non_china_fetch_func=fetch_youtube_home_feed,
            limit=limit,
            content_key='video',
            china_log_msg="检测到中文区域，获取B站视频内容",
            non_china_log_msg="检测到非中文区域，获取 YouTube 首页 Feed",
        )

    logger.info("检测到非中文区域，并行获取 Twitch 直播与 YouTube 视频")
    twitch_result, youtube_result = await asyncio.gather(
        fetch_twitch_live_streams(limit),
        fetch_youtube_home_feed(limit),
        return_exceptions=True,
    )
    if isinstance(twitch_result, Exception):
        logger.warning(f"Twitch 直播获取失败: {twitch_result}")
        twitch_result = {"success": False, "source": "twitch", "videos": [], "error": str(twitch_result)}
    if isinstance(youtube_result, Exception):
        logger.warning(f"YouTube 视频获取失败: {youtube_result}")
        youtube_result = {"success": False, "source": "youtube", "videos": [], "error": str(youtube_result)}

    twitch_videos = list(twitch_result.get("videos") or []) if twitch_result.get("success") else []
    youtube_videos = list(youtube_result.get("videos") or []) if youtube_result.get("success") else []
    merged_videos = [
        item
        for pair in zip_longest(twitch_videos, youtube_videos)
        for item in pair
        if item is not None
    ]
    success = bool(twitch_result.get("success") or youtube_result.get("success"))
    response = {
        "success": success,
        "region": "non-china",
        "video": {"success": success, "source": "mixed", "videos": merged_videos},
        "twitch": twitch_result,
        "youtube": youtube_result,
    }
    if not success:
        errors = [str(item.get("error")) for item in (twitch_result, youtube_result) if item.get("error")]
        response["error"] = "; ".join(errors) if errors else "Twitch 与 YouTube 获取失败（无错误详情）"
    return response

async def fetch_news_content(limit: int = 10) -> Dict[str, Any]:
    """
    Fetch news/trending-topic content based on the user's region
    
    Chinese region: trending Weibo topics, Tieba community discussions,
    and Xiaoheihe feed
    non-Chinese region: Twitter trends and Xiaoheihe feed
    
    Args:
        limit: maximum amount of content
    
    Returns:
        Dict with success status and news content
    """
    china_region = is_china_region()
    region = 'china' if china_region else 'non-china'
    try:
        if china_region:
            logger.info("检测到中文区域，并行获取微博、贴吧与小黑盒内容")
            weibo_result, tieba_result, xhh_result = await asyncio.gather(
                fetch_weibo_trending(limit),
                fetch_tieba_content(
                    limit=limit,
                    candidate_limit=max(_tieba_limit(limit) * 4, 20),
                ),
                fetch_xhh_feed_content(limit),
                return_exceptions=True,
            )
            if isinstance(weibo_result, Exception):
                logger.warning(f"微博热议话题获取失败: {weibo_result}")
                weibo_result = {'success': False, 'error': str(weibo_result)}
            if isinstance(tieba_result, Exception):
                logger.warning(f"贴吧社区讨论获取失败: {tieba_result}")
                tieba_result = {'success': False, 'error': str(tieba_result)}
            if isinstance(xhh_result, Exception):
                logger.warning(f"小黑盒首页内容获取失败: {xhh_result}")
                xhh_result = {'success': False, 'error': str(xhh_result), 'posts': []}

            source_results = (weibo_result, tieba_result, xhh_result)
            success = any(item.get('success') for item in source_results)
            response: Dict[str, Any] = {
                'success': success,
                'region': region,
                'news': weibo_result,
                'tieba': tieba_result,
                'xhh': xhh_result,
            }
            if not success:
                errors = [
                    str(item.get('error'))
                    for item in source_results
                    if item.get('error')
                ]
                response['error'] = '; '.join(errors) if errors else '暂时无法获取热议话题'
            return response

        logger.info("检测到非中文区域，并行获取 Twitter 与小黑盒内容")
        twitter_result, xhh_result = await asyncio.gather(
            fetch_twitter_trending(limit),
            fetch_xhh_feed_content(limit),
            return_exceptions=True,
        )
        if isinstance(twitter_result, Exception):
            logger.warning(f"Twitter 热门话题获取失败: {twitter_result}")
            twitter_result = {'success': False, 'error': str(twitter_result)}
        if isinstance(xhh_result, Exception):
            logger.warning(f"小黑盒首页内容获取失败: {xhh_result}")
            xhh_result = {'success': False, 'error': str(xhh_result), 'posts': []}

        success = bool(twitter_result.get('success') or xhh_result.get('success'))
        response = {
            'success': success,
            'region': region,
            'news': twitter_result,
            'xhh': xhh_result,
        }
        if not success:
            errors = [
                str(item.get('error'))
                for item in (twitter_result, xhh_result)
                if item.get('error')
            ]
            response['error'] = '; '.join(errors) if errors else 'Unable to fetch trending topics'
        return response
    except Exception as e:
        logger.error(f"获取热议内容失败: region={region} error={e}")
        return {
            'success': False,
            'region': region,
            'error': str(e),
        }

_TIEBA_DEFAULT_BARS = ("原神", "明日方舟", "崩坏星穹铁道", "steam", "minecraft")
_TIEBA_HOT_TOPIC_URL = "https://tieba.baidu.com/hottopic/browse/topicList"
_TIEBA_DETAIL_ENRICH_POSTS = 3
_TIEBA_DETAIL_RN = 12
_TIEBA_DETAIL_COMMENT_RN = 3
_TIEBA_HOT_REPLIES_PER_POST = 3
_TIEBA_REACTIONS_PER_REPLY = 2
_TIEBA_HOT_REPLY_MAX_CHARS = 120
_TIEBA_REACTION_MAX_CHARS = 80
_TIEBA_REPLY_MIN_CHARS = 8
_TIEBA_RECENT_TTL_SECONDS = 30 * 60
_TIEBA_RECENT_MAX_KEYS = 1000
_TIEBA_RECENT_KEYS: "OrderedDict[str, float]" = OrderedDict()
_TIEBA_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_TIEBA_PUNCT_ONLY_RE = re.compile(r"^[\W_]+$", re.UNICODE)
_TIEBA_LOW_QUALITY_RE = re.compile(
    r"(同城|人到付款|外围|约\s*吗|相\s*约|上门|招募吧主|撤销.*吧主|吧主管理权限|"
    r"点击这里提交贴吧bug|贷款|兼职|代练接单|加群|私聊|vx|微信|qq|资源合集|"
    r"全集|完结|4k|网盘|你懂的|软件可以直接|哪里都有|水楼|水贴|建个楼水|"
    r"氵|灌水|闲聊楼|记录楼|直播.*记录|打卡|签到|集中贴|专楼|长期楼|镇楼|"
    r"公告|吧务|交易|出物|收物|拼车|代购|擦边)",
    re.IGNORECASE,
)
_TIEBA_CONVERSATION_RE = re.compile(
    r"(如何评价|为什么|有没有|感觉|建议|版本|角色|剧情|机制|强度|攻略|新手|问题|讨论|"
    r"怎么看|怎么|求助|推荐|分析|体验|变化|更新)",
    re.IGNORECASE,
)


def _tieba_limit(limit: int) -> int:
    try:
        return max(1, min(int(limit), 10))
    except Exception:
        return 5


def _tieba_candidate_limit(limit: int | None, fallback: int) -> int:
    try:
        value = int(limit) if limit is not None else int(fallback)
    except Exception:
        value = fallback
    return max(1, min(value, 50))


def _clean_tieba_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _tieba_item_key(item: dict[str, Any]) -> str:
    key = (
        _clean_tieba_text(item.get("tid"))
        or _clean_tieba_text(item.get("topic_id"))
        or _clean_tieba_text(item.get("url"))
        or _clean_tieba_text(item.get("title"))
    )
    item_type = _clean_tieba_text(item.get("type")) or "item"
    return f"{item_type}:{key}" if key else ""


def _tieba_item_recent_keys(item: dict[str, Any]) -> list[str]:
    keys = []
    item_key = _tieba_item_key(item)
    if item_key:
        keys.append(item_key)
    title = _clean_tieba_text(item.get("title"))
    if title:
        keys.append(f"title:{title.casefold()}")
    return keys


def _prune_tieba_recent_keys(now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    expired = [
        key
        for key, seen_at in _TIEBA_RECENT_KEYS.items()
        if now - seen_at > _TIEBA_RECENT_TTL_SECONDS
    ]
    for key in expired:
        _TIEBA_RECENT_KEYS.pop(key, None)
    while len(_TIEBA_RECENT_KEYS) > _TIEBA_RECENT_MAX_KEYS:
        _TIEBA_RECENT_KEYS.popitem(last=False)


def _filter_tieba_recent_items(items: list[dict[str, Any]], *, minimum: int = 1) -> list[dict[str, Any]]:
    _prune_tieba_recent_keys()
    fresh: list[dict[str, Any]] = []
    recent: list[dict[str, Any]] = []
    for item in items:
        keys = _tieba_item_recent_keys(item)
        if not keys or not any(key in _TIEBA_RECENT_KEYS for key in keys):
            fresh.append(item)
        else:
            recent.append(item)
    if len(fresh) >= minimum:
        return fresh
    return [*fresh, *recent[:max(0, minimum - len(fresh))]]


def _remember_tieba_items(posts: list[dict[str, Any]], topics: list[dict[str, Any]]) -> None:
    now = time.monotonic()
    for item in [*posts, *topics]:
        for key in _tieba_item_recent_keys(item):
            _TIEBA_RECENT_KEYS[key] = now
            _TIEBA_RECENT_KEYS.move_to_end(key)
    _prune_tieba_recent_keys(now)


def _safe_tieba_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0


def _tieba_heat_score(item: dict[str, Any]) -> float:
    return _safe_tieba_int(item.get("reply_num")) * 3 + _safe_tieba_int(item.get("view_num")) / 100


def _tieba_conversation_score(item: dict[str, Any]) -> float:
    text = f"{_clean_tieba_text(item.get('title'))} {_clean_tieba_text(item.get('abstract'))}"
    score = 0.0
    if _TIEBA_CONVERSATION_RE.search(text):
        score += 250.0
    title_len = len(_clean_tieba_text(item.get("title")))
    if 8 <= title_len <= 40:
        score += 40.0
    if title_len <= 4:
        score -= 80.0
    return score


def _tieba_sort_priority(item: dict[str, Any]) -> int:
    if item.get("type") == "post" and item.get("origin") == "bar":
        return 2
    if item.get("type") == "post":
        return 1
    return 0


def _tieba_rank_key(item: dict[str, Any]) -> tuple[int, float, float]:
    return (_tieba_sort_priority(item), _tieba_conversation_score(item), _tieba_heat_score(item))


def _is_low_quality_tieba_item(item: dict[str, Any], *, check_interaction: bool = True) -> bool:
    title = _clean_tieba_text(item.get("title"))
    abstract = _clean_tieba_text(item.get("abstract"))
    if not title:
        return True
    if item.get("is_top"):
        return True
    if _TIEBA_LOW_QUALITY_RE.search(f"{title} {abstract}"):
        return True
    if check_interaction and _safe_tieba_int(item.get("reply_num")) < 1 and _safe_tieba_int(item.get("view_num")) < 100:
        return True
    return False


def _clean_tieba_reply_text(value: Any) -> str:
    text = _clean_tieba_text(value)
    if not text:
        return ""
    text = _TIEBA_URL_RE.sub("", text)
    return _clean_tieba_text(text)


def _truncate_tieba_text(text: str, max_chars: int) -> str:
    text = _clean_tieba_text(text)
    if len(text) <= max_chars:
        return text
    return text[:max(1, max_chars - 1)].rstrip() + "…"


def _is_low_quality_tieba_reply(text: str, post: dict[str, Any]) -> bool:
    text = _clean_tieba_reply_text(text)
    if len(text) < _TIEBA_REPLY_MIN_CHARS:
        return True
    if _TIEBA_PUNCT_ONLY_RE.fullmatch(text):
        return True
    if _TIEBA_LOW_QUALITY_RE.search(text):
        return True

    norm = text.casefold()
    title = _clean_tieba_text(post.get("title")).casefold()
    abstract = _clean_tieba_text(post.get("abstract")).casefold()
    if title and (norm == title or norm in title):
        return True
    if abstract and (norm == abstract or norm in abstract):
        return True
    return False


def _tieba_comment_to_reaction(comment: Any, post: dict[str, Any]) -> dict[str, Any] | None:
    text = _clean_tieba_reply_text(getattr(comment, "text", ""))
    if _is_low_quality_tieba_reply(text, post):
        return None
    return {
        "text": _truncate_tieba_text(text, _TIEBA_REACTION_MAX_CHARS),
        "agree": _safe_tieba_int(getattr(comment, "agree", 0)),
        "create_time": _safe_tieba_int(getattr(comment, "create_time", 0)),
    }


def _tieba_post_to_hot_reply(detail_post: Any, source_post: dict[str, Any]) -> dict[str, Any] | None:
    text = _clean_tieba_reply_text(getattr(detail_post, "text", ""))
    if _is_low_quality_tieba_reply(text, source_post):
        return None

    reactions: list[dict[str, Any]] = []
    for comment in list(getattr(detail_post, "comments", []) or []):
        reaction = _tieba_comment_to_reaction(comment, source_post)
        if reaction:
            reactions.append(reaction)
        if len(reactions) >= _TIEBA_REACTIONS_PER_REPLY:
            break

    return {
        "text": _truncate_tieba_text(text, _TIEBA_HOT_REPLY_MAX_CHARS),
        "floor": _safe_tieba_int(getattr(detail_post, "floor", 0)),
        "agree": _safe_tieba_int(getattr(detail_post, "agree", 0)),
        "reply_num": _safe_tieba_int(getattr(detail_post, "reply_num", 0)),
        "is_thread_author": bool(getattr(detail_post, "is_thread_author", False)),
        "create_time": _safe_tieba_int(getattr(detail_post, "create_time", 0)),
        "reactions": reactions,
    }


async def _enrich_tieba_posts_with_hot_replies(posts: list[dict[str, Any]], errors: list[str]) -> None:
    """Attach a small HOT-floor sample to top Tieba candidates.

    This is best-effort enrichment for prompt context. Detail fetch failures are
    recorded as warnings and must not discard the already usable thread list.
    """
    targets = [post for post in posts if _clean_tieba_text(post.get("tid"))][:_TIEBA_DETAIL_ENRICH_POSTS]
    if not targets:
        return

    try:
        from aiotieba.enums import PostSortType
    except Exception as exc:
        errors.append(f"hot_replies: {exc}")
        logger.debug(f"Tieba hot replies enum import failed: {exc}")
        return

    async with _create_aiotieba_client() as client:
        for post in targets:
            tid_raw = _clean_tieba_text(post.get("tid"))
            try:
                tid = int(tid_raw)
            except Exception:
                continue
            try:
                detail_posts = await client.get_posts(
                    tid,
                    pn=1,
                    rn=_TIEBA_DETAIL_RN,
                    sort=PostSortType.HOT,
                    with_comments=True,
                    comment_sort_by_agree=True,
                    comment_rn=_TIEBA_DETAIL_COMMENT_RN,
                )
            except Exception as exc:
                errors.append(f"hot_replies:{tid}: {exc}")
                logger.debug(f"Tieba hot replies fetch failed [{tid}]: {exc}")
                continue

            hot_replies: list[dict[str, Any]] = []
            for detail_post in list(detail_posts or []):
                hot_reply = _tieba_post_to_hot_reply(detail_post, post)
                if hot_reply:
                    hot_replies.append(hot_reply)
                if len(hot_replies) >= _TIEBA_HOT_REPLIES_PER_POST:
                    break
            if hot_replies:
                post["hot_replies"] = hot_replies


def _diversify_tieba_posts(posts: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    selected: list[dict[str, Any]] = []
    selected_keys: set[str] = set()
    used_bars: set[str] = set()

    for post in posts:
        bar_name = _clean_tieba_text(post.get("bar_name"))
        key = _clean_tieba_text(post.get("tid")) or _clean_tieba_text(post.get("url"))
        if not key or not bar_name or bar_name in used_bars:
            continue
        selected.append(post)
        selected_keys.add(key)
        used_bars.add(bar_name)
        if len(selected) >= limit:
            return selected

    for post in posts:
        key = _clean_tieba_text(post.get("tid")) or _clean_tieba_text(post.get("url"))
        if not key or key in selected_keys:
            continue
        selected.append(post)
        selected_keys.add(key)
        if len(selected) >= limit:
            break
    return selected


def _get_aiotieba_client_class():
    import aiotieba

    return aiotieba.Client


def _create_aiotieba_client():
    Client = _get_aiotieba_client_class()
    return Client(proxy=True)


def _tieba_thread_to_post(thread: Any, bar_name: str) -> dict[str, Any] | None:
    tid = _clean_tieba_text(getattr(thread, "tid", ""))
    title = _clean_tieba_text(getattr(thread, "title", ""))
    if not tid or not title:
        return None
    text = _clean_tieba_text(getattr(thread, "text", ""))
    post = {
        "title": title,
        "url": f"https://tieba.baidu.com/p/{tid}",
        "abstract": text,
        "source": "贴吧",
        "bar_name": bar_name,
        "reply_num": _safe_tieba_int(getattr(thread, "reply_num", 0)),
        "view_num": _safe_tieba_int(getattr(thread, "view_num", 0)),
        "is_top": bool(getattr(thread, "is_top", False)),
        "tid": tid,
        "type": "post",
        "origin": "bar",
    }
    if _is_low_quality_tieba_item(post):
        return None
    return post


async def _fetch_tieba_bar_posts(bar_name: str, *, rn: int) -> list[dict[str, Any]]:
    async with _create_aiotieba_client() as client:
        threads = await client.get_threads(bar_name, pn=1, rn=rn)
    err = getattr(threads, "err", None)
    if err:
        raise RuntimeError(str(err))
    posts: list[dict[str, Any]] = []
    for thread in list(threads or []):
        post = _tieba_thread_to_post(thread, bar_name)
        if post:
            posts.append(post)
    return posts


def _tieba_topic_from_item(item: dict[str, Any]) -> dict[str, Any] | None:
    title = _clean_tieba_text(item.get("topic_name") or item.get("title"))
    url = _clean_tieba_text(item.get("topic_url") or item.get("url"))
    if not title or not url:
        return None
    topic = {
        "title": title,
        "url": url,
        "abstract": _clean_tieba_text(item.get("topic_desc") or item.get("abstract")),
        "source": "贴吧",
        "bar_name": _clean_tieba_text(item.get("forum_name") or ""),
        "reply_num": _safe_tieba_int(item.get("discuss_num")),
        "view_num": _safe_tieba_int(item.get("idx_num")),
        "is_top": False,
        "topic_id": _clean_tieba_text(item.get("topic_id")),
        "type": "topic",
    }
    if _is_low_quality_tieba_item(topic, check_interaction=False):
        return None
    return topic


async def _fetch_tieba_hot_topics(limit: int) -> list[dict[str, Any]]:
    client = get_external_http_client()
    response = await client.get(
        _TIEBA_HOT_TOPIC_URL,
        headers={
            "User-Agent": get_random_user_agent(),
            "Referer": "https://tieba.baidu.com/",
            "Accept": "application/json,text/plain,*/*",
        },
        timeout=5.0,
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else {}
    topic_list = (
        ((data or {}).get("bang_topic") or {}).get("topic_list")
        or ((data or {}).get("topic_list"))
        or []
    )
    topics: list[dict[str, Any]] = []
    for item in topic_list:
        if not isinstance(item, dict):
            continue
        topic = _tieba_topic_from_item(item)
        if topic:
            topics.append(topic)
        if len(topics) >= max(1, limit):
            break
    return topics


def _extract_post_links_from_topic_html(html: str, topic: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    posts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in re.finditer(r'href=["\'](?P<href>(?:https?:)?//tieba\.baidu\.com/p/\d+|/p/\d+)[^"\']*["\'][^>]*>(?P<title>.*?)</a>', html or "", re.I | re.S):
        href = match.group("href")
        url = urljoin("https://tieba.baidu.com", href)
        tid_match = re.search(r"/p/(\d+)", url)
        tid = tid_match.group(1) if tid_match else ""
        if not tid or tid in seen:
            continue
        seen.add(tid)
        raw_title = re.sub(r"<[^>]+>", "", match.group("title") or "")
        title = _clean_tieba_text(raw_title) or _clean_tieba_text(topic.get("title"))
        post = {
            "title": title,
            "url": f"https://tieba.baidu.com/p/{tid}",
            "abstract": _clean_tieba_text(topic.get("abstract")),
            "source": "贴吧",
            "bar_name": _clean_tieba_text(topic.get("bar_name")) or "热榜",
            "reply_num": _safe_tieba_int(topic.get("reply_num")),
            "view_num": _safe_tieba_int(topic.get("view_num")),
            "is_top": False,
            "tid": tid,
            "type": "post",
            "origin": "hot_topic",
        }
        if not _is_low_quality_tieba_item(post, check_interaction=False):
            posts.append(post)
        if len(posts) >= limit:
            break
    return posts


async def _fetch_tieba_topic_posts(topics: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if not topics or limit <= 0:
        return []
    client = get_external_http_client()
    posts: list[dict[str, Any]] = []
    for topic in topics[:3]:
        url = _clean_tieba_text(topic.get("url"))
        if not url:
            continue
        try:
            response = await client.get(
                url,
                headers={"User-Agent": get_random_user_agent(), "Referer": "https://tieba.baidu.com/"},
                timeout=5.0,
                follow_redirects=True,
            )
            if response.status_code >= 400:
                continue
            posts.extend(_extract_post_links_from_topic_html(response.text, topic, limit - len(posts)))
        except Exception as exc:
            logger.debug(f"Tieba hot topic page parse failed: {exc}")
        if len(posts) >= limit:
            break
    return posts


def _dedupe_tieba_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        key = _clean_tieba_text(item.get("tid")) or _clean_tieba_text(item.get("url"))
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


async def fetch_tieba_content(
    keyword: str = "",
    limit: int = 5,
    candidate_limit: int | None = None,
) -> Dict[str, Any]:
    """
    Fetch public Tieba community material for proactive chat.

    This is intentionally login-free and read-only: it reads public hot topics
    and forum thread lists, then best-effort enriches a few selected threads
    with HOT floor/comment snippets. It never sends cookies, writes content, or
    reads account state.
    """
    normalized_limit = _tieba_limit(limit)
    normalized_candidate_limit = _tieba_candidate_limit(candidate_limit, normalized_limit)
    clean_keyword = _clean_tieba_text(keyword)
    bars = []
    if clean_keyword:
        bars.append(clean_keyword)
    for bar in _TIEBA_DEFAULT_BARS:
        if bar not in bars:
            bars.append(bar)
    query = f"tieba bars: {', '.join(bars)}; hot_topics"
    errors: list[str] = []

    bar_tasks = [
        _fetch_tieba_bar_posts(bar, rn=max(normalized_candidate_limit * 2, 20))
        for bar in bars
    ]
    bar_results = await asyncio.gather(*bar_tasks, return_exceptions=True)
    posts: list[dict[str, Any]] = []
    for bar, result in zip(bars, bar_results):
        if isinstance(result, Exception):
            errors.append(f"{bar}: {result}")
            logger.debug(f"Tieba bar fetch failed [{bar}]: {result}")
            continue
        posts.extend(result)

    topics: list[dict[str, Any]] = []
    try:
        topics = await _fetch_tieba_hot_topics(limit=max(normalized_candidate_limit, 8))
    except Exception as exc:
        errors.append(f"hot_topics: {exc}")
        logger.debug(f"Tieba hot topic fetch failed: {exc}")

    if topics:
        try:
            posts.extend(await _fetch_tieba_topic_posts(topics, limit=normalized_candidate_limit))
        except Exception as exc:
            logger.debug(f"Tieba hot topic post extraction failed: {exc}")

    posts = _dedupe_tieba_items(posts)
    posts.sort(key=_tieba_rank_key, reverse=True)
    posts = _filter_tieba_recent_items(posts, minimum=normalized_limit)
    posts = _diversify_tieba_posts(posts, normalized_candidate_limit)
    topics = _dedupe_tieba_items(topics)
    topics.sort(key=lambda item: (_tieba_conversation_score(item), _tieba_heat_score(item)), reverse=True)
    topics = _filter_tieba_recent_items(topics, minimum=normalized_limit)
    topics = topics[:normalized_candidate_limit]

    if posts:
        await _enrich_tieba_posts_with_hot_replies(posts, errors)

    success = bool(posts or topics)
    result: Dict[str, Any] = {
        "success": success,
        "query": query,
        "tieba": {"success": success, "posts": posts, "topics": topics},
        "posts": posts,
        "topics": topics,
        "display_limit": normalized_limit,
        "candidate_limit": normalized_candidate_limit,
    }
    if not success:
        result["error"] = "; ".join(errors) if errors else "未找到可用贴吧帖子或热榜话题"
        result["formatted_content"] = ""
        return result

    result["formatted_content"] = format_tieba_content(result)
    _remember_tieba_items(posts, topics)
    if errors:
        result["warnings"] = errors
    return result

def _format_bilibili_videos(videos: List[Dict], limit: int = 5) -> List[str]:
    """Format mixed Bilibili homepage and hot-feed candidates."""
    output_lines = ["【B站内容雷达：首页推荐 + 全站热门】"]
    for i, video in enumerate(videos[:limit], 1):
        title = video.get('title', '')
        author = video.get('author', '')
        reason = video.get('reason', '') or video.get('rcmd_reason', '')
        description = video.get('description_hint', '')
        
        output_lines.append(f"{i}. {title}")
        details = [detail for detail in (author, reason) if detail]
        if details:
            output_lines.append(f"   {' | '.join(details)}")
        if description:
            output_lines.append(f"   简介: {description[:180]}")
    output_lines.append("")
    return output_lines

def _format_youtube_videos(videos: List[Dict], limit: int = 5) -> List[str]:
    """Format YouTube Home Feed videos."""
    output_lines = ["【YouTube 推荐】"]
    for i, video in enumerate(videos[:limit], 1):
        title = video.get('title', '')
        author = video.get('author', '')
        view_count = video.get('view_count', '')
        published_text = video.get('published_text', '')

        output_lines.append(f"{i}. {title}")
        details = [detail for detail in (author, view_count, published_text) if detail]
        if details:
            output_lines.append(f"   {' | '.join(details)}")
    output_lines.append("")
    return output_lines


def _format_twitch_live_streams(streams: List[Dict], limit: int = 5) -> List[str]:
    """Format followed Twitch live streams as lightweight conversation material.

    The English header and viewer unit are intentional: this is structured LLM
    context rather than user-interface copy. Keep them aligned with the fixture
    in ``test_twitch_feed.py``.
    """
    output_lines = ["[Followed Twitch live streams]"]
    for index, stream in enumerate(streams[:limit], 1):
        title = stream.get("title", "")
        if title:
            output_lines.append(f"{index}. {title}")
            details = [detail for detail in (stream.get("author", ""), stream.get("game_name", "")) if detail]
            viewers = stream.get("viewer_count", "")
            if viewers:
                details.append(f"{viewers} viewers")
            if details:
                output_lines.append(f"   {' | '.join(details)}")
    output_lines.append("")
    return output_lines

def _format_reddit_posts(posts: List[Dict], limit: int = 5) -> List[str]:
    """Format the Reddit post list"""
    output_lines = ["【Reddit Hot Posts】"]
    for i, post in enumerate(posts[:limit], 1):
        title = post.get('title', '')
        subreddit = post.get('subreddit', '')
        score = post.get('score', '')
        
        output_lines.append(f"{i}. {title}")
        if subreddit:
            output_lines.append(f"   {subreddit} | {score} upvotes")
    output_lines.append("")
    return output_lines

def _format_weibo_trending(trending_list: List[Dict], limit: int = 5) -> List[str]:
    """Format the trending Weibo topic list"""
    output_lines = ["【微博热议话题】"]
    for i, item in enumerate(trending_list[:limit], 1):
        word = item.get('word', '')
        note = item.get('note', '')
        
        line = f"{i}. {word}"
        if note:
            line += f" [{note}]"
        output_lines.append(line)
    output_lines.append("")
    return output_lines

def _format_twitter_trending(trending_list: List[Dict], limit: int = 5) -> List[str]:
    """Format the Twitter trend list"""
    output_lines = ["【Twitter Trending Topics】"]
    for i, item in enumerate(trending_list[:limit], 1):
        word = item.get('word', '')
        tweet_count = item.get('tweet_count', '')
        
        line = f"{i}. {word}"
        if tweet_count and tweet_count != 'N/A':
            line += f" ({tweet_count} tweets)"
        output_lines.append(line)
    output_lines.append("")
    return output_lines

def format_trending_content(trending_content: Dict[str, Any]) -> str:
    """
    Format trending content into a readable string
    
    Formats automatically by region:
    - Chinese region: Bilibili and Weibo content, displayed in Chinese
    - non-Chinese region: Reddit and Twitter content, displayed in English
    
    Args:
        trending_content: result returned by fetch_trending_content
    
    Returns:
        The formatted string
    """
    output_lines = []
    region = trending_content.get('region', 'china')
    
    if region == 'china':
        bilibili_data = trending_content.get('bilibili', {})
        if bilibili_data.get('success'):
            videos = bilibili_data.get('videos', [])
            output_lines.extend(_format_bilibili_videos(videos))
        
        weibo_data = trending_content.get('weibo', {})
        if weibo_data.get('success'):
            trending_list = weibo_data.get('trending', [])
            output_lines.extend(_format_weibo_trending(trending_list))
        
        if not output_lines:
            return "暂时无法获取推荐内容"
    else:
        reddit_data = trending_content.get('reddit', {})
        if reddit_data.get('success'):
            posts = reddit_data.get('posts', [])
            output_lines.extend(_format_reddit_posts(posts))
        
        twitter_data = trending_content.get('twitter', {})
        if twitter_data.get('success'):
            trending_list = twitter_data.get('trending', [])
            output_lines.extend(_format_twitter_trending(trending_list))
        
        if not output_lines:
            return "Unable to fetch trending content at the moment"
    
    return "\n".join(output_lines)

def format_video_content(video_content: Dict[str, Any]) -> str:
    """
    Format video content into a readable string
    
    Formats automatically by region:
    - Chinese region: Bilibili video content
    - non-Chinese region: followed Twitch live streams and YouTube recommendations
    
    Args:
        video_content: result returned by fetch_video_content
    
    Returns:
        The formatted string
    """
    region = video_content.get('region', 'china')
    video_data = video_content.get('video', {})
    
    if region == 'china':
        if video_data.get('success'):
            videos = video_data.get('videos', [])
            output_lines = _format_bilibili_videos(videos)
            return "\n".join(output_lines)
        return "暂时无法获取视频推荐内容"
    else:
        if video_data.get('success'):
            if video_data.get("source") == "mixed":
                output_lines = []
                twitch_data = video_content.get("twitch", {})
                youtube_data = video_content.get("youtube", {})
                if twitch_data.get("success"):
                    output_lines.extend(_format_twitch_live_streams(twitch_data.get("videos", [])))
                if youtube_data.get("success"):
                    output_lines.extend(_format_youtube_videos(youtube_data.get("videos", [])))
            else:
                videos = video_data.get('videos', [])
                output_lines = _format_twitch_live_streams(videos) if video_data.get("source") == "twitch" else _format_youtube_videos(videos)
            return "\n".join(output_lines)
        return "Unable to fetch Twitch or YouTube recommendations at the moment"

def format_news_content(news_content: Dict[str, Any]) -> str:
    """
    Format news content into a readable string
    
    Formats automatically by region:
    - Chinese region: trending Weibo topics, Tieba community discussions,
      and Xiaoheihe feed
    - non-Chinese region: Twitter trends and Xiaoheihe feed
    
    Args:
        news_content: result returned by fetch_news_content
    
    Returns:
        The formatted string
    """
    region = news_content.get('region', 'china')
    news_data = news_content.get('news', {})
    
    output_lines: list[str] = []
    if region == 'china':
        output_lines = []
        if news_data.get('success'):
            trending_list = news_data.get('trending', [])
            output_lines.extend(_format_weibo_trending(trending_list))
        tieba_data = news_content.get('tieba', {})
        if tieba_data.get('success'):
            formatted_tieba = format_tieba_content(tieba_data)
            if formatted_tieba:
                output_lines.append(formatted_tieba)

    else:
        if news_data.get('success'):
            trending_list = news_data.get('trending', [])
            output_lines.extend(_format_twitter_trending(trending_list))

    xhh_data = news_content.get('xhh', {})
    if xhh_data.get('success'):
        formatted_xhh = format_xhh_feed(xhh_data.get('posts', []))
        if formatted_xhh:
            output_lines.append("【小黑盒首页内容】" if region == 'china' else "【Xiaoheihe Home】")
            output_lines.append(formatted_xhh)

    if output_lines:
        return "\n".join(output_lines)
    return "暂时无法获取热议话题" if region == 'china' else "Unable to fetch trending topics at the moment"


def _plain_xhh_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _xhh_label_values(items: Any) -> list[str]:
    labels: list[str] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            value = item.get("name") or item.get("title") or item.get("text")
        else:
            value = item
        normalized = _plain_xhh_text(value)
        if normalized and normalized not in labels:
            labels.append(normalized)
    return labels


def normalize_xhh_feed(payload: dict[str, Any], *, limit: int = 10) -> list[dict[str, Any]]:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    raw_links = result.get("links") if isinstance(result.get("links"), list) else []
    posts: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for raw in raw_links:
        if not isinstance(raw, dict):
            continue
        try:
            link_id = int(raw.get("linkid") or raw.get("link_id") or 0)
        except (TypeError, ValueError):
            continue
        title = _plain_xhh_text(raw.get("title"))
        if link_id <= 0 or not title or link_id in seen_ids:
            continue
        seen_ids.add(link_id)
        user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
        posts.append(
            {
                "link_id": link_id,
                "title": title,
                "description": _plain_xhh_text(raw.get("description")),
                "author": _plain_xhh_text(user.get("username")),
                "topics": _xhh_label_values(raw.get("topics")),
                "tags": _xhh_label_values(raw.get("hashtags")),
                "url": XHH_WEB_LINK.format(link_id=link_id),
                "create_at": raw.get("create_at"),
            }
        )
        if len(posts) >= max(1, int(limit)):
            break
    return posts


def format_xhh_feed(posts: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, post in enumerate(posts, start=1):
        details: list[str] = []
        if post.get("author"):
            details.append(f"作者: {post['author']}")
        labels = [*post.get("topics", []), *post.get("tags", [])]
        if labels:
            details.append("话题: " + "、".join(labels[:5]))
        description = _plain_xhh_text(post.get("description"))
        suffix = f"（{'；'.join(details)}）" if details else ""
        line = f"{index}. {post['title']}{suffix}"
        if description:
            line += f"\n   {description[:300]}"
        lines.append(line)
    return "\n".join(lines)


def _community_feed_items(payload: Any) -> list[dict[str, Any]]:
    """Return the first list-shaped card collection from a feed response."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    containers: list[Any] = [payload]
    for key in ("data", "result", "feed"):
        value = payload.get(key)
        if isinstance(value, (dict, list)):
            containers.append(value)
    for container in containers:
        if isinstance(container, list):
            return [item for item in container if isinstance(item, dict)]
        if not isinstance(container, dict):
            continue
        for key in ("items", "posts", "cards", "results", "list"):
            value = container.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _community_text(value: Any) -> str:
    """Flatten the common text shapes used by public community feed cards."""
    if isinstance(value, str):
        return _plain_xhh_text(value)
    if isinstance(value, dict):
        for key in ("text", "content", "body", "value", "name", "display_name"):
            text = _community_text(value.get(key))
            if text:
                return text
        return ""
    if isinstance(value, list):
        return _plain_xhh_text(" ".join(_community_text(item) for item in value))
    return ""


def _community_label_values(items: Any) -> list[str]:
    values: list[str] = []
    for item in items if isinstance(items, list) else []:
        value = _community_text(item)
        if value and value not in values:
            values.append(value)
    return values


def _community_identifier(value: Any) -> str:
    """Normalize a public card identifier while retaining scalar numeric IDs."""

    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value).strip()
    return _community_text(value)


def _community_card_url(raw: dict[str, Any]) -> str:
    _, discover_url = _neko_community_urls()
    community_origin = urlparse(discover_url)
    for key in (
        "url",
        "link",
        "href",
        "permalink",
        "canonical_url",
        "post_url",
        "detail_url",
        "path",
    ):
        candidate = _community_text(raw.get(key))
        if not candidate or "\\" in candidate:
            continue
        try:
            parsed_candidate = urlparse(candidate)
        except ValueError:
            continue
        if parsed_candidate.scheme:
            if parsed_candidate.scheme.lower() not in {"http", "https"}:
                continue
            resolved_url = candidate
        else:
            resolved_url = urljoin(discover_url, candidate)
        try:
            parsed_url = urlparse(resolved_url)
        except ValueError:
            continue
        if (
            parsed_url.scheme.lower() == community_origin.scheme.lower()
            and parsed_url.netloc.casefold() == community_origin.netloc.casefold()
        ):
            return resolved_url
    # The feed API does not need to expose a post permalink for a card to stay
    # useful: the discover page is a safe, stable fallback for the source card.
    return discover_url


def normalize_neko_community_feed(
    payload: Any,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Normalize public N.E.K.O community feed cards for proactive chat."""
    posts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in _community_feed_items(payload):
        title = _community_text(
            raw.get("title") or raw.get("headline") or raw.get("subject")
        )
        content = _community_text(
            raw.get("story_md")
            or raw.get("summary")
            or raw.get("content")
            or raw.get("body")
            or raw.get("text")
            or raw.get("description")
            or raw.get("excerpt")
        )
        if not title:
            title = content[:80]
        if not title:
            continue
        author_data = (
            raw.get("author")
            or raw.get("author_name")
            or raw.get("user")
            or raw.get("creator")
        )
        author = _community_text(author_data)
        labels = _community_label_values(
            raw.get("tags") or raw.get("topics") or raw.get("categories")
        )
        url = _community_card_url(raw)
        item_id = _community_identifier(
            raw.get("id") or raw.get("post_id") or raw.get("uuid")
        )
        dedupe_key = item_id or f"{url}|{title.casefold()}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        posts.append(
            {
                "id": item_id,
                "title": title,
                "content": content,
                "author": author,
                "tags": labels,
                "url": url,
                "created_at": raw.get("created_at") or raw.get("createdAt"),
            }
        )
        if len(posts) >= max(1, int(limit)):
            break
    return posts


def format_neko_community_feed(posts: list[dict[str, Any]]) -> str:
    """Format N.E.K.O community cards as bounded, prompt-ready material."""
    lines: list[str] = []
    for index, post in enumerate(posts, start=1):
        details: list[str] = []
        if post.get("author"):
            details.append(f"作者: {post['author']}")
        if post.get("tags"):
            details.append("话题: " + "、".join(post["tags"][:5]))
        suffix = f"（{'；'.join(details)}）" if details else ""
        line = f"{index}. {post['title']}{suffix}"
        content = _plain_xhh_text(post.get("content"))
        if content and content != post["title"]:
            line += f"\n   {content[:300]}"
        lines.append(line)
    return "\n".join(lines)


async def fetch_neko_community_feed(limit: int = 10) -> dict[str, Any]:
    """Fetch the public first-page discover cards from the N.E.K.O community."""
    try:
        feed_api, discover_url = _neko_community_urls()
        response = await get_external_http_client().get(
            feed_api,
            params={"offset": 0, "limit": NEKO_COMMUNITY_FEED_PAGE_SIZE},
            headers={
                "Accept": "application/json",
                "Referer": discover_url,
                "User-Agent": XHH_USER_AGENT,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
        posts = normalize_neko_community_feed(payload, limit=limit)
        if not posts:
            raise ValueError("喵宇宙社区 feed 未返回可用卡牌")
        return {
            "success": True,
            "posts": posts,
            "formatted_content": format_neko_community_feed(posts),
        }
    except Exception as exc:
        logger.warning(f"获取喵宇宙社区内容失败: {type(exc).__name__}: {exc}")
        return {"success": False, "error": f"{type(exc).__name__}: {exc}", "posts": []}


async def fetch_xhh_feed_content(limit: int = 10) -> dict[str, Any]:
    """Fetch Xiaoheihe feed, preferring credentials and falling back to public data."""
    try:
        cookies = await asyncio.to_thread(load_cookies_from_file, "xhh")
    except Exception as exc:
        logger.warning(f"读取小黑盒凭证失败，按未登录模式继续: {exc}")
        cookies = {}

    base_headers = {
        "Referer": "https://www.xiaoheihe.cn/",
        "User-Agent": XHH_USER_AGENT,
    }
    attempts: list[tuple[str, dict[str, str]]] = []
    if cookies:
        attempts.append(("登录态", {**base_headers, "Cookie": build_xhh_cookie_header(cookies)}))
    attempts.append(("未登录", base_headers))

    last_error = "小黑盒 feeds 未返回可用帖子"
    for attempt_name, headers in attempts:
        try:
            response = await get_external_http_client().get(
                f"{XHH_API_BASE}{XHH_FEEDS_PATH}",
                params=build_xhh_request_params(XHH_FEEDS_PATH, extra={"pull": "1"}),
                headers=headers,
                timeout=10.0,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("响应不是 JSON 对象")
            status = str(payload.get("status") or payload.get("stat") or "ok").lower()
            if status not in {"ok", "success"}:
                raise ValueError(str(payload.get("msg") or payload.get("message") or status))
            posts = normalize_xhh_feed(payload, limit=limit)
            if not posts:
                raise ValueError("小黑盒 feeds 未返回可用帖子")
            return {
                "success": True,
                "posts": posts,
                "formatted_content": format_xhh_feed(posts),
                "authenticated": attempt_name == "登录态",
            }
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt_name == "登录态":
                logger.warning(f"小黑盒登录态首页获取失败，回退未登录 feed: {last_error}")
                continue
            break

    return {
        "success": False,
        "error": last_error,
        "posts": [],
    }


def _format_tieba_hot_replies(post: dict[str, Any]) -> list[str]:
    hot_replies = post.get("hot_replies")
    if not isinstance(hot_replies, list) or not hot_replies:
        return []

    output_lines = ["   热门回复："]
    for reply in hot_replies[:_TIEBA_HOT_REPLIES_PER_POST]:
        if not isinstance(reply, dict):
            continue
        text = _clean_tieba_reply_text(reply.get("text"))
        if not text:
            continue
        floor = _safe_tieba_int(reply.get("floor"))
        agree = _safe_tieba_int(reply.get("agree"))
        prefix_parts = []
        if floor:
            prefix_parts.append(f"{floor}楼")
        if agree:
            prefix_parts.append(f"{agree}赞")
        prefix = " ".join(prefix_parts) if prefix_parts else "楼层"
        output_lines.append(f"   - {prefix}：{text}")

        reactions = reply.get("reactions")
        if not isinstance(reactions, list):
            continue
        reaction_texts: list[str] = []
        for reaction in reactions[:_TIEBA_REACTIONS_PER_REPLY]:
            if not isinstance(reaction, dict):
                continue
            reaction_text = _clean_tieba_reply_text(reaction.get("text"))
            if reaction_text:
                reaction_texts.append(reaction_text)
        if reaction_texts:
            output_lines.append(f"     反应：{' / '.join(reaction_texts)}")
    return output_lines if len(output_lines) > 1 else []


def format_tieba_content(tieba_content: Dict[str, Any]) -> str:
    """Format Tieba public post candidates into prompt-ready text."""
    if not tieba_content.get("success"):
        return "\u6682\u65f6\u65e0\u6cd5\u83b7\u53d6\u8d34\u5427\u70ed\u95e8\u5e16\u5b50"
    posts = tieba_content.get("posts") or (tieba_content.get("tieba") or {}).get("posts") or []
    topics = tieba_content.get("topics") or (tieba_content.get("tieba") or {}).get("topics") or []
    if not posts and not topics:
        return "\u6682\u65f6\u6ca1\u6709\u53ef\u7528\u7684\u8d34\u5427\u5e16\u5b50"
    display_limit = _tieba_limit(tieba_content.get("display_limit") or len(posts) or len(topics) or 5)

    output_lines = ["\u3010\u8d34\u5427\u70ed\u95e8\u5e16\u5b50\uff08\u793e\u533a\u8ba8\u8bba\uff0c\u975e\u6743\u5a01\u4fe1\u606f\uff09\u3011"]
    for i, post in enumerate(posts[:display_limit], 1):
        title = str(post.get("title") or "").strip()
        if not title:
            continue
        bar_name = str(post.get("bar_name") or "").strip()
        reply_num = _safe_tieba_int(post.get("reply_num"))
        view_num = _safe_tieba_int(post.get("view_num"))
        meta_parts = []
        if bar_name:
            meta_parts.append(f"{bar_name}吧")
        if reply_num:
            meta_parts.append(f"{reply_num}回复")
        if view_num:
            meta_parts.append(f"{view_num}浏览")
        meta = f"（{'｜'.join(meta_parts)}）" if meta_parts else ""
        output_lines.append(f"{i}. {title}{meta}")
        abstract = str(post.get("abstract") or "").strip()
        if abstract:
            output_lines.append(f"   {abstract[:180]}")
        output_lines.extend(_format_tieba_hot_replies(post))
        url = str(post.get("url") or "").strip()
        if url:
            output_lines.append(f"   {url}")
    topic_limit = max(0, display_limit - min(len(posts), display_limit))
    if topics and topic_limit:
        output_lines.append("【贴吧热榜话题补充】")
        for i, topic in enumerate(topics[:topic_limit], 1):
            title = str(topic.get("title") or "").strip()
            url = str(topic.get("url") or "").strip()
            if not title or not url:
                continue
            discuss_num = _safe_tieba_int(topic.get("reply_num"))
            suffix = f"（{discuss_num}讨论）" if discuss_num else ""
            output_lines.append(f"{i}. {title}{suffix}")
            abstract = str(topic.get("abstract") or "").strip()
            if abstract:
                output_lines.append(f"   {abstract[:180]}")
            output_lines.append(f"   {url}")
    return "\n".join(output_lines)
