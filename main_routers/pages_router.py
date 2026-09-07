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
Pages Router

Handles HTML page rendering endpoints.

URL convention: routes declared WITHOUT trailing slash. The literal root
``@router.get("/")`` is the only legitimate trailing-slash route in the entire
codebase (it serves ``index.html``); the lint exempts it explicitly. Every
other page route uses ``@router.get('/voice_clone')``, ``@router.get('/api_key')``,
etc. See ``main_routers/characters_router.py`` docstring or
``.agent/rules/neko-guide.md`` (§"API URL 末尾不带斜杠") for the rationale;
enforced by ``scripts/check_api_trailing_slash.py``.
"""

import re
import time
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .shared_state import get_templates

router = APIRouter(tags=["pages"])

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TUTORIAL_RUNTIME_ASSET_PATHS = tuple(sorted(
    path
    for pattern in ("**/*.js", "**/*.json")
    for path in (_PROJECT_ROOT / "static/tutorial").glob(pattern)
))
_AVATAR_UI_BUTTON_ASSET_PATHS = tuple(sorted(
    (_PROJECT_ROOT / "static/avatar/avatar-ui-buttons").glob("*.js")
))
_CHARACTER_CARD_MANAGER_JS_PATHS = tuple(sorted(
    (_PROJECT_ROOT / "static/js/character_card_manager").glob("*.js")
))
_MODEL_MANAGER_JS_PATHS = tuple(sorted(
    (_PROJECT_ROOT / "static/js/model_manager").glob("*.js")
))
_YUI_GUIDE_DIRECTOR_JS_PATHS = tuple(sorted(
    (_PROJECT_ROOT / "static/tutorial/yui-guide/director").glob("*.js")
))
_STATIC_ASSET_VERSION_TEMPLATE_PATTERN = re.compile(
    r"/static/([^\"'\s?]+)\?v=\{\{\s*static_asset_version\b"
)


def _template_static_asset_version_paths() -> tuple[Path, ...]:
    """Collect literal static files that templates serve with the version query."""
    static_root = (_PROJECT_ROOT / "static").resolve()
    paths: set[Path] = set()
    for template_path in (_PROJECT_ROOT / "templates").glob("*.html"):
        try:
            template_source = template_path.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in _STATIC_ASSET_VERSION_TEMPLATE_PATTERN.finditer(template_source):
            asset_path = (static_root / urllib.parse.unquote(match.group(1))).resolve()
            if asset_path.is_relative_to(static_root) and asset_path.is_file():
                paths.add(asset_path)
    return tuple(sorted(paths))


_TEMPLATE_STATIC_ASSET_VERSION_PATHS = _template_static_asset_version_paths()
_YUI_GUIDE_ASSET_VERSION_PATHS = (
    _PROJECT_ROOT / "static/css/yui-guide.css",
    _PROJECT_ROOT / "static/css/tutorial-styles.css",
    _PROJECT_ROOT / "static/libs/driver.min.css",
    _PROJECT_ROOT / "static/libs/driver.min.js",
    _PROJECT_ROOT / "static/css/index.css",
    _PROJECT_ROOT / "static/css/window_controls.css",
    _PROJECT_ROOT / "static/js/window_controls.js",
    _PROJECT_ROOT / "static/tutorial/yui-guide/days/day1-home-guide.js",
    _PROJECT_ROOT / "static/tutorial/yui-guide/days/day2-screen-voice-guide.js",
    _PROJECT_ROOT / "static/tutorial/yui-guide/days/day3-interaction-guide.js",
    _PROJECT_ROOT / "static/tutorial/yui-guide/days/day4-companion-guide.js",
    _PROJECT_ROOT / "static/tutorial/yui-guide/days/day5-personalization-guide.js",
    _PROJECT_ROOT / "static/tutorial/yui-guide/days/day6-agent-guide.js",
    _PROJECT_ROOT / "static/tutorial/yui-guide/days/day7-graduation-guide.js",
    _PROJECT_ROOT / "static/tutorial/yui-guide/steps.js",
    _PROJECT_ROOT / "static/tutorial/avatar/yui-standin.js",
    _PROJECT_ROOT / "static/tutorial/yui-guide/overlay.js",
    _PROJECT_ROOT / "static/tutorial/yui-guide/page-handoff.js",
    *_YUI_GUIDE_DIRECTOR_JS_PATHS,
    _PROJECT_ROOT / "static/tutorial/avatar/yui-stage.js",
    _PROJECT_ROOT / "static/tutorial/avatar/standin-controller.js",
    _PROJECT_ROOT / "static/tutorial/core/interaction-takeover.js",
    _PROJECT_ROOT / "static/tutorial/core/seven-day-state.js",
    _PROJECT_ROOT / "static/tutorial/core/avatar-floating-boot-predictor.js",
    _PROJECT_ROOT / "static/tutorial/core/skip-controller.js",
    _PROJECT_ROOT / "static/tutorial/avatar/reload-controller.js",
    _PROJECT_ROOT / "static/tutorial/core/round-prelude-controller.js",
    _PROJECT_ROOT / "static/tutorial/core/universal-manager.js",
    _PROJECT_ROOT / "static/avatar/avatar-performance-stage.js",
    _PROJECT_ROOT / "static/live2d/live2d-interaction.js",
    _PROJECT_ROOT / "static/live2d/live2d-init.js",
    _PROJECT_ROOT / "static/live2d/live2d-ui-buttons.js",
    *sorted(_PROJECT_ROOT.glob("static/vrm/*.js")),
    _PROJECT_ROOT / "static/mmd/mmd-ui-buttons.js",
    _PROJECT_ROOT / "static/mmd/mmd-init.js",
    _PROJECT_ROOT / "static/pngtuber-core.js",
    _PROJECT_ROOT / "static/social-embed.js",
    _PROJECT_ROOT / "static/i18n-i18next.js",
    _PROJECT_ROOT / "static/app/app-auto-goodbye.js",
    _PROJECT_ROOT / "static/app/app-widget-mode.js",
    _PROJECT_ROOT / "static/app/app-widget-interaction.js",
    _PROJECT_ROOT / "static/app/app-cat-mind.js",
    _PROJECT_ROOT / "static/app/app-cat-mind-debug.js",
    *_PROJECT_ROOT.glob("static/app/app-interpage/*.js"),
    *_PROJECT_ROOT.glob("static/app/app-ui/*.js"),
    _PROJECT_ROOT / "static/common_ui.js",
    _PROJECT_ROOT / "static/common-ui-hud.js",
    _PROJECT_ROOT / "static/jukebox/music_ui.js",
    _PROJECT_ROOT / "static/css/music_ui.css",
    _PROJECT_ROOT / "static/assets/music/music-cover-placeholder.png",
    _PROJECT_ROOT / "static/game/games/soccer/soccer-demo.css",
    _PROJECT_ROOT / "static/game/games/soccer/soccer-demo.js",
    *_PROJECT_ROOT.glob("static/app/app-react-chat-window/*.js"),
    _PROJECT_ROOT / "static/app/app-chat-export.js",
    *_AVATAR_UI_BUTTON_ASSET_PATHS,
    _PROJECT_ROOT / "static/subtitle/subtitle.js",
    _PROJECT_ROOT / "static/assets/neko-idle/cat-idle-cat1.gif",
    _PROJECT_ROOT / "static/assets/neko-idle/cat-idle-cat1-click.gif",
    _PROJECT_ROOT / "static/assets/neko-idle/cat-idle-cat1-eat.gif",
    _PROJECT_ROOT / "static/assets/neko-idle/cat-idle-cat-play-1.gif",
    _PROJECT_ROOT / "static/assets/neko-idle/cat-idle-cat2.gif",
    _PROJECT_ROOT / "static/assets/neko-idle/cat-idle-cat2-click.gif",
    _PROJECT_ROOT / "static/assets/neko-idle/cat-idle-cat3.gif",
    _PROJECT_ROOT / "static/assets/neko-idle/cat-idle-cat3-click.gif",
    _PROJECT_ROOT / "static/assets/neko-idle/cat-idle-cat4-1.gif",
    _PROJECT_ROOT / "static/assets/neko-idle/cat-idle-cat4-2.gif",
    _PROJECT_ROOT / "static/assets/neko-idle/cat-idle-cat4-3.gif",
    _PROJECT_ROOT / "static/assets/neko-idle/cat-idle-cat-move-1.gif",
    _PROJECT_ROOT / "static/assets/neko-idle/cat-idle-cat-move-2.gif",
    _PROJECT_ROOT / "static/assets/neko-idle/cat-idle-cat-move-3.gif",
    _PROJECT_ROOT / "static/assets/neko-idle/cat-idle-cat-move-4.gif",
    _PROJECT_ROOT / "static/assets/neko-idle/cat-idle-cat-move-5.gif",
    _PROJECT_ROOT / "static/assets/neko-idle/cat1-question-mark.png",
    _PROJECT_ROOT / "static/assets/neko-idle/cat_model_change.gif",
    _PROJECT_ROOT / "static/assets/neko-idle/chat-minimized-yarn-ball.png",
    _PROJECT_ROOT / "static/assets/neko-idle/thought-items/cloud-thought-bubble.gif",
    _PROJECT_ROOT / "static/assets/neko-idle/thought-items/cloud-thought-bubble-pop.gif",
    _PROJECT_ROOT / "static/assets/neko-idle/thought-items/sleeping-zzz.gif",
    _PROJECT_ROOT / "static/assets/neko-idle/thought-items/cat1-chat-angry.gif",
    _PROJECT_ROOT / "static/assets/neko-idle/thought-items/catnip-pouch.png",
    _PROJECT_ROOT / "static/assets/neko-idle/thought-items/fish-cookie.png",
    _PROJECT_ROOT / "static/assets/neko-idle/thought-items/toy-mouse.png",
    _PROJECT_ROOT / "static/assets/neko-idle/cat1-voice-click.mp3",
    _PROJECT_ROOT / "static/assets/neko-idle/cat1-voice1.mp3",
    _PROJECT_ROOT / "static/assets/neko-idle/cat1-voice2.mp3",
    _PROJECT_ROOT / "static/assets/neko-idle/cat1-voice3.mp3",
    _PROJECT_ROOT / "static/assets/neko-idle/cat1-voice-eat.mp3",
    _PROJECT_ROOT / "static/assets/neko-idle/cat1-voice-funny.mp3",
    _PROJECT_ROOT / "static/assets/neko-idle/cat1-voice-chat-angry.mp3",
    _PROJECT_ROOT / "static/assets/neko-idle/cat2-sleep1.mp3",
    _PROJECT_ROOT / "static/assets/neko-idle/cat2-sleep2.mp3",
    _PROJECT_ROOT / "static/assets/neko-idle/cat3-sleep1.mp3",
    _PROJECT_ROOT / "static/assets/neko-idle/cat3-sleep2.mp3",
    _PROJECT_ROOT / "static/css/character_card_manager.css",
    *_CHARACTER_CARD_MANAGER_JS_PATHS,
    _PROJECT_ROOT / "static/css/character_personality_onboarding.css",
    _PROJECT_ROOT / "static/js/character_personality_onboarding.js",
    _PROJECT_ROOT / "static/css/card_maker.css",
    _PROJECT_ROOT / "static/js/card_maker_embed_layout.js",
    _PROJECT_ROOT / "static/js/card_maker.js",
    _PROJECT_ROOT / "static/js/card_maker_embed_bootstrap.js",
    _PROJECT_ROOT / "static/libs/live2dcubismcore.min.js",
    _PROJECT_ROOT / "static/libs/live2d.min.js",
    _PROJECT_ROOT / "static/libs/pixi.min.js",
    _PROJECT_ROOT / "static/libs/index.min.js",
    _PROJECT_ROOT / "static/live2d/live2d-core.js",
    _PROJECT_ROOT / "static/live2d/live2d-emotion.js",
    _PROJECT_ROOT / "static/live2d/live2d-model.js",
    _PROJECT_ROOT / "static/js/voice_clone.js",
    _PROJECT_ROOT / "static/js/voice_identity.js",
    _PROJECT_ROOT / "static/css/voice_identity.css",
    _PROJECT_ROOT / "static/css/model_manager.css",
    *_MODEL_MANAGER_JS_PATHS,
    _PROJECT_ROOT / "static/vrm/motion/player.js",
    *_TUTORIAL_RUNTIME_ASSET_PATHS,
    *_TEMPLATE_STATIC_ASSET_VERSION_PATHS,
)
_STATIC_ASSET_CACHE_TTL = 30.0
_static_asset_version_cache: tuple[float, str] = (0.0, "0")
_REACT_CHAT_ASSET_VERSION_PATHS = (
    _PROJECT_ROOT / "static/react/neko-chat/neko-chat-window.css",
    _PROJECT_ROOT / "static/react/neko-chat/neko-chat-window.iife.js",
    *_PROJECT_ROOT.glob("static/app/app-react-chat-window/*.js"),
    _PROJECT_ROOT / "static/app/app-chat-adapter.js",
    _PROJECT_ROOT / "static/app/app-buttons.js",
    _PROJECT_ROOT / "static/assets/neko-idle/thought-items/cat1-chat-angry.gif",
    *sorted(_PROJECT_ROOT.glob("static/assets/avatar-tools/**/*.png")),
    *sorted(_PROJECT_ROOT.glob("static/sounds/avatar-tools/**/*.mp3")),
)
_REACT_CHAT_ASSET_CACHE_TTL = 30.0
_react_chat_asset_version_cache: tuple[float, str] = (0.0, "0")


def _vrm_defaults_ctx() -> dict:
    """Return VRM lighting defaults for Jinja2 templates to inject into <script>."""
    from config import DEFAULT_VRM_LIGHTING
    return {"vrm_defaults": dict(DEFAULT_VRM_LIGHTING)}


def _static_assets_ctx() -> dict:
    """Return the unified cache version for template static assets."""
    from config import APP_VERSION

    global _static_asset_version_cache
    now = time.monotonic()
    cached_at, cached_version = _static_asset_version_cache
    if now - cached_at < _STATIC_ASSET_CACHE_TTL:
        return {"static_asset_version": cached_version}

    latest_mtime = 0
    for path in _YUI_GUIDE_ASSET_VERSION_PATHS:
        try:
            latest_mtime = max(latest_mtime, int(path.stat().st_mtime))
        except OSError:
            continue

    version = f"{APP_VERSION}-{latest_mtime or 0}"
    _static_asset_version_cache = (now, version)
    return {"static_asset_version": version}


def _react_chat_assets_ctx() -> dict:
    """Return the unified cache version for React Chat static assets."""
    global _react_chat_asset_version_cache
    now = time.monotonic()
    cached_at, cached_version = _react_chat_asset_version_cache
    if now - cached_at < _REACT_CHAT_ASSET_CACHE_TTL:
        return {"react_chat_asset_version": cached_version}

    latest_mtime = 0
    for path in _REACT_CHAT_ASSET_VERSION_PATHS:
        try:
            latest_mtime = max(latest_mtime, int(path.stat().st_mtime))
        except OSError:
            continue

    version = str(latest_mtime or 0)
    _react_chat_asset_version_cache = (now, version)
    return {"react_chat_asset_version": version}


@router.get("/", response_class=HTMLResponse)
async def get_default_index(request: Request):
    templates = get_templates()
    return templates.TemplateResponse("templates/index.html", {
        "request": request,
        **_vrm_defaults_ctx(),
        **_static_assets_ctx(),
        **_react_chat_assets_ctx(),
    })


def _render_model_manager(request: Request):
    """Internal implementation for rendering the model manager page."""
    templates = get_templates()
    return templates.TemplateResponse("templates/model_manager.html", {
        "request": request,
        **_vrm_defaults_ctx(),
        **_static_assets_ctx(),
    })


@router.get("/l2d", response_class=HTMLResponse)
async def get_l2d_manager(request: Request):
    """Render the model manager page (legacy route compatibility)."""
    return _render_model_manager(request)


@router.get("/model_manager", response_class=HTMLResponse)
async def get_model_manager(request: Request):
    """Render the model manager page."""
    return _render_model_manager(request)


@router.get("/live2d_parameter_editor", response_class=HTMLResponse)
async def live2d_parameter_editor(request: Request):
    """Live2D parameter editor page."""
    templates = get_templates()
    return templates.TemplateResponse("templates/live2d_parameter_editor.html", {
        "request": request,
        **_static_assets_ctx(),
    })


@router.get("/soccer_demo", response_class=HTMLResponse)
async def soccer_demo(request: Request):
    """Soccer MVP demo (VRM + L2D avatars)"""
    templates = get_templates()
    return templates.TemplateResponse("templates/soccer_demo.html", {
        "request": request,
        **_vrm_defaults_ctx(),
        **_static_assets_ctx(),
    })


@router.get("/badminton_demo", response_class=HTMLResponse)
async def badminton_demo(request: Request):
    """Badminton challenge mini-game."""
    templates = get_templates()
    return templates.TemplateResponse("templates/badminton_demo.html", {
        "request": request,
        **_static_assets_ctx(),
    })


@router.get("/live2d_emotion_manager", response_class=HTMLResponse)
async def live2d_emotion_manager(request: Request):
    """Live2D emotion mapping manager page."""
    templates = get_templates()
    return templates.TemplateResponse("templates/live2d_emotion_manager.html", {
        "request": request,
        **_static_assets_ctx(),
    })


@router.get("/vrm_emotion_manager", response_class=HTMLResponse)
async def vrm_emotion_manager(request: Request):
    """VRM emotion mapping manager page."""
    templates = get_templates()
    return templates.TemplateResponse("templates/vrm_emotion_manager.html", {
        "request": request,
        **_static_assets_ctx(),
    })


@router.get("/mmd_emotion_manager", response_class=HTMLResponse)
async def mmd_emotion_manager(request: Request):
    """MMD emotion mapping manager page."""
    templates = get_templates()
    return templates.TemplateResponse("templates/mmd_emotion_manager.html", {
        "request": request,
        **_static_assets_ctx(),
    })


@router.get('/voice_clone', response_class=HTMLResponse)
async def voice_clone_page(request: Request):
    templates = get_templates()
    return templates.TemplateResponse("templates/voice_clone.html", {
        "request": request,
        **_static_assets_ctx(),
    })


@router.get("/api_key", response_class=HTMLResponse)
async def api_key_settings(request: Request):
    """API key settings page."""
    templates = get_templates()
    return templates.TemplateResponse("templates/api_key_settings.html", {
        "request": request,
        **_static_assets_ctx(),
    })


@router.get("/voice_identity", response_class=HTMLResponse)
async def voice_identity_settings(request: Request):
    """Owner voice identity enrollment page."""
    templates = get_templates()
    return templates.TemplateResponse("templates/voice_identity.html", {
        "request": request,
        **_static_assets_ctx(),
    })


@router.get('/chara_manager')
async def chara_manager_redirect(request: Request):
    url = "/character_card_manager"
    if request.query_params:
        url += "?" + str(request.query_params)
    return RedirectResponse(url=url, status_code=307)


@router.get('/character_card_manager', response_class=HTMLResponse)
async def character_card_manager_page(request: Request, lanlan_name: str = ""):
    templates = get_templates()
    return templates.TemplateResponse("templates/character_card_manager.html", {
        "request": request,
        "lanlan_name": lanlan_name,
        **_static_assets_ctx(),
    })


@router.get('/cloudsave_manager', response_class=HTMLResponse)
async def cloudsave_manager_page(request: Request, lanlan_name: str = ""):
    templates = get_templates()
    return templates.TemplateResponse("templates/cloudsave_manager.html", {
        "request": request,
        "lanlan_name": lanlan_name,
        **_static_assets_ctx(),
    })


@router.get('/memory_browser', response_class=HTMLResponse)
async def memory_browser(request: Request):
    templates = get_templates()
    return templates.TemplateResponse('templates/memory_browser.html', {
        "request": request,
        **_static_assets_ctx(),
    })


@router.get('/cookies_login', response_class=HTMLResponse)
async def cookies_login_page(request: Request):
    """Media credential acquisition page."""
    templates = get_templates()
    return templates.TemplateResponse('templates/cookies_login.html', {
        "request": request,
        **_static_assets_ctx(),
    })



@router.get("/chat", response_class=HTMLResponse)
async def get_chat_page(request: Request):
    """Standalone chat window page."""
    templates = get_templates()
    return templates.TemplateResponse("templates/chat.html", {
        "request": request,
        "initial_chat_surface_mode": "compact",
        "initial_chat_host_kind": "compact",
        **_vrm_defaults_ctx(),
        **_static_assets_ctx(),
        **_react_chat_assets_ctx(),
    })


@router.get("/chat_full", response_class=HTMLResponse)
async def get_chat_full_page(request: Request):
    """Web-only full chat window page."""
    templates = get_templates()
    return templates.TemplateResponse("templates/chat.html", {
        "request": request,
        "initial_chat_surface_mode": "full",
        "initial_chat_host_kind": "full",
        **_vrm_defaults_ctx(),
        **_static_assets_ctx(),
        **_react_chat_assets_ctx(),
    })


@router.get("/web_chat_compact", response_class=HTMLResponse)
async def get_web_chat_compact_page(request: Request):
    """Open the home page with React Chat initialized in compact mode."""
    templates = get_templates()
    return templates.TemplateResponse("templates/index.html", {
        "request": request,
        "initial_chat_surface_mode": "compact",
        **_vrm_defaults_ctx(),
        **_static_assets_ctx(),
        **_react_chat_assets_ctx(),
    })


@router.get("/subtitle", response_class=HTMLResponse)
async def get_subtitle_page(request: Request):
    """Standalone subtitle window page."""
    templates = get_templates()
    return templates.TemplateResponse("templates/subtitle.html", {"request": request})


@router.get("/agenthud", response_class=HTMLResponse)
async def get_agenthud_page(request: Request):
    """Standalone AgentHUD window page."""
    templates = get_templates()
    return templates.TemplateResponse("templates/agenthud.html", {"request": request})


@router.get("/card_maker", response_class=HTMLResponse)
async def get_card_maker_page(request: Request):
    """Card-face maker page (loads the model standalone with adjustable composition)."""
    templates = get_templates()
    return templates.TemplateResponse("templates/card_maker.html", {
        "request": request,
        **_vrm_defaults_ctx(),
        **_static_assets_ctx(),
    })


@router.get("/jukebox", response_class=HTMLResponse)
async def get_jukebox_page(request: Request):
    """Standalone jukebox window page (loaded by Electron)."""
    templates = get_templates()
    return templates.TemplateResponse("templates/jukebox.html", {
        "request": request,
        **_static_assets_ctx(),
    })


@router.get("/jukebox/manager", response_class=HTMLResponse)
async def get_jukebox_manager_page(request: Request):
    """Standalone jukebox manager window page (opened from the jukebox)."""
    templates = get_templates()
    return templates.TemplateResponse("templates/jukebox_manager.html", {
        "request": request,
        **_static_assets_ctx(),
    })


@router.get("/toast", response_class=HTMLResponse)
async def get_toast_page(request: Request):
    """Standalone toast notification window page (loaded by Electron)."""
    templates = get_templates()
    return templates.TemplateResponse("templates/toast.html", {"request": request})



@router.get("/{lanlan_name}", response_class=HTMLResponse)
async def get_index(request: Request, lanlan_name: str):
    # lanlan_name 将从 URL 中提取，前端会通过 API 获取配置
    templates = get_templates()
    return templates.TemplateResponse("templates/index.html", {
        "request": request,
        **_vrm_defaults_ctx(),
        **_static_assets_ctx(),
        **_react_chat_assets_ctx(),
    })
