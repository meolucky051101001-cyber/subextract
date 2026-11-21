'use strict';

// Offline regression suite: no service, browser, npm packages, or real timers.
// Run from any directory: node --test data/test/cue_editor_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.resolve(__dirname, '../../app/static/app.js');
const tick = () => new Promise((resolve) => setImmediate(resolve));

class Element {
  constructor(tag, document) {
    this.tagName = tag.toUpperCase();
    this.ownerDocument = document;
    this.children = [];
    this.parentElement = null;
    this.className = '';
    this.dataset = {};
    this.style = {};
    this.attributes = {};
    this.value = '';
    this.textContent = '';
    this.disabled = false;
    this.scrollHeight = 20;
    this.listeners = new Map();
    this.classList = {
      contains: (name) => this.className.split(/\s+/).includes(name),
      add: (...names) => {
        this.className = [...new Set([...this.className.split(/\s+/).filter(Boolean), ...names])].join(' ');
      },
      remove: (...names) => {
        this.className = this.className.split(/\s+/).filter((name) => !names.includes(name)).join(' ');
      },
      toggle: (name, force) => {
        const add = force === undefined ? !this.classList.contains(name) : force;
        this.classList[add ? 'add' : 'remove'](name);
        return add;
      },
    };
  }
  set innerHTML(value) {
    this._innerHTML = value;
    this.children.forEach((child) => { child.parentElement = null; });
    this.children = [];
  }
  get innerHTML() { return this._innerHTML || ''; }
  appendChild(child) {
    if (child.tagName === '#FRAGMENT') {
      [...child.children].forEach((item) => this.appendChild(item));
      child.children = [];
      return child;
    }
    child.remove();
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
  append(...children) { children.forEach((child) => this.appendChild(child)); }
  remove() {
    if (this.parentElement) {
      this.parentElement.children = this.parentElement.children.filter((child) => child !== this);
      this.parentElement = null;
    }
  }
  matches(selector) {
    const tag = selector.match(/^[a-z]+/i);
    if (tag && this.tagName !== tag[0].toUpperCase()) return false;
    for (const match of selector.matchAll(/\.([\w-]+)/g)) {
      if (!this.classList.contains(match[1])) return false;
    }
    for (const match of selector.matchAll(/\[data-([\w-]+)="([^"]*)"\]/g)) {
      if (this.dataset[match[1]] !== match[2]) return false;
    }
    return true;
  }
  closest(selector) {
    return this.matches(selector) ? this : this.parentElement?.closest(selector) || null;
  }
  querySelectorAll(selector) {
    const [first, ...remaining] = selector.trim().split(/\s+/);
    const result = [];
    for (const child of this.children) {
      if (child.matches(first)) {
        if (remaining.length) result.push(...child.querySelectorAll(remaining.join(' ')));
        else result.push(child);
      }
      result.push(...child.querySelectorAll(selector));
    }
    return [...new Set(result)];
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(name, listener) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(listener);
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] || null; }
  removeAttribute(name) { delete this.attributes[name]; }
  getBoundingClientRect() { return { x: 0, y: 0, left: 0, top: 0, width: 640, height: 360 }; }
  click() {
    if (this.tagName === 'A') this.ownerDocument.downloads.push(this.href);
    for (const listener of this.listeners.get('click') || []) listener({ target: this });
  }
  load() {}
  pause() {}
  play() { return Promise.resolve(); }
  focus() {}
  scrollIntoView() {}
  setSelectionRange() {}
}

function fakeDocument() {
  const ids = new Map();
  const document = {
    downloads: [],
    createElement(tag) { return new Element(tag, document); },
    createDocumentFragment() { return new Element('#fragment', document); },
    querySelector(selector) {
      if (/^#[\w-]+$/.test(selector)) {
        if (!ids.has(selector)) ids.set(selector, document.createElement('div'));
        return ids.get(selector);
      }
      for (const element of ids.values()) {
        const found = element.querySelector(selector);
        if (found) return found;
      }
      return document.body.querySelector(selector);
    },
    querySelectorAll(selector) { return document.body.querySelectorAll(selector); },
    addEventListener() {},
  };
  document.body = document.createElement('body');
  return document;
}

function fakeClock() {
  let now = 0;
  let sequence = 0;
  const timers = new Map();
  return {
    setTimeout(callback, delay = 0) {
      const id = ++sequence;
      timers.set(id, { callback, at: now + delay });
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
    async advance(milliseconds) {
      const end = now + milliseconds;
      while (true) {
        const next = [...timers.entries()]
          .filter(([, timer]) => timer.at <= end)
          .sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
        if (!next) break;
        const [id, timer] = next;
        now = timer.at;
        timers.delete(id);
        timer.callback();
        await tick();
      }
      now = end;
    },
  };
}

function job(id = 'job-a', cues = [{ id: 'cue-a', start: 1, end: 2, text: '  original source  ', trans: '', region: 0 }]) {
  return {
    id, filename: id + '.mp4', status: 'done', cues,
    params: { translate_to: 'vi' },
    stats: { untranslated_indices: [0] },
  };
}

function harness() {
  const document = fakeDocument();
  const clock = fakeClock();
  const requests = [];
  const storage = new Map();
  const sandbox = {
    document, console,
    EventSource: class { close() {} },
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    requestAnimationFrame: clock.setTimeout,
    localStorage: {
      setItem: (key, value) => storage.set(key, value),
      getItem: (key) => storage.get(key) || null,
      removeItem: (key) => storage.delete(key),
    },
    window: { confirm: () => true, addEventListener() {} },
    fetch(url, options = {}) {
      let resolve;
      let reject;
      const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
      const request = {
        url: String(url), method: options.method || 'GET',
        body: options.body ? JSON.parse(options.body) : null,
        settled: false,
        respond(status = 200, data = {}) {
          assert.equal(request.settled, false, 'fake response was already settled');
          request.settled = true;
          resolve({ ok: status >= 200 && status < 300, status, json: async () => data });
        },
        reject(error = new Error('offline transport failure')) {
          assert.equal(request.settled, false, 'fake response was already settled');
          request.settled = true;
          reject(error);
        },
      };
      requests.push(request);
      return promise;
    },
  };
  const source = fs.readFileSync(APP, 'utf8');
  assert.match(source, /\binit\(\);\s*$/, 'only the final init call is removed');
  vm.createContext(sandbox);
  vm.runInContext(source.replace(/\binit\(\);\s*$/, ''), sandbox, { filename: APP });
  const api = vm.runInContext(
    '({ state, els, openJob, resetWorkspace, saveCues, markDirty, handleCueInput, '
      + 'exportCues, sendHandoff, renderHandoff, retranslateCue, retranslateMissing, onJobFinished, pollJob, pollHandoff, startJob, bindEvents, '
      + 'addCue, deleteCue, handleCueBlur, undoEdit, splitCue, mergeCue, replaceAllInCues, requestPreview, pollPreview, checkToolV1Connection })', sandbox);
  api.openJob(job(), false);
  function edit(field, value, index = 0) {
    const row = api.els.cueList.querySelector('.cue[data-index="' + index + '"]');
    assert.ok(row, 'cue row exists');
    const selector = field === 'trans' ? '.cue-trans'
      : field === 'text' ? '.cue-text' : '.time-input[data-field="' + field + '"]';
    const target = row.querySelector(selector);
    assert.ok(target, 'editable field exists');
    target.value = value;
    api.handleCueInput({ target });
  }
  return { api, document, clock, requests, edit, window: sandbox.window };
}

const actions = {
  export: (h) => h.api.exportCues('srt'),
  handoff: (h) => h.api.sendHandoff(),
  retranslate: (h) => h.api.retranslateCue(0, h.api.els.retransAllBtn),
  retranslateMissing: (h) => h.api.retranslateMissing(),
};

function assertRequest(h, index, method, suffix) {
  const request = h.requests[index];
  assert.ok(request, 'request ' + index + ' exists');
  assert.equal(request.method, method);
  assert.equal(request.url, '/api/jobs/job-a/' + suffix);
  return request;
}

for (const [name, action] of Object.entries(actions)) {
  for (const failure of ['http', 'network']) {
    test(name + ' stops when the cue PUT fails (' + failure + ')', async () => {
      const h = harness();
      h.edit('text', 'unsaved edit');
      const pending = action(h);
      const put = assertRequest(h, 0, 'PUT', 'cues');
      if (failure === 'http') put.respond(500, { detail: 'disk full' });
      else put.reject();
      await pending;
      await tick();
      assert.equal(h.requests.length, 1, 'no dependent POST follows a failed save');
      assert.deepEqual(h.document.downloads, [], 'no download starts');
      assert.equal(h.api.state.cues[0].text, 'unsaved edit');
      assert.equal(h.api.els.saveState.textContent, 'Lưu thất bại');
      assert.equal(h.api.state.handoffSending, false);
      assert.equal(h.api.state.retranslating, false);
    });
  }
}

test('a failed PUT remains dirty and export retries it before downloading', async () => {
  const h = harness();
  h.edit('text', 'retry this edit');
  const failed = h.api.exportCues('srt');
  assertRequest(h, 0, 'PUT', 'cues').respond(503);
  await failed;
  const retry = h.api.exportCues('vtt');
  assert.equal(h.document.downloads.length, 0);
  const put = assertRequest(h, 1, 'PUT', 'cues');
  assert.equal(put.body.cues[0].text, 'retry this edit');
  put.respond();
  await retry;
  assert.equal(h.document.downloads.length, 1);
  assert.match(h.document.downloads[0], /\/job-a\/export\?format=vtt&/);
});

test('debounced edits wait 700ms and save the latest snapshot', async () => {
  const h = harness();
  h.edit('text', 'first');
  await h.clock.advance(400);
  h.edit('text', 'latest');
  await h.clock.advance(699);
  assert.equal(h.requests.length, 0);
  await h.clock.advance(1);
  const put = assertRequest(h, 0, 'PUT', 'cues');
  assert.equal(put.body.cues[0].text, 'latest');
  put.respond();
  await tick();
  assert.equal(h.api.els.saveState.textContent, 'Đã lưu');
});

test('edits during PUT are serialized; concurrent export waits for the newest PUT', async () => {
  const h = harness();
  h.edit('text', 'first snapshot');
  const saving = h.api.saveCues();
  const first = assertRequest(h, 0, 'PUT', 'cues');
  h.edit('text', 'second snapshot');
  const exporting = h.api.exportCues('srt');
  await h.clock.advance(700);
  assert.equal(h.requests.length, 1, 'PUTs do not overlap');
  assert.equal(first.body.cues[0].text, 'first snapshot', 'request body is immutable');
  assert.equal(h.document.downloads.length, 0);
  first.respond();
  await tick();
  const second = assertRequest(h, 1, 'PUT', 'cues');
  assert.equal(second.body.cues[0].text, 'second snapshot');
  assert.equal(second.body.cues[0].id, first.body.cues[0].id);
  assert.equal(h.document.downloads.length, 0, 'first PUT alone cannot release export');
  second.respond();
  assert.equal(await saving, true);
  await exporting;
  assert.equal(h.document.downloads.length, 1);
  assert.equal(await h.api.saveCues(), true);
  assert.equal(h.requests.length, 2, 'clean editor does not send an extra PUT');
});

test('double handoff click shares one save and sends one POST', async () => {
  const h = harness();
  h.edit('text', 'send this');
  const first = h.api.sendHandoff();
  const second = h.api.sendHandoff();
  assert.equal(h.api.els.handoffBtn.disabled, true, 'busy guard is set before awaiting save');
  assert.equal(h.requests.length, 1);
  assertRequest(h, 0, 'PUT', 'cues').respond();
  await tick();
  const post = assertRequest(h, 1, 'POST', 'handoff');
  assert.equal(h.requests.length, 2);
  post.respond(200, { handoff: { id: 'handoff-a', status: 'done' } });
  await Promise.all([first, second]);
  assert.equal(h.requests.length, 2);
  assert.equal(h.api.state.handoffSending, false);
  assert.equal(h.api.els.handoffBtn.disabled, false);
});

for (const [name, action] of Object.entries(actions)) {
  for (const nextId of ['job-b', 'job-a']) {
    test(name + ' abandons the pending action when opening ' + nextId + ' in a new session', async () => {
      const h = harness();
      h.edit('text', 'old session edit');
      const pending = action(h);
      const oldPut = assertRequest(h, 0, 'PUT', 'cues');
      h.api.openJob(job(nextId, [{ id: 'cue-new', start: 5, end: 6, text: 'new session', trans: '', region: 1 }]), false);
      const saveLabel = h.api.els.saveState.textContent;
      const toast = h.api.els.toast.textContent;
      oldPut.respond();
      await pending;
      await tick();
      assert.equal(h.requests.length, 1, 'old action cannot send a POST for either session');
      assert.equal(h.document.downloads.length, 0);
      assert.equal(h.api.state.job.id, nextId);
      assert.equal(h.api.state.cues[0].text, 'new session');
      assert.equal(h.api.state.cues[0].trans, '');
      assert.equal(h.api.els.saveState.textContent, saveLabel, 'old PUT cannot update current save status');
      assert.equal(h.api.els.toast.textContent, toast, 'old completion cannot show a success toast');
    });
  }
}

test('resetting the workspace while export waits abandons the download', async () => {
  const h = harness();
  const pending = h.api.exportCues('srt');
  const put = assertRequest(h, 0, 'PUT', 'cues');
  h.api.resetWorkspace();
  put.respond();
  await pending;
  assert.equal(h.api.state.job, null);
  assert.equal(h.document.downloads.length, 0);
  assert.equal(h.requests.length, 1);
});

test('translation uses stable cue_id and a preview, then PUTs the accepted translation', async () => {
  const h = harness();
  const pending = actions.retranslate(h);
  assertRequest(h, 0, 'PUT', 'cues').respond();
  await tick();
  const post = assertRequest(h, 1, 'POST', 'retranslate');
  assert.equal(post.body.cue_id, 'cue-a');
  assert.equal(post.body.persist, false);
  assert.equal(post.body.text, 'original source', 'preview uses trimmed source');
  assert.equal(post.body.target, 'vi');
  assert.equal(Object.hasOwn(post.body, 'index'), false, 'mutable display index is not sent');
  post.respond(200, { ok: true, cue_id: 'cue-a', trans: 'Bản dịch mới' });
  await tick();
  const put = assertRequest(h, 2, 'PUT', 'cues');
  assert.equal(put.body.cues[0].id, 'cue-a');
  assert.equal(put.body.cues[0].trans, 'Bản dịch mới');
  assert.equal(h.api.els.retransAllBtn.disabled, true, 'translation waits for persistence');
  put.respond();
  await pending;
  assert.equal(h.api.state.cues[0].trans, 'Bản dịch mới');
  assert.equal(h.api.state.untranslated.length, 0);
  assert.equal(h.requests.length, 3);
});

for (const [field, value] of [['text', 'new source'], ['trans', 'manual translation'], ['start', '1.25']]) {
  test('translation response is discarded after editing ' + field, async () => {
    const h = harness();
    const pending = actions.retranslate(h);
    assertRequest(h, 0, 'PUT', 'cues').respond();
    await tick();
    const post = assertRequest(h, 1, 'POST', 'retranslate');
    h.edit(field, value);
    post.respond(200, { ok: true, cue_id: 'cue-a', trans: 'STALE TRANSLATION' });
    await tick();
    assert.equal(h.requests.length, 2, 'stale preview does not cause a PUT');
    await pending;
    assert.equal(h.api.state.cues[0].trans, field === 'trans' ? value : '');
    assert.equal(h.api.state.untranslated.length, field === 'trans' ? 0 : 1,
      'a manual translation resolves the missing warning even if a preview is discarded');
    assert.doesNotMatch(h.api.els.toast.textContent, /^Đã dịch lại cue$/);
  });
}

test('translation response cannot change a replacement cue at the same index', async () => {
  const h = harness();
  const pending = actions.retranslate(h);
  assertRequest(h, 0, 'PUT', 'cues').respond();
  await tick();
  const post = assertRequest(h, 1, 'POST', 'retranslate');
  h.api.state.cues[0] = { id: 'replacement', start: 1, end: 2, text: 'replacement source', trans: '' };
  h.api.markDirty();
  post.respond(200, { ok: true, cue_id: 'cue-a', trans: 'STALE TRANSLATION' });
  await tick();
  assert.equal(h.requests.length, 2);
  await pending;
  assert.equal(h.api.state.cues[0].id, 'replacement');
  assert.equal(h.api.state.cues[0].trans, '');
});

test('translation response cannot update a newly opened session', async () => {
  const h = harness();
  const pending = actions.retranslate(h);
  assertRequest(h, 0, 'PUT', 'cues').respond();
  await tick();
  const post = assertRequest(h, 1, 'POST', 'retranslate');
  h.api.openJob(job('job-b'), false);
  const toast = h.api.els.toast.textContent;
  post.respond(200, { ok: true, cue_id: 'cue-a', trans: 'STALE TRANSLATION' });
  await tick();
  assert.equal(h.requests.length, 2);
  await pending;
  assert.equal(h.api.state.job.id, 'job-b');
  assert.equal(h.api.state.cues[0].trans, '');
  assert.equal(h.api.els.toast.textContent, toast);
});

test('late onJobFinished result cannot replace job B or its pending edits', async () => {
  const h = harness();
  const pending = h.api.onJobFinished({ id: 'job-a', status: 'done' });
  const get = h.requests[0];
  assert.equal(get.method, 'GET');
  assert.equal(get.url, '/api/jobs/job-a');
  h.api.openJob(job('job-b', [{ id: 'cue-b', start: 8, end: 9, text: 'source B', trans: '', region: 1 }]), false);
  h.edit('text', 'unsaved edit for B');
  const currentJob = h.api.state.job;
  const currentCues = h.api.state.cues;
  const currentSave = h.api.state.cueSave;
  const toast = h.api.els.toast.textContent;
  const stage = h.api.els.stageLabel.textContent;
  get.respond(200, job('job-a', [{ id: 'stale-a', start: 1, end: 2, text: 'STALE A RESULT', trans: 'stale translation' }]));
  await pending;
  assert.equal(h.api.state.job, currentJob);
  assert.equal(h.api.state.cues, currentCues);
  assert.equal(h.api.state.cueSave, currentSave, 'old result cannot reset B save queue');
  assert.equal(h.api.state.cues[0].text, 'unsaved edit for B');
  assert.equal(h.api.els.saveState.textContent, 'Chưa lưu…');
  assert.equal(h.api.els.toast.textContent, toast);
  assert.equal(h.api.els.stageLabel.textContent, stage);
  assert.equal(h.requests.length, 1);
});

test('late poll response cannot update job B or schedule more work for job A', async () => {
  const h = harness();
  h.api.state.running = true;
  h.api.pollJob();
  await h.clock.advance(800);
  const get = h.requests[0];
  assert.equal(get.method, 'GET');
  assert.equal(get.url, '/api/jobs/job-a');
  h.api.openJob(job('job-b'), false);
  const stage = h.api.els.stageLabel.textContent;
  const progress = h.api.els.progressPct.textContent;
  const toast = h.api.els.toast.textContent;
  get.respond(200, { id: 'job-a', status: 'done', stage: 'ocr', progress: 0.37, message: 'STALE POLL' });
  await tick();
  assert.equal(h.requests.length, 1, 'stale completion cannot trigger a result GET');
  assert.equal(h.api.state.job.id, 'job-b');
  assert.equal(h.api.state.cues[0].text, '  original source  ');
  assert.equal(h.api.els.stageLabel.textContent, stage);
  assert.equal(h.api.els.progressPct.textContent, progress);
  assert.equal(h.api.els.toast.textContent, toast);
  await h.clock.advance(3000);
  assert.equal(h.requests.length, 1, 'old poll cannot schedule a retry');
});

function configureStart(h) {
  h.api.els.langEn.checked = true;
  h.api.els.fpsSelect.value = '8';
  h.api.els.confRange.value = '75';
  h.api.state.geminiConfigured = true;
}

for (const status of [400, 503]) {
  test('rejected start (' + status + ') preserves existing cues and undo before choosing a new video', async () => {
    const h = harness();
    configureStart(h);
    h.api.bindEvents();
    const undo = [{
      cues: h.api.state.cues.map((cue) => ({ ...cue })),
      untranslated: [0], label: 'previous edit',
    }];
    h.api.state.undoStack = undo;
    h.edit('text', 'keep this edited subtitle');
    h.edit('trans', 'keep this manual translation');
    const cues = h.api.state.cues;
    const untranslated = h.api.state.untranslated;
    const pending = h.api.startJob();
    const put = assertRequest(h, 0, 'PUT', 'cues');
    assert.equal(put.body.cues.length, 1);
    put.respond();
    await pending;
    const start = assertRequest(h, 1, 'POST', 'start');
    assert.equal(h.api.state.running, true);
    assert.equal(h.api.state.cues, cues, 'keep the old result while start is pending');
    start.respond(status, { detail: 'start rejected by server' });
    await tick();
    assert.equal(h.api.state.running, false);
    assert.equal(h.api.state.cues, cues);
    assert.equal(h.api.state.resultPending, false, 'explicit rejection allows saving the previous result');
    assert.equal(h.api.state.undoStack, undo);
    assert.equal(h.api.state.undoStack[0].label, 'previous edit');
    assert.equal(h.api.state.untranslated, untranslated);
    assert.equal(h.api.state.showTrans, true);
    assert.equal(h.api.els.resultCard.classList.contains('hidden'), false);
    assert.equal(h.api.els.cueList.querySelector('.cue-text').value, 'keep this edited subtitle');
    assert.equal(h.api.els.undoBtn.disabled, false);
    assert.equal(h.api.els.progressMsg.textContent, 'start rejected by server');
    h.api.els.newVideoBtn.click();
    await tick();
    const leavePut = assertRequest(h, 2, 'PUT', 'cues');
    assert.equal(leavePut.body.cues.length, 1, 'choosing a new video must never erase the rejected result');
    assert.equal(leavePut.body.cues[0].text, 'keep this edited subtitle');
    assert.equal(leavePut.body.cues[0].trans, 'keep this manual translation');
    assert.equal(leavePut.body.cues[0].id, 'cue-a');
    assert.equal(h.api.state.job.id, 'job-a', 'new video waits for saving the old result');
    leavePut.respond();
    await tick();
    assert.equal(h.api.state.job, null);
    assert.equal(h.requests.length, 3);
  });
}

test('start does not POST or clear existing cues when its initial PUT fails', async () => {
  const h = harness();
  configureStart(h);
  h.edit('text', 'unsaved subtitle must survive');
  const cues = h.api.state.cues;
  const pending = h.api.startJob();
  assertRequest(h, 0, 'PUT', 'cues').respond(500, { detail: 'save failed' });
  await pending;
  await tick();
  assert.equal(h.requests.length, 1, 'no POST /start follows a failed cue PUT');
  assert.equal(h.api.state.running, false);
  assert.equal(h.api.state.cues, cues);
  assert.equal(h.api.state.cues[0].text, 'unsaved subtitle must survive');
  assert.equal(h.api.els.resultCard.classList.contains('hidden'), false);
  assert.equal(h.api.els.saveState.textContent, 'Lưu thất bại');
});

for (const status of [200, 503]) {
  test('pending result GET (' + status + ') blocks empty saves and dependent editor actions', async () => {
    const h = harness();
    h.api.bindEvents();
    h.window.confirm = () => false;
    h.api.state.cues = [];
    h.api.state.running = true;
    const pending = h.api.onJobFinished({ id: 'job-a', status: 'done' });
    const get = h.requests[0];
    assert.equal(get.method, 'GET');
    assert.equal(get.url, '/api/jobs/job-a');
    assert.equal(h.api.state.resultPending, true);
    assert.equal(h.api.state.running, true, 'run stays busy while its cues load');
    assert.equal(await h.api.saveCues(), false);
    await h.api.exportCues('srt');
    await h.api.sendHandoff();
    h.api.els.newVideoBtn.click();
    await tick();
    assert.equal(h.requests.length, 1, 'no PUT or handoff POST before result GET completes');
    assert.equal(h.document.downloads.length, 0);
    assert.equal(h.api.state.job.id, 'job-a');
    const result = job('job-a', [{ id: 'loaded-cue', start: 3, end: 4, text: 'loaded result', trans: 'bản dịch' }]);
    get.respond(status, result);
    await pending;
    assert.equal(h.api.state.running, false);
    if (status === 200) {
      assert.equal(h.api.state.resultPending, false);
      assert.equal(h.api.state.cues[0].id, 'loaded-cue');
      const saving = h.api.saveCues();
      const put = assertRequest(h, 1, 'PUT', 'cues');
      assert.equal(put.body.cues.length, 1);
      assert.equal(put.body.cues[0].id, 'loaded-cue');
      assert.equal(put.body.cues[0].text, 'loaded result');
      put.respond();
      assert.equal(await saving, true);
    } else {
      assert.equal(h.api.state.resultPending, true, 'failed GET continues blocking destructive empty saves');
      assert.equal(await h.api.saveCues(), false);
      await h.api.exportCues('srt');
      await h.api.sendHandoff();
      h.api.els.newVideoBtn.click();
      await tick();
      assert.equal(h.requests.length, 1, 'failed result fetch cannot release a PUT with empty cues');
      assert.equal(h.api.state.job, null, 'new video safely resets after a failed result GET');
      assert.equal(h.api.state.resultPending, false, 'reset clears the pending result guard');
    }
  });
}

for (const outcome of ['network', 409]) {
  test('uncertain start outcome (' + outcome + ') blocks old-cue PUTs and permits safe video reset', async () => {
  const h = harness();
  configureStart(h);
  h.api.bindEvents();
  h.edit('text', 'previous run subtitle');
  const previousCues = h.api.state.cues;
  const pending = h.api.startJob();
  assertRequest(h, 0, 'PUT', 'cues').respond();
  await pending;
  const start = assertRequest(h, 1, 'POST', 'start');
  // A lost reply or a 409 may mean the server is already processing newer cues.
  if (outcome === 'network') start.reject(new Error('connection lost after server accepted the run'));
  else start.respond(409, { detail: 'job already running' });
  await tick();
  assert.equal(h.api.state.running, false);
  assert.equal(h.api.state.resultPending, true, 'unknown server state must require reconciliation');
  assert.equal(h.api.state.cues, previousCues, 'old result stays available locally');
  assert.match(h.api.els.toast.textContent, /Tải lại trang/);
  assert.equal(await h.api.saveCues(), false);
  await h.api.exportCues('srt');
  await h.api.sendHandoff();
  assert.equal(h.requests.length, 2, 'old cues cannot overwrite a possibly newer server result');
  assert.equal(h.document.downloads.length, 0);
  h.api.els.newVideoBtn.click();
  await tick();
  assert.equal(h.api.state.job, null, 'switching video safely abandons the uncertain run');
  assert.equal(h.api.state.resultPending, false);
  assert.equal(h.requests.length, 2, 'switching must not save cues from the previous run');
  });
}

test('late handoff poll response cannot alter job B or start another poll', async () => {
  const h = harness();
  h.api.pollHandoff(10);
  await h.clock.advance(10);
  const get = assertRequest(h, 0, 'GET', 'handoff');
  const next = job('job-b');
  next.stats.handoff = { id: 'handoff-b', status: 'done', stage: 'B complete', progress: 100 };
  h.api.openJob(next, false);
  const handoff = h.api.state.handoff;
  const stage = h.api.els.handoffStage.textContent;
  const button = h.api.els.handoffBtn.textContent;
  get.respond(200, { handoff: { id: 'handoff-a', status: 'running', stage: 'STALE A', progress: 75 } });
  await tick();
  assert.equal(h.api.state.job.id, 'job-b');
  assert.equal(h.api.state.handoff, handoff);
  assert.equal(h.api.els.handoffStage.textContent, stage);
  assert.equal(h.api.els.handoffBtn.textContent, button);
  assert.equal(h.api.state.handoffFails, 0);
  await h.clock.advance(6000);
  assert.equal(h.requests.length, 1, 'old handoff response cannot schedule polling for B');
});

function mixedJob() {
  const result = job('job-a', [
    { id: 'cue-a', start: 1, end: 2, text: 'source A', trans: 'TRANSLATED A' },
    { id: 'cue-b', start: 3, end: 4, text: 'source B', trans: '' },
    { id: 'cue-c', start: 5, end: 6, text: 'source C', trans: 'MANUAL C' },
  ]);
  result.stats.untranslated_indices = [1];
  return result;
}

function cueField(h, index, selector = '.cue-text') {
  const row = h.api.els.cueList.querySelector('.cue[data-index="' + index + '"]');
  assert.ok(row, 'cue row exists');
  const target = row.querySelector(selector);
  assert.ok(target, 'cue field exists');
  return target;
}

for (const mutation of ['insert blank before B', 'delete A', 'blank A then blur']) {
  test('missing translation follows B after ' + mutation + ' and preserves manual C', async () => {
    const h = harness();
    h.api.openJob(mixedJob(), false);
    if (mutation === 'insert blank before B') {
      h.api.state.activeCue = 0;
      h.api.addCue();
    } else if (mutation === 'delete A') {
      h.api.deleteCue({ target: cueField(h, 0, '.del-btn') });
    } else {
      h.edit('text', '   ', 0);
      h.api.handleCueBlur({ target: cueField(h, 0) });
    }
    assert.deepEqual([...h.api.state.untranslated], ['cue-b']);
    const pending = h.api.retranslateMissing();
    const initial = assertRequest(h, 0, 'PUT', 'cues');
    assert.equal(initial.body.cues.some((cue) => !cue.text.trim()), false, 'blank draft is never persisted');
    initial.respond();
    await tick();
    const post = assertRequest(h, 1, 'POST', 'retranslate');
    assert.equal(post.body.cue_id, 'cue-b');
    assert.equal(post.body.text, 'source B');
    post.respond(200, { ok: true, cue_id: 'cue-b', trans: 'TRANSLATED B' });
    await tick();
    const put = assertRequest(h, 2, 'PUT', 'cues');
    assert.equal(put.body.cues.find((cue) => cue.id === 'cue-c').trans, 'MANUAL C');
    put.respond();
    await pending;
    assert.equal(h.api.state.cues.find((cue) => cue.id === 'cue-b').trans, 'TRANSLATED B');
    assert.equal(h.api.state.cues.find((cue) => cue.id === 'cue-c').trans, 'MANUAL C');
    assert.equal(h.api.els.transWarn.classList.contains('hidden'), true);
    assert.equal(h.requests.length, 3, 'only B is translated');
  });
}

test('failed missing translation retains the B warning after rows shift', async () => {
  const h = harness();
  h.api.openJob(mixedJob(), false);
  h.edit('text', '', 0);
  h.api.handleCueBlur({ target: cueField(h, 0) });
  const pending = h.api.retranslateMissing();
  assertRequest(h, 0, 'PUT', 'cues').respond();
  await tick();
  const post = assertRequest(h, 1, 'POST', 'retranslate');
  assert.equal(post.body.cue_id, 'cue-b');
  post.respond(503, { detail: 'translation unavailable' });
  await pending;
  assert.deepEqual([...h.api.state.untranslated], ['cue-b']);
  assert.equal(h.api.els.transWarn.textContent, '1 cue chưa dịch');
  assert.equal(h.api.state.cues[1].trans, 'MANUAL C');
  assert.equal(h.requests.length, 2);
});

test('blank newly inserted cues never produce unknown-ID translation requests', async () => {
  const h = harness();
  const result = mixedJob();
  result.cues[1].trans = 'MANUAL B';
  result.stats.untranslated_indices = [];
  h.api.openJob(result, false);
  h.api.state.activeCue = 0;
  h.api.addCue();
  const draft = h.api.state.cues[1];
  const pending = h.api.retranslateMissing();
  const put = assertRequest(h, 0, 'PUT', 'cues');
  assert.equal(put.body.cues.some((cue) => cue.id === draft.id), false);
  put.respond();
  await pending;
  assert.equal(h.requests.length, 1, 'no POST follows a PUT that omitted the blank draft');
  assert.equal(h.api.state.untranslated.length, 0);
  await h.api.retranslateCue(1, cueField(h, 1, '.retrans-btn'));
  assert.equal(h.requests.length, 1, 'single-cue translation also rejects blank drafts');
});

test('new cue is included only after source input and a successful PUT', async () => {
  const h = harness();
  const result = job('job-a', [{ id: 'cue-a', start: 1, end: 2, text: 'source', trans: 'manual' }]);
  result.stats.untranslated_indices = [];
  h.api.openJob(result, false);
  h.api.addCue();
  h.edit('text', 'new source', 1);
  const id = h.api.state.cues[1].id;
  assert.deepEqual([...h.api.state.untranslated], [id]);
  const pending = h.api.retranslateMissing();
  const put = assertRequest(h, 0, 'PUT', 'cues');
  assert.equal(put.body.cues.find((cue) => cue.id === id).text, 'new source');
  put.respond();
  await tick();
  const post = assertRequest(h, 1, 'POST', 'retranslate');
  assert.equal(post.body.cue_id, id);
  post.respond(200, { ok: true, cue_id: id, trans: 'new translation' });
  await tick();
  assertRequest(h, 2, 'PUT', 'cues').respond();
  await pending;
  assert.equal(h.api.state.cues[0].trans, 'manual');
  assert.equal(h.api.state.cues[1].trans, 'new translation');
});

for (const change of ['manual translation', 'delete cue', 'blank source']) {
  test('bulk skips a later target changed to ' + change + ' while an earlier preview waits', async () => {
    const h = harness();
    const result = mixedJob();
    result.cues[0].trans = '';
    result.stats.untranslated_indices = [0, 1];
    h.api.openJob(result, false);
    const pending = h.api.retranslateMissing();
    assertRequest(h, 0, 'PUT', 'cues').respond();
    await tick();
    const first = assertRequest(h, 1, 'POST', 'retranslate');
    assert.equal(first.body.cue_id, 'cue-a');
    if (change === 'manual translation') h.edit('trans', 'MANUAL B', 1);
    else if (change === 'delete cue') h.api.deleteCue({ target: cueField(h, 1, '.del-btn') });
    else h.edit('text', ' ', 1);
    first.respond(200, { ok: true, cue_id: 'cue-a', trans: 'TRANSLATED A' });
    await tick();
    const put = assertRequest(h, 2, 'PUT', 'cues');
    if (change === 'manual translation') assert.equal(put.body.cues.find((cue) => cue.id === 'cue-b').trans, 'MANUAL B');
    else assert.equal(put.body.cues.some((cue) => cue.id === 'cue-b'), false);
    put.respond();
    await pending;
    assert.equal(h.requests.length, 3, 'removed or resolved target cannot cause a second POST');
    assert.equal(h.api.state.cues.find((cue) => cue.id === 'cue-c').trans, 'MANUAL C');
  });
}

test('missing warnings reflect manual edits, clearing translations, deletion and undo', () => {
  const h = harness();
  h.api.openJob(mixedJob(), false);
  h.edit('trans', 'MANUAL B', 1);
  assert.equal(h.api.els.transWarn.classList.contains('hidden'), true);
  h.edit('trans', '   ', 1);
  assert.deepEqual([...h.api.state.untranslated], ['cue-b']);
  h.api.deleteCue({ target: cueField(h, 1, '.del-btn') });
  assert.equal(h.api.state.untranslated.length, 0);
  h.api.undoEdit();
  assert.deepEqual([...h.api.state.untranslated], ['cue-b']);
  assert.equal(h.api.els.transWarn.textContent, '1 cue chưa dịch');
});

test('split and merge preserve missing identity without turning manual translations into targets', () => {
  const h = harness();
  h.api.openJob(mixedJob(), false);
  cueField(h, 1).selectionStart = 6;
  h.api.splitCue({ target: cueField(h, 1, '.split-btn') });
  assert.equal(h.api.state.untranslated.length, 2);
  assert.equal(new Set(h.api.state.untranslated).size, 2);
  assert.ok(h.api.state.untranslated.every((id) => h.api.state.cues.some((cue) => cue.id === id)));
  h.api.mergeCue({ target: cueField(h, 1, '.merge-btn') });
  assert.deepEqual([...h.api.state.untranslated], ['cue-b']);
  assert.equal(h.api.state.cues[2].trans, 'MANUAL C');
});

test('reload prefers stable missing IDs over stale row indices and still translates only B', async () => {
  const h = harness();
  const reloaded = mixedJob();
  reloaded.cues.shift();
  reloaded.stats.untranslated_indices = [1];
  reloaded.stats.untranslated_ids = ['cue-b', 'deleted-id', 'cue-c'];
  h.api.openJob(reloaded, true);
  assert.deepEqual([...h.api.state.untranslated], ['cue-b'], 'deleted IDs and manual translations are pruned');
  const pending = h.api.retranslateMissing();
  assertRequest(h, 0, 'PUT', 'cues').respond();
  await tick();
  const post = assertRequest(h, 1, 'POST', 'retranslate');
  assert.equal(post.body.cue_id, 'cue-b');
  post.respond(200, { ok: true, cue_id: 'cue-b', trans: 'TRANSLATED B' });
  await tick();
  const put = assertRequest(h, 2, 'PUT', 'cues');
  assert.equal(put.body.cues.find((cue) => cue.id === 'cue-c').trans, 'MANUAL C');
  put.respond();
  await pending;
  assert.equal(h.requests.length, 3);
  assert.equal(h.api.state.untranslated.length, 0);
});

test('explicit empty stable missing IDs preserve exemptions instead of falling back to stale indices', async () => {
  const h = harness();
  const result = job();
  result.stats.untranslated_ids = [];
  h.api.openJob(result, true);
  assert.equal(h.api.state.untranslated.length, 0);
  assert.equal(h.api.els.transWarn.classList.contains('hidden'), true);
  const pending = h.api.retranslateMissing();
  assertRequest(h, 0, 'PUT', 'cues').respond();
  await pending;
  assert.equal(h.requests.length, 1, 'explicit stable metadata cannot trigger unintended translation');
});


test('find and replace updates missing identities only for changed blank translations', () => {
  const h = harness();
  const result = mixedJob();
  result.stats.untranslated_indices = [];
  h.api.openJob(result, false);
  h.api.els.findInput.value = 'source B';
  h.api.els.replaceInput.value = 'edited source B';
  h.api.replaceAllInCues();
  assert.deepEqual([...h.api.state.untranslated], ['cue-b']);
  assert.equal(h.api.state.cues[2].trans, 'MANUAL C');
  h.api.els.findInput.value = 'MANUAL C';
  h.api.els.replaceInput.value = '';
  h.api.replaceAllInCues();
  assert.deepEqual([...h.api.state.untranslated], ['cue-b', 'cue-c']);
});


test('same-target backend exemptions remain warning-free until a source is edited', async () => {
  const h = harness();
  const result = job();
  result.stats = { translate_total: 0, untranslated_indices: [] };
  h.api.openJob(result, false);
  assert.equal(h.api.state.untranslated.length, 0);
  assert.equal(h.api.els.transWarn.classList.contains('hidden'), true);
  const pending = h.api.retranslateMissing();
  assertRequest(h, 0, 'PUT', 'cues').respond();
  await pending;
  assert.equal(h.requests.length, 1, 'bulk does not translate language-exempt sources');
  h.edit('text', 'new source');
  assert.deepEqual([...h.api.state.untranslated], ['cue-a']);
});

for (const outcome of ['ok', 'http', 'network']) {
  for (const nextId of ['job-b', 'job-a']) {
    test('late preview POST (' + outcome + ') cannot poll or alter a new ' + nextId + ' session', async () => {
      const h = harness();
      const pending = h.api.requestPreview();
      const post = assertRequest(h, 0, 'POST', 'preview');
      h.api.openJob(job(nextId), false);
      const src = h.api.els.video.src;
      const toast = h.api.els.toast.textContent;
      assert.equal(h.api.els.previewBtn.disabled, false, 'new session resets the busy button');
      const current = h.api.requestPreview();
      const currentPost = h.requests[1];
      assert.equal(currentPost.url, '/api/jobs/' + nextId + '/preview');
      const label = h.api.els.previewState.textContent;
      if (outcome === 'network') post.reject();
      else post.respond(outcome === 'ok' ? 200 : 503);
      await pending;
      assert.equal(h.requests.length, 2, 'stale POST must not start a status GET for either session');
      assert.equal(h.api.els.video.src, src);
      assert.equal(h.api.els.previewBtn.disabled, true, 'old failure cannot release the current button');
      assert.equal(h.api.els.previewState.textContent, label);
      assert.equal(h.api.els.toast.textContent, toast);
      assert.equal(h.api.state.previewTimer, null);
      currentPost.respond(503);
      await current;
      await h.clock.advance(3000);
      assert.equal(h.requests.length, 2);
    });
  }
}

for (const outcome of ['ready', 'error', 'running', 'network']) {
  test('late preview GET (' + outcome + ') cannot change B footage, button or timer', async () => {
    const h = harness();
    const old = h.api.pollPreview();
    const getA = h.requests[0];
    assert.equal(getA.url, '/api/jobs/job-a');
    h.api.openJob(job('job-b'), false);
    const current = h.api.requestPreview();
    h.requests[1].respond();
    await current;
    await tick();
    const getB = h.requests[2];
    assert.equal(getB.url, '/api/jobs/job-b');
    getB.respond(200, { id: 'job-b', preview_status: 'running' });
    await tick();
    const timer = h.api.state.previewTimer;
    assert.ok(timer);
    const src = h.api.els.video.src;
    const label = h.api.els.previewState.textContent;
    const toast = h.api.els.toast.textContent;
    if (outcome === 'network') getA.reject();
    else getA.respond(200, { id: 'job-a', preview_status: outcome });
    await old;
    assert.equal(h.api.els.video.src, src);
    assert.equal(h.api.els.previewBtn.disabled, true);
    assert.equal(h.api.els.previewState.textContent, label);
    assert.equal(h.api.els.toast.textContent, toast);
    assert.equal(h.api.state.previewTimer, timer, 'old response cannot cancel or replace B timer');
    await h.clock.advance(1500);
    assert.equal(h.requests.length, 4);
    assert.equal(h.requests[3].url, '/api/jobs/job-b');
    h.requests[3].respond(200, { id: 'job-b', preview_status: 'ready' });
    await tick();
    assert.match(h.api.els.video.src, /^\/api\/jobs\/job-b\/video\?preview=1&/);
  });
}

for (const leave of ['open', 'reset']) {
  test('scheduled preview timer is cleared on ' + leave, async () => {
    const h = harness();
    const pending = h.api.pollPreview();
    h.requests[0].respond(200, { id: 'job-a', preview_status: 'running' });
    await pending;
    assert.ok(h.api.state.previewTimer);
    if (leave === 'open') h.api.openJob(job('job-b'), false);
    else h.api.resetWorkspace();
    assert.equal(h.api.state.previewTimer, null);
    await h.clock.advance(4000);
    assert.equal(h.requests.length, 1, 'no stale timer may fetch a new job or revive an old one');
  });
}

test('preview POST failure enables retry and double click sends only one request', async () => {
  const h = harness();
  const pending = h.api.requestPreview();
  await h.api.requestPreview();
  assert.equal(h.requests.length, 1);
  h.requests[0].respond(503);
  await pending;
  assert.equal(h.api.els.previewBtn.disabled, false);
  assert.equal(h.api.els.previewState.textContent, 'Không gửi được yêu cầu.');
  await h.clock.advance(3000);
  assert.equal(h.requests.length, 1, 'failed POST must not start polling');
  const retry = h.api.requestPreview();
  h.requests[1].respond();
  await retry;
  h.requests[2].respond(200, { id: 'job-a', preview_status: 'error' });
  await tick();
  assert.equal(h.api.els.previewBtn.disabled, false);
  assert.equal(h.api.els.previewState.textContent, 'Tạo bản xem trước thất bại.');
  assert.equal(h.api.state.previewTimer, null);
});

test('preview session is checked after awaiting the response body', async () => {
  const h = harness();
  let resolveBody;
  const body = new Promise((resolve) => { resolveBody = resolve; });
  const pending = h.api.pollPreview();
  h.requests[0].respond(200, body);
  await tick();
  h.api.openJob(job('job-b'), false);
  const src = h.api.els.video.src;
  resolveBody({ id: 'job-a', preview_status: 'ready' });
  await pending;
  assert.equal(h.api.els.video.src, src);
  assert.equal(h.api.state.previewTimer, null);
});

for (const action of ['bulk', 'single']) {
  test('switch resets ' + action + ' translation button and old finally cannot release B work', async () => {
    const h = harness();
    const old = action === 'bulk' ? h.api.retranslateMissing()
      : h.api.retranslateCue(0, h.api.els.retransAllBtn);
    const oldPut = assertRequest(h, 0, 'PUT', 'cues');
    h.api.openJob(job('job-b'), false);
    assert.equal(h.api.els.retransAllBtn.disabled, false);
    assert.equal(h.api.els.retransAllBtn.textContent, 'Dịch lại chỗ thiếu');
    assert.equal(h.api.els.retransAllBtn.classList.contains('spin'), false);
    const current = h.api.retranslateMissing();
    assert.equal(h.api.els.retransAllBtn.disabled, true);
    oldPut.respond();
    await old;
    assert.equal(h.api.state.retranslating, true);
    assert.equal(h.api.els.retransAllBtn.disabled, true, 'old finally must not enable B busy button');
    h.requests[1].respond(503);
    await current;
    assert.equal(h.api.els.retransAllBtn.disabled, false);
  });
}

for (const outcome of ['ok', 'http', 'network']) {
  test('late handoff cancel (' + outcome + ') cannot replace B handoff or release B cancel button', async () => {
    const h = harness();
    h.api.bindEvents();
    const first = job();
    first.stats.handoff = { id: 'handoff-a', remote_job_id: 'remote-a', status: 'running', stage: 'A working' };
    h.api.openJob(first, false);
    h.api.els.handoffCancelBtn.click();
    const old = assertRequest(h, 0, 'POST', 'handoff/cancel');
    const next = job('job-b');
    next.stats.handoff = { id: 'handoff-b', remote_job_id: 'remote-b', status: 'running', stage: 'B working' };
    h.api.openJob(next, false);
    assert.equal(h.api.els.handoffCancelBtn.disabled, false);
    h.api.els.handoffCancelBtn.click();
    const current = h.requests[1];
    assert.equal(current.url, '/api/jobs/job-b/handoff/cancel');
    const handoff = h.api.state.handoff;
    const timer = h.api.state.handoffTimer;
    const stage = h.api.els.handoffStage.textContent;
    const toast = h.api.els.toast.textContent;
    if (outcome === 'network') old.reject();
    else old.respond(outcome === 'ok' ? 200 : 503, { handoff: { id: 'handoff-a', status: 'cancelled' } });
    await tick();
    assert.equal(h.api.state.handoff, handoff);
    assert.equal(h.api.state.handoffTimer, timer);
    assert.equal(h.api.els.handoffStage.textContent, stage);
    assert.equal(h.api.els.toast.textContent, toast);
    assert.equal(h.api.els.handoffCancelBtn.disabled, true);
    current.respond(200, { handoff: { id: 'handoff-b', status: 'cancelled', stage: 'B stopped' } });
    await tick();
    assert.equal(h.api.state.handoff.id, 'handoff-b');
    assert.equal(h.api.els.handoffStage.textContent, 'B stopped');
    assert.equal(h.api.els.handoffCancelBtn.disabled, false);
  });
}

test('handoff cancel double click sends one POST and retains normal success behavior', async () => {
  const h = harness();
  h.api.bindEvents();
  const result = job();
  result.stats.handoff = { id: 'handoff-a', remote_job_id: 'remote-a', status: 'running' };
  h.api.openJob(result, false);
  h.api.els.handoffCancelBtn.click();
  h.api.els.handoffCancelBtn.click();
  assert.equal(h.requests.length, 1);
  h.requests[0].respond(200, { handoff: { id: 'handoff-a', status: 'cancelling', stage: 'Stopping' } });
  await tick();
  assert.equal(h.api.state.handoff.status, 'cancelling');
  assert.equal(h.api.els.handoffStage.textContent, 'Stopping');
  assert.equal(h.api.els.toast.textContent, 'Đã yêu cầu Tool V1 dừng render');
});

for (const status of ['error', 'cancelled']) {
  test('opening ' + status + ' rerun displays retained cues, translations and handoff', () => {
    const h = harness();
    const result = mixedJob();
    result.status = status;
    result.error = status === 'error' ? 'rerun failed' : '';
    result.stats.handoff = { id: 'retained-handoff', status: 'done' };
    h.api.openJob(result, true);
    assert.equal(h.api.els.resultCard.classList.contains('hidden'), false);
    assert.equal(h.api.els.cueList.querySelectorAll('.cue').length, 3);
    assert.equal(cueField(h, 2, '.cue-trans').value, 'MANUAL C');
    assert.deepEqual([...h.api.state.untranslated], ['cue-b']);
    assert.equal(h.api.state.handoff.id, 'retained-handoff');
    assert.equal(h.api.els.stageLabel.textContent, status === 'error' ? 'Lỗi' : 'Đã hủy');
    assert.equal(h.api.state.running, false);
  });

  test('accepted rerun ending ' + status + ' reloads last-good cues before enabling export', async () => {
    const h = harness();
    configureStart(h);
    h.edit('trans', 'KEEP MANUAL TRANSLATION');
    const starting = h.api.startJob();
    assertRequest(h, 0, 'PUT', 'cues').respond();
    await starting;
    assertRequest(h, 1, 'POST', 'start').respond();
    await tick();
    assert.equal(h.api.state.running, true);
    assert.equal(h.api.state.cues.length, 0);
    const finishing = h.api.onJobFinished({ id: 'job-a', status, error: status === 'error' ? 'decode failed' : '' });
    const get = h.requests[2];
    assert.equal(get.url, '/api/jobs/job-a');
    assert.equal(h.api.state.resultPending, true);
    assert.equal(await h.api.saveCues(), false);
    await h.api.exportCues('srt');
    assert.equal(h.requests.length, 3);
    const retained = job('job-a', [{ id: 'cue-a', start: 1, end: 2, text: 'retained source', trans: 'KEEP MANUAL TRANSLATION' }]);
    retained.status = status;
    retained.params.translate_to = 'en';
    retained.stats.handoff = { id: 'retained-handoff', status: 'running' };
    retained.stats.untranslated_indices = [];
    get.respond(200, retained);
    await finishing;
    assert.equal(h.api.state.resultPending, false);
    assert.equal(h.api.state.running, false);
    assert.equal(h.api.els.resultCard.classList.contains('hidden'), false);
    assert.equal(h.api.state.cues[0].trans, 'KEEP MANUAL TRANSLATION');
    assert.equal(cueField(h, 0, '.cue-trans').value, 'KEEP MANUAL TRANSLATION');
    assert.equal(h.api.els.translateTo.value, 'en', 'retained result target is restored');
    assert.equal(h.api.state.handoff.id, 'retained-handoff');
    assert.equal(h.api.els.stageLabel.textContent, status === 'error' ? 'Lỗi' : 'Đã hủy');
    const exporting = h.api.exportCues('srt');
    const put = assertRequest(h, 3, 'PUT', 'cues');
    assert.equal(put.body.cues[0].trans, 'KEEP MANUAL TRANSLATION');
    put.respond();
    await exporting;
    assert.equal(h.document.downloads.length, 1);
  });

  test('failed ' + status + ' result fetch blocks empty cue PUTs', async () => {
    const h = harness();
    h.api.state.running = true;
    h.api.state.cues = [];
    const pending = h.api.onJobFinished({ id: 'job-a', status });
    h.requests[0].respond(503);
    await pending;
    assert.equal(h.api.state.running, false);
    assert.equal(h.api.state.resultPending, true);
    assert.equal(await h.api.saveCues(), false);
    await h.api.sendHandoff();
    assert.equal(h.requests.length, 1);
    assert.match(h.api.els.toast.textContent, /Tải lại trang/);
  });

  test('late ' + status + ' result cannot change a replacement editor session', async () => {
    const h = harness();
    const pending = h.api.onJobFinished({ id: 'job-a', status });
    const get = h.requests[0];
    h.api.openJob(job('job-a', [{ id: 'new-cue', start: 4, end: 5, text: 'NEW SESSION', trans: 'new manual' }]), false);
    const cues = h.api.state.cues;
    const label = h.api.els.stageLabel.textContent;
    get.respond(200, mixedJob());
    await pending;
    assert.equal(h.api.state.cues, cues);
    assert.equal(h.api.state.cues[0].id, 'new-cue');
    assert.equal(h.api.els.stageLabel.textContent, label);
    assert.equal(h.requests.length, 1);
  });

  test(status + ' without a last-good result keeps the empty result panel hidden', async () => {
    const h = harness();
    const pending = h.api.onJobFinished({ id: 'job-a', status });
    h.requests[0].respond(200, { ...job('job-a', []), status });
    await pending;
    assert.equal(h.api.els.resultCard.classList.contains('hidden'), true);
    assert.equal(h.api.state.resultPending, false);
  });
}

test('pending handoff intent hides cancel and blocks resend until a remote ID is known', async () => {
  const h = harness();
  h.api.bindEvents();
  const pending = { status: 'queued', remote_job_id: null, stage: 'Kiểm tra Tool V1 trước khi gửi lại.' };
  h.api.renderHandoff(pending);
  assert.equal(h.api.els.handoffStage.textContent, pending.stage, 'backend recovery instructions are preserved');
  assert.equal(h.api.els.handoffBtn.textContent, 'Đang xác nhận gửi sang Tool V1…');
  assert.equal(h.api.els.handoffBtn.disabled, true);
  assert.equal(h.api.els.handoffCancelBtn.classList.contains('hidden'), true);
  h.api.els.handoffCancelBtn.click();
  await h.api.sendHandoff();
  assert.equal(h.requests.length, 0, 'unknown intent cannot be cancelled or resent');
  h.api.els.handoffCloseBtn.click();
  assert.equal(h.api.state.handoff, pending, 'dismissing the panel must retain the active intent');
  assert.equal(h.api.els.handoffBtn.disabled, true);
  await h.api.sendHandoff();
  assert.equal(h.requests.length, 0, 'dismissing the panel cannot bypass the resend guard');
  h.api.renderHandoff({ ...pending, remote_job_id: 'remote-known', queue_position: 2 });
  assert.equal(h.api.els.handoffCancelBtn.classList.contains('hidden'), false);
  assert.equal(h.api.els.handoffBtn.textContent, 'Đang trong hàng chờ…');
  assert.equal(h.api.els.handoffCancelBtn.textContent, 'Rút khỏi hàng chờ');
  assert.match(h.api.els.handoffNote.textContent, /Vị trí chờ: 2/);
  h.api.renderHandoff({ status: 'running', remote_job_id: 'remote-known' });
  assert.equal(h.api.els.handoffCancelBtn.textContent, 'Hủy render');
  assert.doesNotMatch(h.api.els.handoffNote.textContent, /Vị trí chờ/);
});

test('failed handoff POST reconciles durable state, recovers IDs and retains ambiguous intent if GET fails', async () => {
  const instruction = 'Chưa xác định kết quả gửi. Kiểm tra Tool V1 trước khi gửi lại.';
  const cases = [
    { status: 507, record: { status: 'queued', remote_job_id: 'recovered-id', stage: 'Render recovered' } },
    { status: 502, record: { status: 'queued', remote_job_id: null, stage: instruction } },
    { status: 502, getFails: true },
    { status: 'network', getFails: true },
    { status: 400, record: null },
    { status: 400, getFails: true },
  ];
  for (const scenario of cases) {
    const h = harness();
    const pending = h.api.sendHandoff();
    assertRequest(h, 0, 'PUT', 'cues').respond();
    await tick();
    const post = assertRequest(h, 1, 'POST', 'handoff');
    if (scenario.status === 'network') post.reject();
    else post.respond(scenario.status, { detail: 'send outcome (' + scenario.status + ')' });
    await tick();
    const get = assertRequest(h, 2, 'GET', 'handoff');
    assert.equal(h.api.els.handoffBtn.disabled, true, 'resend stays blocked while reconciling');
    if (scenario.getFails) get.respond(503);
    else get.respond(200, { handoff: scenario.record });
    await pending;
    assert.equal(h.api.state.handoffSending, false);
    const definiteFailure = scenario.status === 400;
    assert.equal(h.api.state.handoff.status, definiteFailure ? 'error' : 'queued');
    assert.equal(h.api.els.handoffBtn.disabled, !definiteFailure);
    assert.equal(h.api.els.handoffCancelBtn.classList.contains('hidden'), !scenario.record?.remote_job_id);
    if (scenario.record) {
      assert.equal(h.api.state.handoff, scenario.record, 'authoritative backend record wins over POST error');
      assert.equal(h.api.els.handoffStage.textContent, scenario.record.stage);
    } else if (!definiteFailure) {
      assert.equal(h.api.state.handoff.remote_job_id, null);
      assert.match(h.api.els.handoffStage.textContent, /Không gửi lại; kiểm tra Tool V1/);
      assert.match(h.api.els.toast.textContent, /Không gửi lại; kiểm tra Tool V1/);
    }
    if (!definiteFailure) {
      await h.api.sendHandoff();
      assert.equal(h.requests.length, 3, 'pending render cannot be resent after reconciliation');
    }
  }
});

test('late handoff reconciliation GET cannot alter a replacement session or release its send button', async () => {
  for (const nextId of ['job-b', 'job-a']) {
    for (const getFails of [false, true]) {
      const h = harness();
      const old = h.api.sendHandoff();
      assertRequest(h, 0, 'PUT', 'cues').respond();
      await tick();
      assertRequest(h, 1, 'POST', 'handoff').respond(502, { detail: 'remote timeout' });
      await tick();
      const oldGet = assertRequest(h, 2, 'GET', 'handoff');
      const next = job(nextId);
      next.stats.handoff = { status: 'done', remote_job_id: 'current-id', stage: 'Current complete' };
      h.api.openJob(next, false);
      const current = h.api.sendHandoff();
      const currentPut = h.requests[3];
      const handoff = h.api.state.handoff;
      const label = h.api.els.handoffBtn.textContent;
      const toast = h.api.els.toast.textContent;
      if (getFails) oldGet.reject();
      else oldGet.respond(200, { handoff: { status: 'queued', remote_job_id: null, stage: 'STALE INTENT' } });
      await old;
      assert.equal(h.api.state.handoff, handoff);
      assert.equal(h.api.state.handoffSending, true);
      assert.equal(h.api.els.handoffBtn.disabled, true);
      assert.equal(h.api.els.handoffBtn.textContent, label);
      assert.equal(h.api.els.toast.textContent, toast);
      assert.equal(h.api.state.handoffTimer, null, 'old reconciliation cannot start a poll');
      assert.equal(h.requests.length, 4);
      currentPut.respond(503);
      await current;
    }
  }
});

test('dashboard handshake shows authorized connection and allows navigation without sending a project', async () => {
  const h = harness();
  const pending = h.api.checkToolV1Connection();
  assert.equal(h.requests[0].url, '/api/tool-v1/status');
  assert.equal(h.requests[0].method, 'GET');
  h.requests[0].respond(200, { ok: true, authorized: true, dashboard_url: 'http://127.0.0.1:8088/' });
  await pending;
  assert.equal(h.api.state.toolV1Connected, true);
  assert.match(h.api.els.toolV1Connection.textContent, /Đã kết nối/);
  assert.equal(h.api.els.toolV1Dashboard.href, 'http://127.0.0.1:8088/');
  assert.equal(h.api.els.handoffBtn.disabled, false);
  assert.equal(h.requests.length, 1, 'connection cannot import or render');
});

test('offline or unauthorized dashboard disables sending and retry restores the connection', async () => {
  const h = harness();
  let pending = h.api.checkToolV1Connection();
  h.requests[0].respond(200, { ok: false, detail: '403: missing control token', dashboard_url: 'http://127.0.0.1:8088/' });
  await pending;
  assert.equal(h.api.els.handoffBtn.disabled, true);
  assert.match(h.api.els.toolV1Connection.textContent, /403/);
  await h.api.sendHandoff();
  assert.equal(h.requests.length, 1, 'offline cannot send even via direct handler');
  h.api.openJob(job('job-b'), false);
  assert.equal(h.api.els.handoffBtn.disabled, true, 'switch cannot release offline guard');
  pending = h.api.checkToolV1Connection();
  h.requests[1].respond(200, { ok: true, authorized: true, dashboard_url: 'http://127.0.0.1:8088/' });
  await pending;
  assert.equal(h.api.els.handoffBtn.disabled, false);
});

test('older dashboard check cannot overwrite a newer check or release an active render', async () => {
  const h = harness();
  const old = h.api.checkToolV1Connection();
  const current = h.api.checkToolV1Connection();
  h.api.renderHandoff({ status: 'running', remote_job_id: 'remote', stem: 'project' });
  h.requests[1].respond(200, { ok: true, authorized: true, dashboard_url: 'http://127.0.0.1:8088/' });
  await current;
  h.requests[0].respond(200, { ok: false, detail: 'old offline' });
  await old;
  assert.equal(h.api.state.toolV1Connected, true);
  assert.match(h.api.els.toolV1Connection.textContent, /Đã kết nối/);
  assert.equal(h.api.els.handoffBtn.disabled, true);
  assert.equal(h.api.state.handoff.remote_job_id, 'remote');
});

test('selected blur strength is sent only after latest cue save succeeds', async () => {
  const h = harness();
  h.api.els.handoffBlur.value = '24';
  h.edit('trans', 'Latest translation');
  const pending = h.api.sendHandoff();
  const save = assertRequest(h, 0, 'PUT', 'cues');
  assert.equal(h.requests.length, 1);
  save.respond(200, { ok: true });
  await tick();
  const send = assertRequest(h, 1, 'POST', 'handoff');
  assert.equal(send.body.blur_radius, 24);
  send.respond(200, { handoff: { status: 'running', remote_job_id: 'remote' } });
  await pending;
  assert.equal(h.api.els.handoffBtn.disabled, true);
});

test('a failed connection during cue save prevents the later render POST', async () => {
  const h = harness();
  h.edit('trans', 'Latest');
  const pending = h.api.sendHandoff();
  assertRequest(h, 0, 'PUT', 'cues');
  const checking = h.api.checkToolV1Connection();
  h.requests[1].respond(200, { ok: false, detail: 'connection lost' });
  await checking;
  h.requests[0].respond(200, { ok: true });
  await pending;
  assert.equal(h.requests.length, 2);
  assert.equal(h.api.els.handoffBtn.disabled, true);
  assert.equal(h.api.state.handoffSending, false);
});
