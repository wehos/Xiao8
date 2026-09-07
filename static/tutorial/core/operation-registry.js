(function (root, factory) {
    'use strict';

    const api = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.TutorialOperationRegistry = api;
    }
})(typeof window !== 'undefined' ? window : globalThis, function () {
    'use strict';

    class OperationRegistry {
        constructor(director, options) {
            const normalizedOptions = options || {};
            this.director = director;
            this.registry = normalizedOptions.registry || null;
            this.pluginDashboardWindowName = normalizedOptions.pluginDashboardWindowName || 'plugin_dashboard';
            this.resolveGuideLocale = typeof normalizedOptions.resolveGuideLocale === 'function'
                ? normalizedOptions.resolveGuideLocale
                : () => '';
            this.operationHandlers = [];
            this.day1AvatarZoomOriginalLive2dLocked = null;
            this.registerBuiltInOperations();
        }

        normalizeOperationMatcher(matcher) {
            if (typeof matcher === 'string') {
                return (context) => context.operation === matcher;
            }
            if (typeof matcher === 'function') {
                return matcher;
            }
            if (matcher && typeof matcher.prefix === 'string') {
                return (context) => context.operation.indexOf(matcher.prefix) === 0;
            }
            if (matcher && Array.isArray(matcher.anyOf)) {
                const matchers = matcher.anyOf.map((entry) => this.normalizeOperationMatcher(entry));
                return (context) => matchers.some((matches) => matches(context));
            }
            return () => false;
        }

        registerOperation(matcher, handler) {
            if (typeof handler !== 'function') {
                return null;
            }
            const entry = {
                matches: this.normalizeOperationMatcher(matcher),
                handler: handler
            };
            this.operationHandlers.push(entry);
            return entry;
        }

        registerBuiltInOperations() {
            this.registerOperation('day1-intro-activation-flow', () => (
                this.runDay1IntroActivationFlow()
            ));
            this.registerOperation('day1-intro-greeting-flow', () => (
                this.runDay1IntroGreetingFlow()
            ));
            this.registerOperation('day1-intro-greeting-performance', (context) => (
                this.runDay1IntroGreetingPerformance(context)
            ));
            this.registerOperation('daily-intro-greeting-performance', (context) => (
                this.runDailyIntroGreetingPerformance(context.scene, context)
            ));
            this.registerOperation('daily-intro-avatar-performance', (context) => (
                this.runDailyIntroAvatarPerformance(context.scene, context)
            ));
            this.registerOperation('day1-intro-basic-voice-showcase', (context) => (
                this.runDay1IntroBasicVoiceShowcase(
                    context.scene,
                    context.narrationStartedAt,
                    context.narrationPromise
                )
            ));
            this.registerOperation('day1-screen-share-entry-flow', (context) => (
                this.runDay1ScreenShareEntryFlow(context.scene)
            ));
            this.registerOperation('day1-managed-scene:takeover_capture_cursor', (context) => (
                this.runDay1TakeoverCaptureCursor(context.scene)
            ));
            this.registerOperation({ prefix: 'day1-managed-scene-settled:' }, () => true);
            this.registerOperation('day6-plugin-open-agent-panel-flow', (context) => (
                this.runDay6PluginOpenAgentPanelFlow(context.scene)
            ));
            this.registerOperation('day6-plugin-open-management-panel-flow', (context) => (
                this.runDay6PluginOpenManagementPanelFlow(context.scene)
            ));
            this.registerOperation('day6-plugin-dashboard-handoff-flow', (context) => (
                this.runDay6PluginDashboardHandoffFlow(context.scene, context.narrationStartedAt)
            ));
            this.registerOperation('day6-plugin-sidepanel-flow', (context) => (
                this.runDay6PluginSidePanelFlow(context.scene, context.narrationStartedAt)
            ));
            this.registerOperation('rotate-galgame-tool-into-center', (context) => (
                this.runDay3GalgameWheelDragScene(context.scene, context.primaryTarget)
            ));
            this.registerOperation((context) => (
                context.operation.indexOf('show-agent-sidepanel:') === 0
                && context.scene
                && context.scene.activateSecondaryAction === true
            ), (context) => this.runShowAgentSidePanelAction(context.scene, context.operation));
            this.registerOperation('cleanup', (context) => this.runCleanup(context.scene));
            this.registerOperation((context) => (
                !context.operation
                || context.operation === 'show-task-hud'
                || context.operation.indexOf('show-agent-sidepanel:') === 0
                || context.operation.indexOf('show-settings-sidepanel:') === 0
            ), (context) => this.runPreparedNoopOperation(context.scene, context.operation));
            this.registerOperation('day3-open-settings-personalization', () => this.runDay3OpenSettingsPersonalization());
            this.registerOperation('day3-settings-detail', () => this.runDay3SettingsDetail());
            this.registerOperation('day4-animation-distance-showcase', (context) => (
                this.runDay4AnimationDistanceShowcase(context.scene, context.narrationStartedAt)
            ));
            this.registerOperation('settings-peek-panic', (context) => (
                this.runSettingsPeekPanic(context.scene, context.primaryTarget, context.narrationStartedAt)
            ));
            this.registerOperation({ prefix: 'show-settings-menu:' }, (context) => this.runShowSettingsMenu(context.operation));
            this.registerOperation('show-settings-management', (context) => this.runShowSettingsManagement(context.scene));
            this.registerOperation('click', (context) => this.runClick(context.primaryTarget));
            this.registerOperation('open-agent', (context) => this.runOpenAgent(context.scene));
            this.registerOperation('open-screen-popup', (context) => this.runOpenScreenPopup(context.primaryTarget));
            this.registerOperation('open-mic-popup', (context) => this.runOpenMicPopup(context.primaryTarget));
            this.registerOperation('open-compact-history-during-narration', (context) => (
                this.runOpenCompactHistoryDuringNarration(context.scene, context.narrationStartedAt)
            ));
            this.registerOperation('open-compact-tool-fan', (context) => this.runOpenCompactToolFan(context.primaryTarget));
            this.registerOperation('open-avatar-tool-menu', (context) => (
                this.runOpenAvatarToolMenu(context.scene, context.primaryTarget)
            ));
            this.registerOperation('show-avatar-tools-then-hide-after-narration', (context) => (
                this.runShowAvatarToolsThenHideAfterNarration(
                    context.scene,
                    context.primaryTarget,
                    context.narrationStartedAt,
                    context.narrationPromise
                )
            ));
            this.registerOperation('toggle-avatar-tool-after-narration', (context) => (
                this.runToggleAvatarToolAfterNarration(context.scene, context.narrationStartedAt)
            ));
        }

        resolveTargetEntry(targetKey) {
            if (!this.registry || typeof this.registry.resolve !== 'function') {
                return null;
            }
            return this.registry.resolve(targetKey) || null;
        }

        getExternalKind(targetKey) {
            if (this.registry && typeof this.registry.getExternalKind === 'function') {
                return this.registry.getExternalKind(targetKey) || '';
            }
            const entry = this.resolveTargetEntry(targetKey);
            return entry ? entry.externalKind || '' : '';
        }

        getLocalSelectors(targetKey) {
            if (this.registry && typeof this.registry.getLocalSelectors === 'function') {
                const selectors = this.registry.getLocalSelectors(targetKey);
                return Array.isArray(selectors) ? selectors.slice() : [];
            }
            const entry = this.resolveTargetEntry(targetKey);
            return entry && Array.isArray(entry.localSelectors) ? entry.localSelectors.slice() : [];
        }

        resolveTarget(targetKey, fallbackTarget) {
            if (fallbackTarget) {
                return fallbackTarget;
            }
            const normalizedKey = typeof targetKey === 'string' ? targetKey.trim() : '';
            if (!normalizedKey) {
                return null;
            }
            return this.director.resolveAvatarFloatingSelector(normalizedKey);
        }

        async runDay1IntroActivationFlow() {
            const director = this.director;
            if (!director || typeof director.playDay1IntroActivationRoundScene !== 'function') {
                return false;
            }
            return await director.playDay1IntroActivationRoundScene(director.sceneRunId);
        }

        async runDay1IntroGreetingFlow() {
            const director = this.director;
            if (!director || typeof director.playDay1IntroGreetingRoundScene !== 'function') {
                return false;
            }
            return await director.playDay1IntroGreetingRoundScene(director.sceneRunId);
        }

        async runDailyIntroGreetingPerformance(scene, context) {
            const director = this.director;
            if (!director || typeof director.runDailyIntroGreetingPerformance !== 'function') {
                return false;
            }
            return await director.runDailyIntroGreetingPerformance(scene, undefined, context);
        }

        async runDailyIntroAvatarPerformance(scene, context) {
            const director = this.director;
            if (!director || typeof director.runDailyIntroAvatarPerformance !== 'function') {
                return false;
            }
            return await director.runDailyIntroAvatarPerformance(scene, undefined, context);
        }

        async runDay1IntroGreetingPerformance(context) {
            const director = this.director;
            if (!director) {
                return false;
            }
            const avatarPerformancePromise = typeof director.runDailyIntroAvatarPerformance === 'function'
                ? director.runDailyIntroAvatarPerformance({
                        id: 'day1_intro_greeting',
                        introAvatarPerformance: { preset: 'wave-zoom' }
                    }, undefined, context).catch((error) => {
                        console.warn('[YuiGuide] intro greeting hug performance failed:', error);
                    })
                : Promise.resolve();
            const giftHeartPromise = typeof director.runIntroGiftHeartPerformance === 'function'
                ? director.runIntroGiftHeartPerformance().catch((error) => {
                    console.warn('[YuiGuide] intro gift heart performance failed:', error);
                })
                : Promise.resolve();
            if (context && context.isFirstDailyScene === true) {
                giftHeartPromise.catch(() => {});
                return await avatarPerformancePromise;
            }
            await Promise.all([
                avatarPerformancePromise,
                giftHeartPromise
            ]);
            return true;
        }

        async runDay1IntroBasicVoiceShowcase(scene, narrationStartedAt, narrationPromise) {
            const director = this.director;
            const voiceKey = scene && scene.voiceKey ? scene.voiceKey : '';
            const fallbackText = scene && scene.text ? scene.text : '';
            if (!director || typeof director.runIntroVoiceControlButtonShowcase !== 'function') {
                return false;
            }
            const introVoiceLookAtHandle = typeof director.ensurePreTakeoverGhostCursorLookAtPerformance === 'function'
                ? await director.ensurePreTakeoverGhostCursorLookAtPerformance({
                    isCancelled: () => (typeof director.isStopping === 'function' && director.isStopping())
                })
                : null;
            try {
                const narrationDurationMs = (
                    typeof director.getGuideVoiceDurationMs === 'function'
                        ? director.getGuideVoiceDurationMs(voiceKey, this.resolveGuideLocale())
                        : 0
                ) || 0;
                const cueDelayMs = Math.max(0, Math.floor(narrationDurationMs * 0.16));
                const elapsedMs = Number.isFinite(narrationStartedAt)
                    ? Math.max(0, Date.now() - narrationStartedAt)
                    : 0;
                if (cueDelayMs > elapsedMs && typeof director.waitForSceneDelay === 'function') {
                    await director.waitForSceneDelay(cueDelayMs - elapsedMs);
                }
                await Promise.all([
                    director.runIntroVoiceControlButtonShowcase(voiceKey, fallbackText).catch(() => {}),
                    narrationPromise ? Promise.resolve(narrationPromise) : Promise.resolve()
                ]);
            } finally {
                if (typeof director.isStopping === 'function' && director.isStopping()) {
                    if (
                        introVoiceLookAtHandle
                        && typeof director.stopIntroVoiceCursorLookAtPerformance === 'function'
                    ) {
                        await director.stopIntroVoiceCursorLookAtPerformance(
                            introVoiceLookAtHandle,
                            'intro_voice_showcase_complete'
                        );
                    }
                } else if (typeof director.adoptPreTakeoverGhostCursorLookAtHandle === 'function') {
                    director.adoptPreTakeoverGhostCursorLookAtHandle();
                }
            }
            return true;
        }

        async runDay1ScreenShareEntryFlow(scene) {
            const director = this.director;
            if (!director || typeof director.openMicPanel !== 'function') {
                return false;
            }

            const opened = await director.openMicPanel();
            if (!opened || director.isStopping()) {
                return false;
            }

            const screenShareRow = await director.waitForElement(() => {
                const popup = director.getManagedPanelElement('mic');
                const target = popup
                    ? popup.querySelector('[data-neko-mic-main-action-row="screen"]')
                    : null;
                return target && director.getElementRect(target) ? target : null;
            }, 2200);
            if (!screenShareRow || director.isStopping()) {
                return false;
            }

            const screenShareButton = screenShareRow.querySelector('[data-neko-mic-main-action="screen"]');
            if (!screenShareButton || !director.getElementRect(screenShareButton) || director.isStopping()) {
                return false;
            }

            director.setSpotlightGeometryHint(screenShareRow, {
                padding: 4,
                radius: 12,
                geometry: 'rounded-rect'
            });
            director.applyGuideHighlights({
                key: (scene && scene.id ? scene.id : 'day1-screen-share-entry') + '-screen-share',
                primary: screenShareRow
            });
            return !!(await director.moveCursorToElement(screenShareButton, 760));
        }

        async runDay1TakeoverCaptureCursor(scene) {
            const director = this.director;
            if (typeof director.captureDay1TakeoverAgentSwitches === 'function') {
                await director.captureDay1TakeoverAgentSwitches();
            }
            const step = director.getStep('takeover_capture_cursor') || {
                anchor: scene.target || '',
                performance: {}
            };
            const performance = Object.assign({}, step.performance || {}, {
                cursorTarget: scene.cursorTarget || scene.target || (step.performance && step.performance.cursorTarget) || '',
                voiceKey: scene.voiceKey || (step.performance && step.performance.voiceKey) || '',
                emotion: scene.emotion || (step.performance && step.performance.emotion) || ''
            });
            return await director.runTakeoverKeyboardControlSequence(step, performance, director.sceneRunId);
        }

        setDay1AvatarZoomInteractionActive(active) {
            const director = this.director;
            const isActive = active === true;
            if (!director) {
                return false;
            }

            if (director.overlay && typeof director.overlay.setInteractionShieldSuppressed === 'function') {
                director.overlay.setInteractionShieldSuppressed(isActive);
            }
            if (typeof director.clearUserCursorRevealSuppression === 'function') {
                director.clearUserCursorRevealSuppression(true);
            }
            if (isActive) {
                if (typeof director.disableInterrupts === 'function') {
                    director.disableInterrupts();
                }
                if (director.cursor) {
                    if (typeof director.cursor.cancel === 'function') {
                        director.cursor.cancel();
                    }
                    if (typeof director.cursor.hide === 'function') {
                        director.cursor.hide();
                    }
                }
            }
            this.setDay1AvatarZoomModelLocked(isActive);

            if (typeof document !== 'undefined' && document.body) {
                document.documentElement.classList.toggle('yui-user-cursor-revealed', isActive);
                document.body.classList.toggle('yui-user-cursor-revealed', isActive);
            }
            if (typeof director.syncSystemCursorHidden === 'function') {
                director.syncSystemCursorHidden(
                    !isActive,
                    isActive ? 'day1-avatar-zoom-hint' : 'day1-return-control'
                );
            }
            return true;
        }

        setDay1AvatarZoomModelLocked(active) {
            const manager = typeof window !== 'undefined' ? window.live2dManager : null;
            if (!manager || typeof manager.setLocked !== 'function') {
                return false;
            }
            if (active === true) {
                if (typeof this.day1AvatarZoomOriginalLive2dLocked !== 'boolean') {
                    this.day1AvatarZoomOriginalLive2dLocked = manager.isLocked === true;
                }
                manager.setLocked(false, { updateFloatingButtons: false });
                return true;
            }
            if (typeof this.day1AvatarZoomOriginalLive2dLocked !== 'boolean') {
                return false;
            }
            manager.setLocked(this.day1AvatarZoomOriginalLive2dLocked, { updateFloatingButtons: false });
            this.day1AvatarZoomOriginalLive2dLocked = null;
            return true;
        }

        async runCleanup(scene) {
            const sceneId = scene && typeof scene.id === 'string' ? scene.id : '';
            const day1RestoreReason = sceneId === 'day1_avatar_zoom_hint'
                ? 'day1-before-avatar-zoom-hint'
                : (sceneId === 'day1_takeover_return_control' ? 'day1-return-control' : '');
            if (sceneId === 'day1_takeover_return_control') {
                this.setDay1AvatarZoomInteractionActive(false);
            }
            if (
                day1RestoreReason
                && this.director
                && typeof this.director.restoreDay1TakeoverAgentSwitches === 'function'
            ) {
                const restored = await this.director.restoreDay1TakeoverAgentSwitches(day1RestoreReason);
                if (sceneId === 'day1_avatar_zoom_hint') {
                    this.setDay1AvatarZoomInteractionActive(true);
                }
                return restored;
            }
            if (sceneId === 'day1_avatar_zoom_hint') {
                this.setDay1AvatarZoomInteractionActive(true);
            }
            return true;
        }

        async runDay6PluginOpenAgentPanelFlow(scene) {
            return this.director.runDay6PluginOpenAgentPanelFlow(scene);
        }

        async runDay6PluginOpenManagementPanelFlow(scene) {
            return this.director.runDay6PluginOpenManagementPanelFlow(scene);
        }

        async runDay6PluginDashboardHandoffFlow(scene, narrationStartedAt) {
            return this.director.runDay6PluginDashboardHandoffFlow(scene, narrationStartedAt);
        }

        async runDay6PluginSidePanelFlow(scene, narrationStartedAt) {
            return this.director.runDay6PluginSidePanelFlow(scene, narrationStartedAt);
        }

        async runDay3GalgameWheelDragScene(scene, primaryTarget) {
            return this.director.runDay3GalgameWheelDragScene(scene, primaryTarget);
        }

        async runShowAgentSidePanelAction(scene, operation) {
            const director = this.director;
            const parts = operation.split(':');
            const toggleId = parts[1] === 'openclaw' ? 'agent-openclaw' : 'agent-user-plugin';
            const actionId = parts[2] || '';
            if (actionId) {
                const hadPluginDashboard = toggleId === 'agent-user-plugin' && actionId === 'management-panel'
                    ? !!(await director.waitForOpenedWindow(this.pluginDashboardWindowName, 120))
                    : false;
                await director.clickAgentSidePanelAction(toggleId, actionId, {
                    keepMainUIVisible: true,
                    source: 'avatar-floating-guide',
                    sceneId: scene.id || ''
                });
                if (toggleId === 'agent-user-plugin' && actionId === 'management-panel' && !hadPluginDashboard) {
                    const pluginDashboardWindow = await director.waitForOpenedWindow(this.pluginDashboardWindowName, 1400);
                    director.pluginDashboardWindowCreatedByGuide = !!(pluginDashboardWindow && !pluginDashboardWindow.closed);
                    if (director.pluginDashboardWindowCreatedByGuide) {
                        await director.waitForSceneDelay(900);
                        await director.closePluginDashboardWindowIfCreatedByGuide('Day 6 插件管理预览完成');
                    }
                }
                await director.waitForSceneDelay(460);
            }
            return true;
        }

        async runPreparedNoopOperation(scene, operation) {
            if (!operation && scene && scene.id === 'day2_galgame_games') {
                await this.director.tourMiniGameChoiceButtons();
            }
            return true;
        }

        async runDay3OpenSettingsPersonalization() {
            return await this.director.openSettingsPanel();
        }

        async runDay3SettingsDetail() {
            await this.director.ensureCharacterSettingsSidePanelVisible().catch(() => null);
            return true;
        }

        async runDay4AnimationDistanceShowcase(scene, narrationStartedAt) {
            return this.director.runDay4AnimationDistanceShowcase(scene, narrationStartedAt);
        }

        async runSettingsPeekPanic(scene, primaryTarget, narrationStartedAt) {
            const director = this.director;
            const fullDurationMs = director.getGuideVoiceDurationMs(scene.voiceKey || '', this.resolveGuideLocale())
                || 0;
            const elapsedMs = Number.isFinite(narrationStartedAt)
                ? Math.max(0, Date.now() - narrationStartedAt)
                : 0;
            await director.runSettingsPeekPanicPerformance({
                targetRect: director.getElementRect(primaryTarget),
                totalDurationMs: Math.max(600, Math.round(fullDurationMs - elapsedMs)),
                runId: director.sceneRunId
            }).catch(() => {});
            return true;
        }

        async runShowSettingsMenu(operation) {
            await this.director.ensureSettingsMenuVisible(operation.split(':')[1] || '');
            return true;
        }

        async runShowSettingsManagement(scene) {
            const director = this.director;
            await director.closeAgentPanel().catch(() => {});
            const opened = await director.openSettingsPanel();
            if (!opened) {
                return false;
            }
            await director.ensureCharacterSettingsSidePanelVisible();
            const targets = director.getSettingsPeekTargets();
            const p = typeof director.resolveModelPrefix === 'function' ? director.resolveModelPrefix() : 'live2d';
            director.setSceneExtraSpotlights([
                targets.characterMenu,
                director.getCharacterSettingsSidePanel(),
                director.resolveElement(`#${p}-menu-api-keys`),
                director.resolveElement(`#${p}-menu-memory`)
            ].filter(Boolean));
            return true;
        }

        async runClick(primaryTarget) {
            if (primaryTarget && typeof primaryTarget.click === 'function') {
                primaryTarget.click();
            }
            await this.director.waitForSceneDelay(360);
            return true;
        }

        async runOpenAgent(scene) {
            const director = this.director;
            const opened = await director.openAgentPanel();
            if (opened) {
                director.applyGuideHighlights({
                    key: (scene.id || 'avatar-floating-open-agent') + '-panel-open',
                    persistent: await director.resolveAvatarFloatingPersistent(scene, {
                        fallbackToChatWindow: false
                    }),
                    primary: null,
                    secondary: null
                });
            }
            return opened;
        }

        async runOpenScreenPopup(primaryTarget) {
            const director = this.director;
            const p = typeof director.resolveModelPrefix === 'function' ? director.resolveModelPrefix() : 'live2d';
            if (primaryTarget && typeof primaryTarget.click === 'function') {
                primaryTarget.click();
            }
            await director.waitForElement(() => {
                const popup = director.resolveElement(`#${p}-popup-screen`);
                return popup && director.isElementVisible(popup) && popup.style.display === 'flex' ? popup : null;
            }, 1800);
            return true;
        }

        async runOpenMicPopup(primaryTarget) {
            const director = this.director;
            const p = typeof director.resolveModelPrefix === 'function' ? director.resolveModelPrefix() : 'live2d';
            if (primaryTarget && typeof primaryTarget.click === 'function') {
                primaryTarget.click();
            }
            await director.waitForElement(() => {
                const popup = director.resolveElement(`#${p}-popup-mic`);
                return popup && director.isElementVisible(popup) && popup.style.display === 'flex' ? popup : null;
            }, 1800);
            return true;
        }

        async runOpenCompactHistoryDuringNarration(scene, narrationStartedAt) {
            const director = this.director;
            director.setCompactHistoryOpen(true, 'avatar-floating-guide-open-history');
            const durationMs = director.getAvatarFloatingNarrationDurationMs(scene.voiceKey || '', scene.text || '');
            const elapsedMs = Number.isFinite(narrationStartedAt)
                ? Math.max(0, Date.now() - narrationStartedAt)
                : 0;
            await director.waitForSceneDelay(Math.max(360, durationMs - elapsedMs));
            director.setCompactHistoryOpen(false, 'avatar-floating-guide-close-history');
            return true;
        }

        async runOpenCompactToolFan(primaryTarget) {
            const director = this.director;
            const toggle = this.resolveTarget('chat-tool-toggle', primaryTarget);
            const isOpen = !!(toggle && (
                toggle.getAttribute('aria-expanded') === 'true'
                || toggle.classList.contains('is-open')
            ));
            if (!director.isHomeChatExternalized() && toggle && typeof toggle.click === 'function' && !isOpen) {
                toggle.click();
            }
            director.setCompactToolFanOpen(true, 'avatar-floating-guide-open-tool-fan');
            await director.waitForSceneDelay(360);
            return true;
        }

        async runOpenAvatarToolMenu(scene, primaryTarget) {
            const director = this.director;
            director.setCompactToolFanOpen(true, 'avatar-floating-guide-open-avatar-tool-fan');
            if (director.isHomeChatExternalized()) {
                await director.waitForSceneDelay(960);
                director.setChatAvatarToolMenuOpen(true, 'avatar-floating-guide-open-avatar-tool-menu');
                await director.waitForSceneDelay(260);
                if (
                    director.interactionTakeover
                    && typeof director.interactionTakeover.setExternalizedChatSpotlight === 'function'
                ) {
                    if (typeof director.clearHomeSpotlightsForExternalizedChat === 'function') {
                        director.clearHomeSpotlightsForExternalizedChat();
                    }
                    const spotlightVariant = scene && typeof scene.spotlightVariant === 'string'
                        ? scene.spotlightVariant.trim()
                        : '';
                    director.interactionTakeover.setExternalizedChatSpotlight(
                        this.getExternalKind(scene && scene.persistent || '')
                        || director.getExternalizedChatTargetKind(scene && scene.persistent || '', scene)
                        || 'avatar-tools',
                        { variant: spotlightVariant }
                    );
                }
                await director.waitForSceneDelay(520);
                return true;
            }
            const button = this.resolveTarget('chat-avatar-tools', primaryTarget);
            if (button) {
                director.keepAvatarToolButtonHighlightedAfterMenuOpen(button, scene);
            }
            if (!director.getVisibleChatAvatarToolMenuPopover()) {
                director.setChatAvatarToolMenuOpen(true, 'avatar-floating-guide-open-avatar-tool-menu');
            }
            let toolTargets = await director.waitForAvatarToolMenuTargets(3, 900);
            if (toolTargets.length < 3) {
                director.setChatAvatarToolMenuOpen(true, 'avatar-floating-guide-open-avatar-tool-menu-retry');
                toolTargets = await director.waitForAvatarToolMenuTargets(3, 1400);
            }
            director.keepAvatarToolButtonHighlightedAfterMenuOpen(button, scene);
            return toolTargets.length >= 3;
        }

        async runShowAvatarToolsThenHideAfterNarration(scene, primaryTarget, narrationStartedAt, narrationPromise) {
            const director = this.director;
            const waitForNarrationPromise = async () => {
                if (narrationPromise && typeof narrationPromise.then === 'function') {
                    await narrationPromise;
                    return;
                }
                const elapsedMs = Number.isFinite(narrationStartedAt)
                    ? Math.max(0, Date.now() - narrationStartedAt)
                    : 0;
                await director.waitForSceneDelay(Math.max(0, 360 - elapsedMs));
            };
            if (director.isHomeChatExternalized()) {
                director.clickChatAvatarToolButton('avatar-floating-guide-open-avatar-tool-menu');
                director.setChatAvatarToolMenuOpen(true, 'avatar-floating-guide-open-avatar-tool-menu');
                await waitForNarrationPromise();
                director.clickChatAvatarToolButton('avatar-floating-guide-close-avatar-tool-menu-after-narration');
                director.setChatAvatarToolMenuOpen(false, 'avatar-floating-guide-close-avatar-tool-menu-after-narration');
                return true;
            }
            const button = this.resolveTarget('chat-avatar-tools', primaryTarget);
            if (button && typeof button.click === 'function') {
                button.click();
            }
            if (!director.getVisibleChatAvatarToolMenuPopover()) {
                director.setChatAvatarToolMenuOpen(true, 'avatar-floating-guide-open-avatar-tool-menu');
            }
            let toolTargets = await director.waitForAvatarToolMenuTargets(3, 900);
            if (toolTargets.length < 3) {
                director.setChatAvatarToolMenuOpen(true, 'avatar-floating-guide-open-avatar-tool-menu-retry');
                toolTargets = await director.waitForAvatarToolMenuTargets(3, 1400);
            }
            director.keepAvatarToolButtonHighlightedAfterMenuOpen(button, scene);
            await waitForNarrationPromise();
            if (button && typeof button.click === 'function') {
                button.click();
            }
            director.setChatAvatarToolMenuOpen(false, 'avatar-floating-guide-close-avatar-tool-menu-after-narration');
            return toolTargets.length >= 3;
        }

        async runToggleAvatarToolAfterNarration(scene, narrationStartedAt) {
            const director = this.director;
            const durationMs = director.getAvatarFloatingNarrationDurationMs(scene.voiceKey || '', scene.text || '');
            const elapsedMs = Number.isFinite(narrationStartedAt)
                ? Math.max(0, Date.now() - narrationStartedAt)
                : 0;
            await director.waitForSceneDelay(Math.max(360, durationMs - elapsedMs));
            if (director.isHomeChatExternalized()) {
                director.setChatAvatarToolMenuOpen(false, 'avatar-floating-guide-toggle-avatar-tool-after-narration');
                await director.waitForSceneDelay(220);
                return true;
            }
            const button = this.resolveTarget('chat-avatar-tools');
            if (button && typeof button.click === 'function') {
                button.click();
            } else {
                director.setChatAvatarToolMenuOpen(false, 'avatar-floating-guide-toggle-avatar-tool-after-narration');
            }
            return true;
        }

        run(scene, primaryTarget, narrationStartedAt, narrationPromise, operationContext) {
            const operation = scene && typeof scene.operation === 'string' ? scene.operation : '';
            const context = Object.assign({}, operationContext || {}, {
                scene,
                primaryTarget,
                narrationStartedAt,
                narrationPromise,
                operation
            });
            for (let index = 0; index < this.operationHandlers.length; index += 1) {
                const entry = this.operationHandlers[index];
                if (entry.matches(context)) {
                    return entry.handler.call(this, context);
                }
            }
            return true;
        }

    }

    return {
        OperationRegistry: OperationRegistry
    };
});
