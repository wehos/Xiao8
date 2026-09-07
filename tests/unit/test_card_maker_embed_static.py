import re
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

from main_routers import pages_router
from tests.fake_clock import patch_module_clock


ROOT = Path(__file__).resolve().parents[2]


def render_card_maker(mode: str) -> str:
    environment = Environment(loader=FileSystemLoader(ROOT))
    template = environment.get_template("templates/card_maker.html")
    return template.render(
        request=SimpleNamespace(query_params={"mode": mode}),
        vrm_defaults={},
        static_asset_version="test-version",
    )


def test_card_maker_exposes_transparent_model_embed_mode() -> None:
    template = (ROOT / "templates" / "card_maker.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "css" / "card_maker.css").read_text(encoding="utf-8")
    script = (ROOT / "static" / "js" / "card_maker.js").read_text(encoding="utf-8")
    layout = (ROOT / "static" / "js" / "card_maker_embed_layout.js").read_text(encoding="utf-8")
    bootstrap = (ROOT / "static" / "js" / "card_maker_embed_bootstrap.js").read_text(encoding="utf-8")
    model_runtime = (ROOT / "static" / "live2d" / "live2d-model.js").read_text(encoding="utf-8")

    assert "document.documentElement.classList.add('card-maker-embed')" in template
    assert "background: transparent !important;" in css
    assert "html.card-maker-embed #model-viewport" in css
    assert "html.card-maker-embed #card-edit-area" in css
    assert "const isEmbedMode = _urlParams.get('mode') === 'embed';" in script
    assert "type: 'neko-card-maker-embed'" in script
    assert "character: currentCharaName" not in script
    assert "modelType: currentModelType" not in script
    assert "if (isEmbedMode) {" in script
    assert "requestedEmbedScale" not in script
    assert "requestedEmbedOffsetY" not in script
    assert "if (!isEmbedMode) {\n                startPreviewLoop();\n                refreshPreview();\n            }\n            notifyEmbedHost('ready');" in script
    assert "notifyEmbedHost('ready')" in script
    assert "notifyEmbedHost('error'" in script
    config_error_handler = script.split(
        "console.error('[CardExport] 加载角色模型失败:', e);", 1
    )[1].split("}", 1)[0]
    assert "notifyEmbedHost('error');" in config_error_handler
    assert "if (!isEmbedMode) {\n                startPreviewLoop();\n                refreshPreview();" in script
    assert "const HEIGHT_RATIO = 1.25;" in layout
    assert "const CENTER_X_RATIO = 0.25;" in layout
    assert "const CENTER_Y_RATIO = 0.66;" in layout
    assert "const MAX_WIDTH_RATIO = CENTER_X_RATIO * 2;" in layout
    assert "window.NEKOCardMakerEmbedLayout" in script
    assert "card_maker_embed_layout.js?v={{ static_asset_version" in template
    assert "const EMBED_MODEL_HEIGHT_RATIO = 1.34;" in script
    assert "const EMBED_MODEL_CENTER_X_RATIO = 0.22;" in script
    assert "const EMBED_MODEL_CENTER_Y_RATIO = 0.67;" in script
    assert "frameLive2DModelForEmbed(window.live2dManager);" in script
    assert "frameVRMModelForEmbed(window.vrmManager);" in script
    assert "frameMMDModelForEmbed(window.mmdManager);" in script
    assert "framePNGTuberForEmbed(mgr);" in script
    assert "isEmbedMode ? Math.max(1, window.innerWidth) : CARD_BASE_WIDTH" in script
    assert "window.addEventListener('resize'" in script
    assert "function syncEmbedModelViewport()" in script
    assert "resizeModelRendererForCard(currentModelType, activeModelSourceScale);" in script
    assert "window.__NEKO_CARD_MAKER_EMBED__ = {{ card_maker_embed | tojson }};" in template
    assert "card_maker_embed_bootstrap.js?v={{ static_asset_version" in template
    assert "window.__NEKO_CARD_MAKER_CONFIG_PROMISE__" in bootstrap
    assert "await loaders[effectiveModelType(config)]();" in bootstrap
    assert "minimalEmbed: true" in script
    assert "preserveDrawingBuffer: !isEmbedMode" in script
    assert "loadEmotionMapping: false" in script
    assert "const minimalEmbed = options.minimalEmbed === true;" in model_runtime
    assert "minimalEmbed ? 480 : 2000" in model_runtime


def test_card_maker_embed_fixes_vrm_mmd_and_pngtuber_without_scanning_live2d() -> None:
    script = (ROOT / "static" / "js" / "card_maker.js").read_text(encoding="utf-8")
    vrm_core = (ROOT / "static" / "vrm" / "vrm-core.js").read_text(encoding="utf-8")
    mmd_core = (ROOT / "static" / "mmd" / "mmd-core.js").read_text(encoding="utf-8")

    assert "function getLive2DVisibleBoundsForEmbed" not in script
    assert "mgr._getDrawableDirectScreenRect(index, true)" not in script
    assert "function frameVRMModelForEmbed" in script
    assert "function frameMMDModelForEmbed" in script
    assert "const box = new THREE.Box3().setFromObject(model);" in script
    assert "embedThreeFrameCache.get(model)" in script
    assert "embedThreeFrameCache.set(model, bounds)" in script
    assert "embed: isEmbedMode,\n            addShadow: !isEmbedMode" in script
    assert "if (!embed) {" in vrm_core
    assert "if (!embed && !hasSavedRotation" in vrm_core
    assert "window.mmdManager.loadModel(modelPath, { embed: isEmbedMode })" in script
    assert "await loadMMDIdlePoseForEmbed(window.mmdManager);" in script
    assert "mgr.pauseAnimation?.();" in script
    assert "const embed = options.embed === true;" in mmd_core
    assert "if (!embed) {\n                await this._restoreUserPreferences(mmd, modelUrl);" in mmd_core
    assert "window.mmdManager.core?.renderer" not in script
    assert "if (!window.mmdManager.renderer)" in script

    for field in (
        "offset_x: 0",
        "offset_y: 0",
        "mobile_offset_x: 0",
        "mobile_offset_y: 0",
        "position_anchor: 'center'",
    ):
        assert field in script
    assert "mgr.applyTransform?.();" in script


def test_card_maker_embed_runtime_loaders_are_provider_symmetric() -> None:
    bootstrap = (ROOT / "static" / "js" / "card_maker_embed_bootstrap.js").read_text(encoding="utf-8")

    for provider in ("live2d", "vrm", "mmd", "pngtuber"):
        assert f"{provider}: load" in bootstrap
    assert "loadLive2DRuntime" in bootstrap
    assert "loadVRMRuntime" in bootstrap
    assert "loadMMDRuntime" in bootstrap
    assert "loadPNGTuberRuntime" in bootstrap


def test_embed_template_omits_full_editor_runtimes() -> None:
    embedded = render_card_maker("embed")
    full_editor = render_card_maker("maker")

    assert "/static/js/card_maker_embed_bootstrap.js?v=test-version" in embedded
    assert "/static/js/card_maker_embed_layout.js?v=test-version" in embedded
    assert "/static/libs/live2dcubismcore.min.js" not in embedded
    assert "/static/vrm/vrm-init.js" not in embedded
    assert "/static/mmd/mmd-init.js" not in embedded
    assert "/static/i18n-i18next.js" not in embedded
    assert "/static/js/card_maker_embed_bootstrap.js" not in full_editor
    assert "/static/js/card_maker_embed_layout.js?v=test-version" in full_editor
    assert "/static/libs/live2dcubismcore.min.js" in full_editor
    assert "/static/vrm/vrm-init.js" in full_editor
    assert "/static/mmd/mmd-init.js" in full_editor


def test_versioned_embed_assets_use_immutable_cache_headers() -> None:
    web_app = (ROOT / "app" / "main_server" / "web_app.py").read_text(encoding="utf-8")
    pages_router = (ROOT / "main_routers" / "pages_router.py").read_text(encoding="utf-8")

    assert 'if _has_generated_asset_version(scope.get("query_string", b"")):' in web_app
    assert '"public, max-age=31536000, immutable"' in web_app
    assert 'static/js/card_maker_embed_bootstrap.js' in pages_router


def test_versioned_runtime_assets_change_static_asset_version(monkeypatch) -> None:
    runtime_paths = tuple(
        ROOT / relative_path
        for relative_path in (
            "static/libs/live2dcubismcore.min.js",
            "static/libs/live2d.min.js",
            "static/libs/pixi.min.js",
            "static/libs/index.min.js",
            "static/live2d/live2d-core.js",
            "static/live2d/live2d-emotion.js",
            "static/live2d/live2d-model.js",
            "static/js/card_maker_embed_layout.js",
            "static/mmd/mmd-init.js",
            "static/social-embed.js",
        )
    )
    tracked_paths = set(pages_router._YUI_GUIDE_ASSET_VERSION_PATHS)
    assert set(runtime_paths) <= tracked_paths

    class FakePath:
        def __init__(self, mtime: int) -> None:
            self._mtime = mtime

        def stat(self):
            return SimpleNamespace(st_mtime=self._mtime)

    patch_module_clock(monkeypatch, pages_router, monotonic=lambda: 100.0)
    for index, _runtime_path in enumerate(runtime_paths, start=1):
        paths = tuple(FakePath(0) for _ in runtime_paths)
        paths = paths[:index - 1] + (FakePath(index),) + paths[index:]
        monkeypatch.setattr(pages_router, "_static_asset_version_cache", (0.0, "0"))
        monkeypatch.setattr(pages_router, "_YUI_GUIDE_ASSET_VERSION_PATHS", paths)
        assert pages_router._static_assets_ctx()["static_asset_version"].endswith(f"-{index}")


def test_template_versioned_static_assets_are_tracked() -> None:
    referenced_paths = set()
    for template_path in (ROOT / "templates").glob("*.html"):
        template_source = template_path.read_text(encoding="utf-8")
        for match in re.finditer(r"/static/([^\"'\s?]+)\?v=\{\{\s*static_asset_version\b", template_source):
            referenced_paths.add(
                (ROOT / "static" / urllib.parse.unquote(match.group(1))).resolve()
            )

    tracked_paths = set(pages_router._YUI_GUIDE_ASSET_VERSION_PATHS)
    assert referenced_paths
    assert ROOT / "static/js/card_maker_embed_bootstrap.js" in referenced_paths
    assert ROOT / "static/js/card_maker_embed_layout.js" in referenced_paths
    assert referenced_paths <= tracked_paths
    assert ROOT / "static/app/app-chat.js" in tracked_paths
