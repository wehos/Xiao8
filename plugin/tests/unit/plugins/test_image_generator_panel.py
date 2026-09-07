"""Behavior tests for the image generator management-panel script."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.plugin_unit

PANEL_HTML = (
    Path(__file__).resolve().parents[3]
    / "plugins"
    / "image_generator"
    / "static"
    / "index.html"
)


def _extract_panel_script() -> str:
    html = PANEL_HTML.read_text(encoding="utf-8")
    scripts = list(
        re.finditer(
            r"<script(?P<attrs>\s[^>]*)?>(?P<body>.*?)</script\s*>",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )
    assert len(scripts) == 1, "the panel must contain exactly one expected script"
    attrs = scripts[0].group("attrs") or ""
    assert re.search(r"\bsrc\s*=", attrs, flags=re.IGNORECASE) is None
    source = scripts[0].group("body")
    assert source.count("const PLUGIN_ID = 'image_generator';") == 1
    assert source.count("initialize().catch") == 1
    return source


NODE_HARNESS = r"""
'use strict';

const fs = require('node:fs');
const { webcrypto } = require('node:crypto');

const panelSource = fs.readFileSync(process.env.PANEL_SCRIPT_PATH, 'utf8');
const scenario = process.env.PANEL_SCENARIO;
const expectedOrdinaryError = process.env.EXPECTED_ORDINARY_ERROR || '';
const SECRET = 'sk-panel-plaintext-must-never-enter-runs-987654321';
const realSetTimeout = globalThis.setTimeout.bind(globalThis);
const realClearTimeout = globalThis.clearTimeout.bind(globalThis);
const toastMessages = [];

function check(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

class FakeEvent {
  constructor(type, init = {}) {
    this.type = type;
    this.defaultPrevented = false;
    this.bubbles = Boolean(init.bubbles);
    Object.assign(this, init);
  }

  preventDefault() {
    this.defaultPrevented = true;
  }
}

class FakeElement {
  constructor(id = '', tagName = 'div') {
    this.id = id;
    this.tagName = String(tagName).toUpperCase();
    this.dataset = {};
    this.attributes = new Map();
    this.children = [];
    this.listeners = new Map();
    this.parentNode = null;
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.placeholder = '';
    this.title = '';
    this.className = '';
    this.href = '';
    this.name = id;
    this.validity = { valid: true };
    this.labels = [{ textContent: id }];
    this._textContent = '';
  }

  set textContent(value) {
    this._textContent = String(value ?? '');
    if (this.id === 'toast' && this._textContent) {
      toastMessages.push(this._textContent);
    }
  }

  get textContent() {
    return this._textContent;
  }

  get options() {
    return this.children.filter((child) => child && child.tagName === 'OPTION');
  }

  setAttribute(name, value) {
    const normalized = String(value);
    this.attributes.set(String(name), normalized);
    if (name === 'placeholder') this.placeholder = normalized;
    if (name === 'title') this.title = normalized;
  }

  getAttribute(name) {
    return this.attributes.has(String(name))
      ? this.attributes.get(String(name))
      : null;
  }

  removeAttribute(name) {
    this.attributes.delete(String(name));
    if (name === 'title') this.title = '';
    if (name === 'href') this.href = '';
  }

  addEventListener(type, callback) {
    const callbacks = this.listeners.get(type) || [];
    callbacks.push(callback);
    this.listeners.set(type, callbacks);
  }

  dispatchEvent(event) {
    event.target = this;
    for (const callback of this.listeners.get(event.type) || []) {
      callback.call(this, event);
    }
    return !event.defaultPrevented;
  }

  appendChild(child) {
    if (child && typeof child === 'object') child.parentNode = this;
    this.children.push(child);
    return child;
  }

  append(...children) {
    children.forEach((child) => this.appendChild(child));
  }

  replaceChildren(...children) {
    this.children.forEach((child) => {
      if (child && typeof child === 'object') child.parentNode = null;
    });
    this.children = [];
    this.append(...children);
  }

  focus() {}

  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter(
      (child) => child !== this,
    );
    this.parentNode = null;
  }
}

const elementIds = [
  'connectionBadge', 'connectionText', 'refreshButton',
  'runtimeValue', 'runtimeDetail', 'credentialValue', 'credentialDetail',
  'cacheValue', 'cacheDetail', 'historyValue', 'historyDetail',
  'apiStatusPill', 'apiStatusText', 'settingsForm', 'provider', 'apiBaseUrl',
  'apiKey', 'apiKeyHelp', 'model', 'outputFormat', 'responseFormat',
  'allowedSizes', 'defaultSize', 'allowedQualities', 'defaultQuality',
  'allowedStyles', 'defaultStyle', 'timeoutSeconds', 'maxDownloadMiB',
  'cacheMaxCount', 'cacheMaxMiB', 'historyLimit', 'autoShow', 'saveButton',
  'resetButton', 'clearKeyButton', 'testPrompt', 'promptCounter', 'testButton',
  'testResult', 'testPreview', 'testResultTitle', 'testResultText',
  'testResultLink', 'historyRefreshButton', 'lastRequestValue', 'historyList',
  'historyEmpty', 'clearHistoryButton', 'toast',
  'lightbox', 'lightboxImage', 'lightboxClose',
];
const selectIds = new Set([
  'provider', 'outputFormat', 'responseFormat', 'defaultSize',
  'defaultQuality', 'defaultStyle',
]);
const buttonIds = new Set([
  'refreshButton', 'saveButton', 'resetButton', 'clearKeyButton', 'testButton',
  'historyRefreshButton', 'clearHistoryButton', 'lightboxClose',
]);
const textareaIds = new Set([
  'allowedSizes', 'allowedQualities', 'allowedStyles', 'testPrompt',
]);
const elements = new Map();
for (const id of elementIds) {
  let tagName = 'div';
  if (id === 'settingsForm') tagName = 'form';
  else if (selectIds.has(id)) tagName = 'select';
  else if (buttonIds.has(id)) tagName = 'button';
  else if (textareaIds.has(id)) tagName = 'textarea';
  else if (id === 'testResultLink') tagName = 'a';
  else if (id === 'lightboxImage') tagName = 'img';
  else if ([
    'apiBaseUrl', 'apiKey', 'model', 'timeoutSeconds', 'maxDownloadMiB',
    'cacheMaxCount', 'cacheMaxMiB', 'historyLimit', 'autoShow',
  ].includes(id)) tagName = 'input';
  elements.set(id, new FakeElement(id, tagName));
}

const document = {
  body: new FakeElement('body', 'body'),
  documentElement: new FakeElement('html', 'html'),
  title: '',
  _listeners: {},
  addEventListener(type, callback) {
    (this._listeners[type] = this._listeners[type] || []).push(callback);
  },
  removeEventListener(type, callback) {
    const list = this._listeners[type] || [];
    const index = list.indexOf(callback);
    if (index >= 0) list.splice(index, 1);
  },
  getElementById(id) {
    return elements.get(id) || null;
  },
  querySelectorAll(selector) {
    if (selector === 'button') {
      return [...elements.values()].filter((element) => (
        element.tagName === 'BUTTON'
      ));
    }
    return [];
  },
  createElement(tagName) {
    return new FakeElement('', tagName);
  },
  createTextNode(value) {
    return { nodeType: 3, textContent: String(value), parentNode: null };
  },
};

globalThis.window = globalThis;
globalThis.document = document;
globalThis.Event = FakeEvent;
globalThis.location = new URL(
  'http://testserver/plugin/image_generator/ui/?locale=zh-CN',
);
Object.defineProperty(globalThis, 'navigator', {
  value: { language: 'zh-CN' },
  configurable: true,
});
Object.defineProperty(globalThis, 'crypto', {
  value: webcrypto,
  configurable: true,
});
globalThis.confirm = () => true;
globalThis.setTimeout = (callback, delay, ...args) => (
  realSetTimeout(callback, Math.min(Number(delay) || 0, 2), ...args)
);
globalThis.clearTimeout = realClearTimeout;
globalThis.atob = (value) => Buffer.from(String(value), 'base64').toString('binary');
globalThis.btoa = (value) => Buffer.from(String(value), 'binary').toString('base64');

function response(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() {
      return JSON.parse(JSON.stringify(payload));
    },
  };
}

function settings(model) {
  return {
    provider: 'openai',
    api_base_url: 'https://api.openai.com/v1',
    model,
    default_size: '1024x1024',
    default_quality: 'standard',
    default_style: '',
    allowed_sizes: ['1024x1024'],
    allowed_qualities: ['standard'],
    allowed_styles: ['', 'auto'],
    output_format: 'auto',
    response_format: 'b64_json',
    timeout_seconds: 120,
    max_download_bytes: 10 * 1024 * 1024,
    cache_max_count: 20,
    cache_max_bytes: 100 * 1024 * 1024,
    history_limit: 30,
    auto_show_in_chat: true,
  };
}

async function makeEnvelope(keyId) {
  const keyPair = await webcrypto.subtle.generateKey(
    {
      name: 'RSA-OAEP',
      modulusLength: 2048,
      publicExponent: new Uint8Array([1, 0, 1]),
      hash: 'SHA-256',
    },
    true,
    ['encrypt', 'decrypt'],
  );
  const spki = await webcrypto.subtle.exportKey('spki', keyPair.publicKey);
  return {
    keyPair,
    envelope: {
      key_id: keyId,
      public_key_spki_b64: Buffer.from(spki).toString('base64'),
      algorithm: 'RSA-OAEP-256+A256GCM',
      expires_at: Date.now() + 60_000,
      max_plaintext_bytes: 32_768,
    },
  };
}

function panelState(envelope, configured, model) {
  return {
    running: true,
    api_state: configured ? { status: 'ok', healthy: true } : { status: 'idle' },
    configuration_warning: '',
    store_enabled: true,
    asset_cache_available: true,
    api_key_configured: configured,
    secret_envelope: envelope,
    settings: settings(model),
    defaults: settings('default-model'),
    history: [],
    cache: { count: 0, bytes: 0 },
    last_request: {},
  };
}

async function decryptSaveArgs(args, keyPair) {
  const outer = JSON.parse(
    Buffer.from(args.encrypted_payload, 'base64').toString('utf8'),
  );
  const additionalData = new TextEncoder().encode(
    `image_generator:${args.key_id}`,
  );
  const rawContentKey = await webcrypto.subtle.decrypt(
    { name: 'RSA-OAEP', label: additionalData },
    keyPair.privateKey,
    Buffer.from(outer.wrapped_key, 'base64'),
  );
  const contentKey = await webcrypto.subtle.importKey(
    'raw',
    rawContentKey,
    { name: 'AES-GCM' },
    false,
    ['decrypt'],
  );
  const plaintext = await webcrypto.subtle.decrypt(
    {
      name: 'AES-GCM',
      iv: Buffer.from(outer.iv, 'base64'),
      additionalData,
    },
    contentKey,
    Buffer.from(outer.ciphertext, 'base64'),
  );
  return JSON.parse(Buffer.from(plaintext).toString('utf8'));
}

async function waitFor(predicate, label, timeout = 5000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise((resolve) => realSetTimeout(resolve, 5));
  }
  throw new Error(
    `timed out waiting for ${label}; entries=${JSON.stringify(entryCalls)}; `
      + `toasts=${JSON.stringify(toastMessages)}`,
  );
}

async function main() {
  check(
    ['retry_success', 'detail_error', 'error_error'].includes(scenario),
    `unknown scenario: ${scenario}`,
  );
  const old = await makeEnvelope('a'.repeat(32));
  const freshOne = await makeEnvelope('b'.repeat(32));
  const freshTwo = await makeEnvelope('c'.repeat(32));
  const fresh = [freshOne, freshTwo];
  const keyPairs = new Map(
    fresh.map((item) => [item.envelope.key_id, item.keyPair]),
  );

  globalThis.entryCalls = [];
  globalThis.postBodies = [];
  globalThis.saveArgs = [];
  const runs = new Map();
  let runCounter = 0;
  let envelopeCallCount = 0;
  let saveCallCount = 0;
  let saveSucceeded = false;

  globalThis.fetch = async (rawUrl, options = {}) => {
    const url = String(rawUrl);
    if (url === '/plugin/image_generator/ui-api/locale') {
      return response({}, 404);
    }
    if (url.includes('/plugin/image_generator/ui-api/i18n/')) {
      return response({});
    }
    if (url === '/runs' && options.method === 'POST') {
      const rawBody = String(options.body || '{}');
      const body = JSON.parse(rawBody);
      postBodies.push(rawBody);
      entryCalls.push(body.entry_id);
      const runId = `run-${++runCounter}`;
      let record = { status: 'succeeded' };
      let data = {};

      if (body.entry_id === 'get_panel_state') {
        data = saveSucceeded
          ? panelState(old.envelope, true, 'persisted-model-after-refresh')
          : panelState(old.envelope, false, 'initial-model');
      } else if (body.entry_id === 'get_secret_envelope') {
        const issued = fresh[envelopeCallCount];
        check(issued, 'the panel requested more than two fresh envelopes');
        envelopeCallCount += 1;
        data = { secret_envelope: issued.envelope };
      } else if (body.entry_id === 'save_settings') {
        saveCallCount += 1;
        saveArgs.push(body.args);
        if (scenario === 'retry_success' && saveCallCount === 1) {
          record = {
            status: 'failed',
            error: {
              message: '加密设置载荷已过期或已使用，请刷新面板后重试',
            },
          };
        } else if (scenario === 'detail_error') {
          record = {
            status: 'failed',
            error: { detail: '服务端拒绝了当前模型配置' },
          };
        } else if (scenario === 'error_error') {
          record = {
            status: 'failed',
            error: { error: 'provider rejected this model' },
          };
        } else {
          saveSucceeded = true;
          data = { saved: true };
        }
      } else {
        throw new Error(`unexpected panel entry: ${body.entry_id}`);
      }

      runs.set(runId, { record, data });
      return response({ run_id: runId, status: 'queued' });
    }

    const match = /^\/runs\/([^/]+)(\/export)?$/.exec(url);
    if (match) {
      const run = runs.get(decodeURIComponent(match[1]));
      check(run, `unknown run requested: ${url}`);
      if (match[2]) {
        return response({
          items: [{
            type: 'json',
            json: { success: true, data: run.data },
          }],
        });
      }
      return response(run.record);
    }
    throw new Error(`unexpected fetch: ${url}`);
  };

  eval(panelSource);
  await waitFor(
    () => (
      entryCalls.filter((entry) => entry === 'get_panel_state').length >= 1
      && elements.get('model').value === 'initial-model'
    ),
    'initial panel state',
  );

  elements.get('apiKey').value = SECRET;
  elements.get('model').value = 'edited-model-before-save';
  elements.get('settingsForm').dispatchEvent(new FakeEvent('submit'));

  if (scenario === 'retry_success') {
    await waitFor(
      () => (
        saveCallCount === 2
        && entryCalls.filter((entry) => entry === 'get_panel_state').length >= 2
        && toastMessages.some((message) => message.includes('设置已保存'))
      ),
      'transparent envelope retry and saved-state refresh',
    );
    check(envelopeCallCount === 2, `fresh envelope count: ${envelopeCallCount}`);
    check(saveCallCount === 2, `save attempt count: ${saveCallCount}`);
    const relevantEntries = entryCalls.filter((entry) => (
      ['get_panel_state', 'get_secret_envelope', 'save_settings'].includes(entry)
    ));
    check(
      JSON.stringify(relevantEntries.slice(0, 6)) === JSON.stringify([
        'get_panel_state',
        'get_secret_envelope',
        'save_settings',
        'get_secret_envelope',
        'save_settings',
        'get_panel_state',
      ]),
      `unexpected save sequence: ${JSON.stringify(relevantEntries)}`,
    );
    check(
      saveArgs[0].key_id === freshOne.envelope.key_id,
      `first save did not use the click-time envelope: ${saveArgs[0].key_id}`,
    );
    check(
      saveArgs[1].key_id === freshTwo.envelope.key_id,
      `retry did not use a refreshed envelope: ${saveArgs[1].key_id}`,
    );
    check(
      saveArgs.every((args) => args.key_id !== old.envelope.key_id),
      'a save reused the envelope returned when the page was opened',
    );
    const decrypted = await decryptSaveArgs(
      saveArgs[1],
      keyPairs.get(saveArgs[1].key_id),
    );
    check(decrypted.api_key === SECRET, 'the replacement credential was not encrypted');
    check(
      decrypted.model === 'edited-model-before-save',
      `unexpected encrypted non-secret setting: ${decrypted.model}`,
    );
    check(
      elements.get('model').value === 'persisted-model-after-refresh',
      `the form was not refreshed from persisted settings: ${elements.get('model').value}`,
    );
    check(elements.get('apiKey').value === '', 'the API key input was not cleared');
    check(
      elements.get('credentialValue').textContent.includes('已配置'),
      `credential status is not configured: ${elements.get('credentialValue').textContent}`,
    );
    check(
      elements.get('clearKeyButton').disabled === false,
      'the explicit clear-key action is unavailable after saving',
    );
  } else {
    await waitFor(
      () => (
        saveCallCount >= 1
        && toastMessages.some((message) => message.includes(expectedOrdinaryError))
      ),
      'readable ordinary save error',
    );
    await new Promise((resolve) => realSetTimeout(resolve, 30));
    check(envelopeCallCount === 1, `ordinary error refreshed envelope: ${envelopeCallCount}`);
    check(saveCallCount === 1, `ordinary error retried save: ${saveCallCount}`);
    check(
      saveArgs[0].key_id === freshOne.envelope.key_id,
      `ordinary-error save did not use a fresh envelope: ${saveArgs[0].key_id}`,
    );
    check(
      toastMessages.some((message) => message.includes(expectedOrdinaryError)),
      `ordinary error was not shown: ${JSON.stringify(toastMessages)}`,
    );
  }

  check(
    postBodies.every((body) => !body.includes(SECRET)),
    'a /runs request contained the plaintext API key',
  );
  check(
    saveArgs.every((args) => (
      args
      && typeof args.encrypted_payload === 'string'
      && Object.keys(args).sort().join(',') === 'encrypted_payload,key_id'
    )),
    `save args escaped the encrypted-only boundary: ${JSON.stringify(saveArgs)}`,
  );
  check(
    toastMessages.every((message) => !message.includes('[object Object]')),
    `an object error leaked into the toast: ${JSON.stringify(toastMessages)}`,
  );

  process.stdout.write(JSON.stringify({
    scenario,
    entryCalls,
    envelopeCallCount,
    saveCallCount,
    toastMessages,
    model: elements.get('model').value,
    apiKey: elements.get('apiKey').value,
    credential: elements.get('credentialValue').textContent,
  }));
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
"""


def _run_panel_scenario(
    tmp_path: Path,
    scenario: str,
    *,
    expected_error: str = "",
) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    panel_script_path = tmp_path / "image-generator-panel.js"
    panel_script_path.write_text(_extract_panel_script(), encoding="utf-8")
    harness_path = tmp_path / "panel-harness.cjs"
    harness_path.write_text(NODE_HARNESS, encoding="utf-8")
    env = {
        **os.environ,
        "PANEL_SCRIPT_PATH": str(panel_script_path),
        "PANEL_SCENARIO": scenario,
        "EXPECTED_ORDINARY_ERROR": expected_error,
    }
    completed = subprocess.run(
        [node, str(harness_path)],
        cwd=PANEL_HTML.parent,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout)


def test_save_uses_fresh_envelopes_retries_once_and_refreshes_panel(
    tmp_path: Path,
) -> None:
    result = _run_panel_scenario(tmp_path, "retry_success")

    assert result["envelopeCallCount"] == 2
    assert result["saveCallCount"] == 2
    assert result["model"] == "persisted-model-after-refresh"
    assert result["apiKey"] == ""
    assert "已配置" in str(result["credential"])


@pytest.mark.parametrize(
    ("scenario", "expected_error"),
    [
        ("detail_error", "服务端拒绝了当前模型配置"),
        ("error_error", "provider rejected this model"),
    ],
)
def test_non_envelope_object_errors_are_readable_and_not_retried(
    tmp_path: Path,
    scenario: str,
    expected_error: str,
) -> None:
    result = _run_panel_scenario(
        tmp_path,
        scenario,
        expected_error=expected_error,
    )

    assert result["envelopeCallCount"] == 1
    assert result["saveCallCount"] == 1
    assert any(expected_error in str(message) for message in result["toastMessages"])
