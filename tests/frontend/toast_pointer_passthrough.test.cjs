const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const templatePath = path.resolve(__dirname, '../../templates/toast.html');
const html = fs.readFileSync(templatePath, 'utf8');
const inlineScript = Array.from(html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g))
  .map((match) => match[1])
  .find((source) => source.includes('// ===== Toast API'));

function createClassList() {
  const names = new Set();
  return {
    add(...values) { values.forEach((value) => names.add(value)); },
    remove(...values) { values.forEach((value) => names.delete(value)); },
    contains(value) { return names.has(value); },
  };
}

function createElement(tagName = 'div') {
  const listeners = new Map();
  const attributes = new Map();
  const element = {
    id: '',
    tagName: tagName.toUpperCase(),
    nodeType: 1,
    parentNode: null,
    childNodes: [],
    classList: createClassList(),
    style: { cssText: '' },
    textContent: '',
    innerHTML: '',
    disabled: false,
    tabIndex: 0,
    appendChild(child) {
      child.parentNode = this;
      this.childNodes.push(child);
      return child;
    },
    remove() {
      if (!this.parentNode) return;
      this.parentNode.childNodes = this.parentNode.childNodes.filter((child) => child !== this);
      this.parentNode = null;
    },
    querySelector(selector) {
      if (!selector.startsWith('#')) return null;
      return findElement(this, (child) => child.id === selector.slice(1));
    },
    setAttribute(name, value) { attributes.set(name, String(value)); },
    removeAttribute(name) { attributes.delete(name); },
    addEventListener(type, listener) { listeners.set(type, listener); },
    removeEventListener(type, listener) {
      if (listeners.get(type) === listener) listeners.delete(type);
    },
    dispatch(type, event = {}) {
      const listener = listeners.get(type);
      if (listener) listener(event);
    },
    matches() { return false; },
    focus() { document.activeElement = this; },
    blur() { if (document.activeElement === this) document.activeElement = null; },
    getBoundingClientRect() {
      return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 };
    },
    _attributes: attributes,
  };
  return element;
}

function findElement(root, predicate) {
  for (const child of root.childNodes) {
    if (predicate(child)) return child;
    const nested = findElement(child, predicate);
    if (nested) return nested;
  }
  return null;
}

let document = null;

function createDeferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function createHarness() {
  const callbacks = {};
  const mouseThrough = [];
  const timers = new Map();
  const windowListeners = new Map();
  const documentListeners = new Map();
  let nextTimerId = 0;
  let cursorPoint = { x: 20, y: 20 };
  let cursorProvider = () => Promise.resolve(cursorPoint);

  const body = createElement('body');
  const head = createElement('head');
  const statusToast = createElement('div');
  statusToast.id = 'status-toast';
  statusToast.getBoundingClientRect = () => ({
    left: 100,
    top: 40,
    right: 300,
    bottom: 100,
    width: 200,
    height: 60,
  });
  body.appendChild(statusToast);

  document = {
    body,
    head,
    hidden: false,
    activeElement: null,
    createElement,
    getElementById(id) {
      if (statusToast.id === id) return statusToast;
      return findElement(body, (element) => element.id === id)
        || findElement(head, (element) => element.id === id);
    },
    querySelector(selector) {
      if (selector === 'style[data-toast-close]') {
        return findElement(head, (element) => (
          element.tagName === 'STYLE' && element._attributes.has('data-toast-close')
        ));
      }
      return null;
    },
    addEventListener(type, listener) { documentListeners.set(type, listener); },
  };

  const api = {
    supportsStatusPointerTracking: true,
    getCursorPoint() { return cursorProvider(); },
    setMouseThrough(ignore) { mouseThrough.push(ignore); },
    onShowStatusToast(callback) { callbacks.status = callback; },
    onShowVoicePreparing(callback) { callbacks.voicePreparing = callback; },
    onHideVoicePreparing(callback) { callbacks.voiceHide = callback; },
    onShowReadyToSpeak(callback) { callbacks.voiceReady = callback; },
    onShowProminentNotice(callback) { callbacks.prominent = callback; },
  };
  const window = {
    nekoToastAPI: api,
    t(_key, options) { return options.defaultValue; },
    addEventListener(type, listener) { windowListeners.set(type, listener); },
    removeEventListener() {},
  };

  vm.runInNewContext(inlineScript, {
    window,
    document,
    console: { log() {} },
    Promise,
    Date,
    Number,
    Array,
    Object,
    String,
    JSON,
    Math,
    setTimeout(callback, delay) {
      nextTimerId += 1;
      timers.set(nextTimerId, { callback, delay });
      return nextTimerId;
    },
    clearTimeout(id) { timers.delete(id); },
  }, { filename: templatePath });

  return {
    statusToast,
    mouseThrough,
    setCursor(point) { cursorPoint = point; },
    setCursorProvider(provider) { cursorProvider = provider; },
    emitStatus(message = 'saved') { callbacks.status({ message, duration: 3000 }); },
    emitProminent(message = 'important') { callbacks.prominent({ message }); },
    runTimer(delay) {
      const match = Array.from(timers.entries()).find(([, timer]) => timer.delay === delay);
      assert.ok(match, `expected a pending ${delay}ms timer`);
      timers.delete(match[0]);
      match[1].callback();
    },
    hasTimer(delay) {
      return Array.from(timers.values()).some((timer) => timer.delay === delay);
    },
    getProminentButton() {
      const overlay = document.getElementById('prominent-notice-overlay');
      assert.ok(overlay);
      return findElement(overlay, (element) => element.tagName === 'BUTTON');
    },
    dispatchBeforeUnload() { windowListeners.get('beforeunload')(); },
  };
}

async function flushPromises() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

test('ordinary status toast only captures input while the cursor is inside its rectangle', async () => {
  const harness = createHarness();

  harness.emitStatus();
  harness.runTimer(10);
  await flushPromises();
  assert.deepEqual(harness.mouseThrough, [true]);

  harness.setCursor({ x: 150, y: 60 });
  harness.runTimer(50);
  await flushPromises();
  assert.deepEqual(harness.mouseThrough, [true, false]);

  harness.setCursor({ x: 20, y: 20 });
  harness.runTimer(50);
  await flushPromises();
  assert.deepEqual(harness.mouseThrough, [true, false, true]);

  harness.setCursor({ x: 150, y: 60 });
  harness.runTimer(50);
  await flushPromises();
  assert.deepEqual(harness.mouseThrough, [true, false, true, false]);

  const closeButton = harness.statusToast.querySelector('#status-toast-close');
  closeButton.onclick({ stopPropagation() {} });
  assert.deepEqual(harness.mouseThrough, [true, false, true, false, true]);
  assert.equal(harness.hasTimer(50), false);
});

test('stale cursor results cannot make a prominent notice mouse-through', async () => {
  const harness = createHarness();
  const pendingCursor = createDeferred();
  harness.setCursorProvider(() => pendingCursor.promise);

  harness.emitStatus();
  harness.runTimer(10);
  assert.deepEqual(harness.mouseThrough, [true]);

  harness.emitProminent('first');
  harness.emitProminent('second');
  assert.deepEqual(harness.mouseThrough, [true, false]);

  pendingCursor.resolve({ x: 20, y: 20 });
  await flushPromises();
  assert.deepEqual(harness.mouseThrough, [true, false]);
  assert.equal(harness.hasTimer(50), false);

  harness.getProminentButton().dispatch('click');
  harness.runTimer(200);
  assert.deepEqual(harness.mouseThrough, [true, false]);

  harness.getProminentButton().dispatch('click');
  harness.runTimer(200);
  await flushPromises();
  assert.deepEqual(harness.mouseThrough, [true, false, true]);
});

test('page teardown force-restores desktop passthrough', () => {
  const harness = createHarness();

  harness.emitProminent();
  assert.deepEqual(harness.mouseThrough, [false]);
  harness.dispatchBeforeUnload();
  assert.deepEqual(harness.mouseThrough, [false, true]);
});
