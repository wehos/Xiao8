from pathlib import Path

import pytest
from playwright.sync_api import Page


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INTERPAGE_SCRIPT = (
    PROJECT_ROOT
    / "static"
    / "app"
    / "app-interpage"
    / "bootstrap-resources-and-model-reload.js"
)


def _set_up_mmd_reload_page(page: Page):
    page.set_content(
        """
        <div id="live2d-container"><canvas id="live2d-canvas"></canvas></div>
        <div id="vrm-container"><canvas id="vrm-canvas"></canvas></div>
        <div id="mmd-container"><canvas id="mmd-canvas"></canvas></div>
        """
    )
    page.evaluate(
        """
        () => {
            window.appState = {};
            window.lanlan_config = {
                lanlan_name: 'TestMMD',
                model_type: 'live3d',
                live3d_sub_type: 'mmd'
            };
            window.showStatusToast = () => {};
            window.__overlayEnded = false;
            window.MMDLoadingOverlay = {
                begin() {},
                update() {},
                end() { window.__overlayEnded = true; },
                fail() {}
            };
            window._createMMDLoadingSessionId = () => 'mmd-visibility-race';
            window._waitForMMDRenderFrame = async () => {};

            window.mmdManager = {
                _shouldRender: true,
                currentModel: null,
                enablePhysics: true,
                pauseRendering() { this._shouldRender = false; },
                resumeRendering() { this._shouldRender = true; },
                loadModel() {
                    return new Promise((resolve) => {
                        window.__finishMmdLoad = () => {
                            this.currentModel = { mesh: { visible: true } };
                            resolve({ name: 'Loaded MMD' });
                        };
                    });
                },
                applySettings() {}
            };

            window.fetch = async (url) => {
                if (String(url).includes('/api/config/page_config')) {
                    return {
                        json: async () => ({
                            success: true,
                            model_path: '/static/mmd/Miku/Miku.pmx',
                            model_type: 'live3d',
                            live3d_sub_type: 'mmd'
                        })
                    };
                }
                if (String(url).includes('/mmd_settings')) {
                    return { json: async () => ({ success: true, settings: {} }) };
                }
                if (String(url) === '/api/characters') {
                    return { ok: true, json: async () => ({ '猫娘': { TestMMD: {} } }) };
                }
                throw new Error(`Unexpected request: ${url}`);
            };
        }
        """
    )
    page.add_script_tag(path=str(INTERPAGE_SCRIPT))


@pytest.mark.frontend
def test_mmd_hot_reload_survives_hide_show_during_model_loading(page: Page):
    _set_up_mmd_reload_page(page)

    result = page.evaluate(
        """
        async () => {
            const interpage = window.__appInterpageParts;

            // The manager page already owns visibility before the reload starts.
            interpage.handleHideMainUI();
            const reloadPromise = interpage.handleModelReload('TestMMD');
            while (typeof window.__finishMmdLoad !== 'function') {
                await new Promise((resolve) => setTimeout(resolve, 0));
            }

            // Reproduce the observed lifecycle pulse while loadModel is pending.
            interpage.handleHideMainUI();
            const canvasDuringLoad = document.getElementById('mmd-canvas');
            const sessionAfterSecondHide = canvasDuringLoad.dataset.mmdLoadingSessionId || '';
            interpage.handleShowMainUI();

            window.__finishMmdLoad();
            await reloadPromise;

            const container = document.getElementById('mmd-container');
            const canvas = document.getElementById('mmd-canvas');
            return {
                mainUIHiddenByModelManager: document.body.classList.contains(
                    'neko-main-ui-hidden-by-model-manager'
                ),
                sessionAfterSecondHide,
                overlayEnded: window.__overlayEnded,
                reloadSucceeded: window._lastModelReloadResult,
                rendering: window.mmdManager._shouldRender,
                containerHidden: container.classList.contains('hidden'),
                containerDisplay: getComputedStyle(container).display,
                canvasVisibility: canvas.style.visibility,
                canvasPointerEvents: canvas.style.pointerEvents,
                finalCanvasSession: canvas.dataset.mmdLoadingSessionId || ''
            };
        }
        """
    )

    assert result == {
        "mainUIHiddenByModelManager": False,
        "sessionAfterSecondHide": "mmd-visibility-race",
        "overlayEnded": True,
        "reloadSucceeded": True,
        "rendering": True,
        "containerHidden": False,
        "containerDisplay": "block",
        "canvasVisibility": "visible",
        "canvasPointerEvents": "auto",
        "finalCanvasSession": "",
    }


@pytest.mark.frontend
def test_failed_mmd_hot_reload_clears_session_before_deferred_show(page: Page):
    _set_up_mmd_reload_page(page)

    result = page.evaluate(
        """
        async () => {
            const interpage = window.__appInterpageParts;
            window.__overlayFailed = false;
            window.MMDLoadingOverlay.fail = () => { window.__overlayFailed = true; };
            window.mmdManager.loadModel = () => new Promise((resolve, reject) => {
                window.__failMmdLoad = () => reject(new Error('expected MMD load failure'));
            });

            interpage.handleHideMainUI();
            const reloadPromise = interpage.handleModelReload('TestMMD');
            while (typeof window.__failMmdLoad !== 'function') {
                await new Promise((resolve) => setTimeout(resolve, 0));
            }

            interpage.handleHideMainUI();
            interpage.handleShowMainUI();
            window.__failMmdLoad();
            await reloadPromise;

            const container = document.getElementById('mmd-container');
            const canvas = document.getElementById('mmd-canvas');
            return {
                overlayFailed: window.__overlayFailed,
                reloadSucceeded: window._lastModelReloadResult,
                mainUIHiddenByModelManager: document.body.classList.contains(
                    'neko-main-ui-hidden-by-model-manager'
                ),
                containerDisplay: getComputedStyle(container).display,
                canvasVisibility: canvas.style.visibility,
                canvasPointerEvents: canvas.style.pointerEvents,
                finalCanvasSession: canvas.dataset.mmdLoadingSessionId || ''
            };
        }
        """
    )

    assert result == {
        "overlayFailed": True,
        "reloadSucceeded": False,
        "mainUIHiddenByModelManager": False,
        "containerDisplay": "block",
        "canvasVisibility": "visible",
        "canvasPointerEvents": "auto",
        "finalCanvasSession": "",
    }


@pytest.mark.frontend
def test_non_mmd_reload_does_not_preserve_stale_mmd_loading_session(page: Page):
    page.set_content(
        """
        <div id="live2d-container"><canvas id="live2d-canvas"></canvas></div>
        <div id="vrm-container"><canvas id="vrm-canvas"></canvas></div>
        <div id="mmd-container"><canvas id="mmd-canvas"></canvas></div>
        """
    )
    page.evaluate(
        """
        () => {
            window.appState = {};
            window.lanlan_config = { model_type: 'pngtuber' };
        }
        """
    )
    page.add_script_tag(path=str(INTERPAGE_SCRIPT))

    result = page.evaluate(
        """
        () => {
            const canvas = document.getElementById('mmd-canvas');
            canvas.dataset.mmdLoadingSessionId = 'stale-mmd-session';
            window._modelReloadInFlight = true;

            window.__appInterpageParts.handleHideMainUI();

            return {
                finalCanvasSession: canvas.dataset.mmdLoadingSessionId || '',
                canvasVisibility: canvas.style.visibility,
                canvasPointerEvents: canvas.style.pointerEvents
            };
        }
        """
    )

    assert result == {
        "finalCanvasSession": "",
        "canvasVisibility": "hidden",
        "canvasPointerEvents": "none",
    }
