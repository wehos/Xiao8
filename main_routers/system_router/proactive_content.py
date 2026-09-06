"""Compatibility re-exports for proactive content logging helpers."""

from main_logic.proactive_chat.content_logging import (
    _log_neko_community_content,
    _log_news_content,
    _log_personal_dynamics,
    _log_trending_content,
    _log_video_content,
    _tieba_log_title,
)
from main_logic.proactive_chat.music_recommendation import (
    _append_music_recommendations,
    _format_music_content,
    _log_music_content,
)

__all__ = [
    "_append_music_recommendations",
    "_format_music_content",
    "_log_music_content",
    "_log_neko_community_content",
    "_log_news_content",
    "_log_personal_dynamics",
    "_log_trending_content",
    "_log_video_content",
    "_tieba_log_title",
]
