"""Exercise the real ASR status dispatch and identity fence in Node."""

import json
import shutil
from pathlib import Path

import pytest

from tests.node_harness import run_node_script
from tests.unit.test_app_websocket_static import _block_after


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "static" / "app" / "app-websocket.js"


@pytest.mark.parametrize(
    "scenario",
    [
        "active",
        "before_session_ack",
        "duplicate_incident",
        "old_session",
        "old_transport",
        "old_revision",
        "missing_identity",
        "missing_incident_and_identity",
        "missing_incident_stale_identity",
        "real_failure_after_degradation",
    ],
)
def test_speaker_degradation_status_behavior(scenario: str) -> None:
    source = SOURCE.read_text(encoding="utf-8")
    signatures = (
        "function parseAsrControlIdentity(details) {",
        "function acceptAsrControlIdentity(details) {",
        "function normalizeAsrReasonCode(value) {",
        "function normalizeAsrIncidentId(value) {",
        "function formatAsrFailureMessage(baseMessage, reasonCode) {",
        "function showAsrIncidentToast(incidentId, message, durationMs) {",
        "function tearDownBlockedVoiceRoute() {",
    )
    helpers = "\n".join(
        signature + _block_after(source, signature) + "}" for signature in signatures
    )
    marker = "var statusReasonCode = normalizeAsrReasonCode("
    dispatch = marker + source.split(marker, 1)[1].split(
        "if (statusCode === 'TTS_CONNECTION_FAILED')", 1
    )[0]
    script = r"""
        const assert = require('node:assert/strict');
        let _latestAsrControlIdentity = null;
        const _seenAsrIncidentIds = Object.create(null);
        const _seenAsrIncidentOrder = [];
        const MAX_SEEN_ASR_INCIDENTS = 64;
        const shown = [];
        let previewRemoved = 0;
        let micStopped = 0;
        const S = {
            independentAsrActive: true,
            independentAsrProvider: 'qwen',
            voiceInputRouteBlocked: false,
            voiceInputLifecycleState: 'active',
            isRecording: true,
            gameVoiceSttGateActive: false,
            isSessionStarted: true,
        };
        const window = {
            t: key => key,
            showStatusToast: (...args) => shown.push(args),
            stopMicCapture: () => { micStopped += 1; },
        };
        function removeExternalAsrPreview() { previewRemoved += 1; }
        function setBlockedGameVoiceTranscriptionState() {
            S.transcription = 'blocked';
        }
        function details(extra = {}) {
            return Object.assign({
                session_epoch: 2, transport_generation: 3, lifecycle_revision: 5,
                incident_id: 'asr-failure-speaker-evidence',
                reason_code: 'ASR_SPEAKER_CAPTURE_UNAVAILABLE',
            }, extra);
        }
    """ + helpers + "\nfunction dispatch(statusCode, statusDetails) {\n" + dispatch + "\n}\n"
    script += "const scenario = " + json.dumps(scenario) + ";\n"
    script += r"""
        if (scenario === 'before_session_ack') S.isSessionStarted = false;
        const before = JSON.stringify(S);
        const incoming = details();
        let expectedToasts = 1;
        if (scenario.startsWith('old_') || scenario === 'missing_incident_stale_identity') {
            acceptAsrControlIdentity(details());
            expectedToasts = 0;
            if (scenario === 'old_session') {
                incoming.session_epoch = 1;
                incoming.transport_generation = 99;
                incoming.lifecycle_revision = 99;
            } else if (scenario === 'old_transport') {
                incoming.transport_generation = 2;
                incoming.lifecycle_revision = 99;
            } else {
                incoming.lifecycle_revision = 4;
            }
        }
        if (scenario.includes('missing_incident')) delete incoming.incident_id;
        if (scenario === 'missing_identity' || scenario === 'missing_incident_and_identity') {
            delete incoming.session_epoch;
            expectedToasts = 0;
        }
        dispatch('ASR_SPEAKER_EVIDENCE_UNAVAILABLE', incoming);
        if (scenario === 'duplicate_incident') {
            dispatch('ASR_SPEAKER_EVIDENCE_UNAVAILABLE', details({ lifecycle_revision: 6 }));
        }
        assert.equal(JSON.stringify(S), before, 'degradation must preserve all route and UI state');
        assert.equal(previewRemoved, 0, 'degradation must preserve the ASR preview');
        assert.equal(micStopped, 0, 'degradation must preserve microphone capture');
        assert.equal(shown.length, expectedToasts, 'identity fence and incident dedup apply');
        if (expectedToasts) {
            assert.equal(shown[0][0],
                'microphone.speakerEvidenceUnavailable [ASR_SPEAKER_CAPTURE_UNAVAILABLE]');
        }
        if (scenario === 'real_failure_after_degradation') {
            dispatch('ASR_AUDIO_ORDERING_FAILED', details({
                lifecycle_revision: 6,
                incident_id: 'asr-failure-real-transport',
                reason_code: 'ASR_AUDIO_ORDERING_FAILED',
            }));
            assert.equal(S.independentAsrActive, false);
            assert.equal(S.voiceInputRouteBlocked, true);
            assert.equal(previewRemoved, 1);
            assert.equal(micStopped, 1);
            assert.equal(shown.length, 2, 'a later real failure still notifies');
            assert.match(shown[1][0], /ASR_AUDIO_ORDERING_FAILED/);
        }
    """
    result = run_node_script(
        shutil.which("node") or pytest.skip("node is required"),
        script,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("locale", ["en", "ja", "ko", "zh-CN", "zh-TW", "ru", "pt", "es"])
def test_speaker_degradation_locale_is_present(locale: str) -> None:
    translations = json.loads((ROOT / "static" / "locales" / f"{locale}.json").read_text("utf-8"))
    assert translations["microphone"]["speakerEvidenceUnavailable"].strip()
