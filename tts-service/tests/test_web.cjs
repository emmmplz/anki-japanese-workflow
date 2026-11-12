const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const script = fs.readFileSync(path.join(__dirname, '../tts_service/web.js'), 'utf8');

function client({ online = true, failSynthesis = false, preparing = false } = {}) {
  const state = { online, failSynthesis, preparing };
  const elements = new Map();
  for (const id of ['status', 'text', 'voice', 'language', 'speed', 'generate', 'result', 'audio', 'download', 'form']) {
    elements.set(id, {
      value: '', textContent: '', disabled: true, hidden: true, children: [], listeners: {},
      replaceChildren() { this.children = []; this.value = ''; },
      append(option) { this.children.push(option); if (option.selected) this.value = option.value; },
      addEventListener(event, handler) { this.listeners[event] = handler; },
    });
  }
  elements.get('text').value = '今日は晴れです。';
  elements.get('speed').value = '1';
  const timers = new Map();
  let sequence = 0;
  const context = {
    document: { getElementById: id => elements.get(id), createElement: () => ({}) },
    window: { addEventListener() {} }, AbortController, TypeError,
    setTimeout(fn, ms) { const id = ++sequence; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    URL: { createObjectURL: () => 'blob:test-audio', revokeObjectURL() {} },
    async fetch(url) {
      if (!state.online || (url === '/tts' && state.failSynthesis)) {
        state.online = false;
        throw new TypeError('Failed to fetch');
      }
      if (url === '/health') return { ok: !state.preparing,
        json: async () => ({ status: 'ready', backend: { engine: 'GPT-SoVITS', version: 'v2Pro' } }) };
      if (url === '/voices') return { ok: true, json: async () => ({ default_voice: 'default',
        default_language: 'ja', max_text_length: 1000, languages: ['ja', 'zh'],
        voices: [{ id: 'default', name: '日语参考音色' }] }) };
      assert.equal(url, '/tts');
      return { ok: true, blob: async () => ({}), headers: { get: () => '1.234' } };
    },
  };
  vm.runInNewContext(script, context);
  return { state, elements, async poll() {
    const entry = [...timers.entries()].find(([, value]) => value.ms === 3000);
    assert.ok(entry, 'A reconnection check should be scheduled');
    timers.delete(entry[0]);
    await entry[1].fn();
  } };
}

const settle = () => new Promise(resolve => setImmediate(resolve));

test('an offline page reconnects and preserves typed text and selected language', async () => {
  const page = client({ online: false });
  await settle();
  assert.equal(page.elements.get('generate').disabled, true);
  assert.match(page.elements.get('status').textContent, /自动重连/);
  page.state.online = true;
  await page.poll();
  assert.equal(page.elements.get('generate').disabled, false);
  page.elements.get('language').value = 'zh';
  await page.poll();
  assert.equal(page.elements.get('language').value, 'zh');
  assert.equal(page.elements.get('text').value, '今日は晴れです。');
  assert.equal(page.elements.get('voice').children.length, 1);
});

test('a preparing service becomes ready on the next connection check', async () => {
  const page = client({ preparing: true });
  await settle();
  assert.equal(page.elements.get('generate').disabled, true);
  assert.match(page.elements.get('status').textContent, /正在准备/);
  page.state.preparing = false;
  await page.poll();
  assert.equal(page.elements.get('generate').disabled, false);
});

test('a lost synthesis connection shows an actionable message and disables submission', async () => {
  const page = client({ failSynthesis: true });
  await settle();
  await page.elements.get('form').listeners.submit({ preventDefault() {} });
  await settle();
  assert.equal(page.elements.get('generate').disabled, true);
  assert.match(page.elements.get('result').textContent, /连接已中断/);
  assert.doesNotMatch(page.elements.get('result').textContent, /Failed to fetch/);
  page.state.online = true;
  await page.poll();
  assert.equal(page.elements.get('generate').disabled, false);
});

test('successful synthesis enables playback and a download', async () => {
  const page = client();
  await settle();
  await page.elements.get('form').listeners.submit({ preventDefault() {} });
  assert.equal(page.elements.get('audio').hidden, false);
  assert.equal(page.elements.get('download').href, 'blob:test-audio');
  assert.match(page.elements.get('result').textContent, /生成完成/);
  assert.equal(page.elements.get('generate').disabled, false);
});
