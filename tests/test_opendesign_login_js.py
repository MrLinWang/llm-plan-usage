"""Exercise the browser authorization lifecycle without external requests."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.events = {}; this.attributes = {};
    this.dataset = {}; this.style = {}; this.value = ''; this._text = '';
    this.disabled = false; this.hidden = false;
  }
  set textContent(value) { this._text = value; this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  get childNodes() { return this.children; }
  appendChild(child) { child.parent = this; this.children.push(child); return child; }
  insertBefore(child, before) {
    child.parent = this; this.children.splice(this.children.indexOf(before), 0, child);
  }
  remove() {
    if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1);
  }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(key, fn) { this.events[key] = fn; }
  async click() { if (!this.disabled && this.events.click) await this.events.click(); }
  focus() { this.focused = true; }
  select() { this.selected = true; }
  querySelectorAll(selector) {
    return nodes(this).filter(e => selector === 'input[type=checkbox]' &&
                                     e.tag === 'input' && e.type === 'checkbox');
  }
}
function nodes(element) {
  return element.children.flatMap(child => [child, ...nodes(child)]);
}
function button(element, text) {
  const result = nodes(element).find(e => e.tag === 'button' && e.textContent === text);
  assert.ok(result, 'missing button: ' + text); return result;
}
function response(data, status = 200) {
  return {ok: status >= 200 && status < 300, status, async json() { return data; }};
}
const timers = new Map();
let timerId = 0;
global.setTimeout = (fn, delay) => { timers.set(++timerId, {fn, delay}); return timerId; };
global.clearTimeout = id => timers.delete(id);
let now = Date.now();
Date.now = () => now;
async function flush() { for (let i = 0; i < 15; i++) await Promise.resolve(); }
async function tick() {
  assert.equal(timers.size, 1);
  const [id, timer] = timers.entries().next().value;
  timers.delete(id); now += timer.delay; timer.fn(); await flush();
}
const windowEvents = {};
global.window = {addEventListener(name, callback) { windowEvents[name] = callback; }};
global.document = {
  createElement(tag) { return new Element(tag); },
  createTextNode(text) { const node = new Element('#text'); node.textContent = text; return node; },
};
global.currentUser = 'admin';
const requests = [], cancellations = [];
let answers = [];
global.api = async (url, options) => {
  requests.push({url, ...options});
  assert.ok(answers.length, 'unexpected request: ' + url);
  const answer = answers.shift();
  if (answer instanceof Error) throw answer;
  return typeof answer === 'function' ? answer() : answer;
};
global.fetch = async (url, options) => {
  cancellations.push({url, ...options}); return response({});
};
global.detailOf = async r => (await r.json()).detail || 'HTTP ' + r.status;
const html = fs.readFileSync(process.argv[1], 'utf8');
vm.runInThisContext(html.slice(html.indexOf('function el('),
                              html.indexOf('// ---- 拖动排序')));
function authorization(id = 'session-1') {
  return response({id, status: 'pending',
    activation_url: 'https://amr.open-design.ai/activate?code=ABCD-EFGH',
    user_code: 'ABCD-EFGH', expires_at: new Date(now + 60000).toISOString(),
    poll_interval_seconds: 3,
  });
}
function credentialSlot(index, name = '套餐1') {
  return {index, name, credentials: {control_key: {set: true, hint: 'vela…12'}}};
}
function platform(slots = []) {
  return {key: 'opendesign#2', type: 'opendesign', display_name: '我的 OpenDesign',
          enabled: false, credential_slots: slots, visibility: {type: 'private', targets: []}};
}
function slotBoxes(card) {
  return nodes(card).filter(e => e.className === 'group-editor');
}
function inputFor(card, label) {
  const row = nodes(card).find(e => e.tag === 'label' && e._text === label);
  assert.ok(row, 'missing label: ' + label);
  return nodes(row).find(e => e.tag === 'input');
}
"""


def _run_scenario(scenario: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed to exercise browser authorization")
    page = Path(__file__).resolve().parents[1] / "src/llm_usage/static/config.html"
    script = (
        _HARNESS + "\n(async () => {\n" + scenario
        + "\n})().catch(e => { console.error(e); process.exit(1); });"
    )
    result = subprocess.run([node, "-e", script, str(page)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_browser_login_updates_only_target_slot_and_preserves_drafts() -> None:
    _run_scenario(r"""
const card = platformCard(platform([credentialSlot(0), credentialSlot(1, '另一个账号')]), true);
const first = slotBoxes(card)[0], other = slotBoxes(card)[1];
inputFor(first, '账户控制密钥').value = 'unsaved-old-control-key';
inputFor(other, '账户控制密钥').value = 'other-account-draft';
inputFor(card, '显示名称').value = '尚未保存的新名称';
// The first checkbox is the platform's enabled field.
const enabled = nodes(card).find(e => e.tag === 'input' && e.type === 'checkbox');
enabled.checked = true;
answers.push(authorization(), response({status: 'pending', poll_interval_seconds: 7}),
             response({status: 'authorized', slot_index: 0,
                       platform: platform([credentialSlot(0), credentialSlot(1)])}));
await button(first, '网页登录授权').click();
assert.deepEqual(JSON.parse(requests[0].body), {platform: 'opendesign#2', index: 0, name: '套餐1'});
assert.equal(button(card, '保存').disabled, true);
assert.equal(button(other, '网页登录授权').disabled, true);
assert.equal(inputFor(other, '账户控制密钥').disabled, true);
const link = nodes(first).find(e => e.tag === 'a');
assert.equal(link.hidden, false); assert.equal(link.target, '_blank');
assert.equal(link.rel, 'noopener noreferrer'); assert.equal(link.focused, true);
assert.equal(timers.values().next().value.delay, 3000);
await tick();
assert.equal(timers.values().next().value.delay, 7000);
await tick();
assert.equal(timers.size, 0); assert.equal(openDesignLogins.size, 0);
assert.equal(button(card, '保存').disabled, false);
assert.equal(inputFor(first, '账户控制密钥').value, '');
assert.equal(inputFor(first, '账户控制密钥').placeholder, '已设置 (vela…12)');
assert.equal(inputFor(other, '账户控制密钥').value, 'other-account-draft');
assert.equal(inputFor(card, '显示名称').value, '尚未保存的新名称');
assert.equal(enabled.checked, true);
assert.match(first.textContent, /授权成功，凭证已自动保存/);
// Adding a slot must preserve manual input drafts in every previous slot.
await button(card, '添加凭证').click();
assert.equal(inputFor(slotBoxes(card)[1], '账户控制密钥').value, 'other-account-draft');
assert.equal(slotBoxes(card).length, 3);
answers.push(authorization('new-slot'), response({status: 'authorized', slot_index: 2,
             platform: platform([credentialSlot(0), credentialSlot(1),
                                 credentialSlot(2, '新账号')])}));
await button(slotBoxes(card)[2], '网页登录授权').click(); await tick();
assert.equal(JSON.parse(requests[3].body).index, null);
// The appended persisted index is retained in the subsequent normal card save.
answers.push(response(platform([credentialSlot(0), credentialSlot(1), credentialSlot(2)])));
await button(card, '保存').click();
const saved = JSON.parse(requests.at(-1).body);
assert.equal(saved.enabled, true); assert.equal(saved.display_name, '尚未保存的新名称');
assert.equal(saved.credential_slots[0].control_key, null);
assert.equal(saved.credential_slots[1].control_key, 'other-account-draft');
assert.equal(saved.credential_slots[2].index, 2);
assert.ok(requests.at(-1).url.startsWith('/api/config/platforms/'));
""")


def test_browser_login_retries_expires_cancels_and_ignores_late_results() -> None:
    _run_scenario(r"""
const card = platformCard(platform(), false);
const slot = slotBoxes(card)[0];
answers.push(authorization(), new Error('network failure'), response({status: 'denied'}));
await button(slot, '网页登录授权').click(); await tick();
assert.match(slot.textContent, /稍后自动重试/);
assert.equal(timers.values().next().value.delay, 3000);
await tick();
assert.match(slot.textContent, /已拒绝/); assert.equal(button(card, '保存').disabled, false);
answers.push(authorization('expiring'));
await button(slot, '网页登录授权').click(); now += 60001; await tick();
assert.match(slot.textContent, /授权已过期/); assert.equal(timers.size, 0);
assert.ok(cancellations.some(r => r.url.endsWith('/expiring')));
let resolveStart;
answers.push(() => new Promise(resolve => { resolveStart = resolve; }));
const starting = button(slot, '网页登录授权').click(); await flush();
await button(slot, '取消授权').click();
resolveStart(authorization('late')); await starting; await flush();
assert.equal(timers.size, 0); assert.match(slot.textContent, /授权已取消/);
assert.ok(cancellations.some(r => r.url.endsWith('/late')));
answers.push(authorization('leaving'));
await button(slot, '网页登录授权').click();
windowEvents.pagehide();
assert.equal(timers.size, 0); assert.equal(openDesignLogins.size, 0);
assert.ok(cancellations.some(r => r.url.endsWith('/leaving') && r.keepalive));
assert.equal(button(card, '保存').disabled, false);
""")


def test_browser_login_conflicts_and_untrusted_links_do_not_save_credentials() -> None:
    _run_scenario(r"""
const card = platformCard(platform([credentialSlot(0)]), false);
const slot = slotBoxes(card)[0];
inputFor(slot, '账户控制密钥').value = 'preserved-draft';
answers.push(authorization('conflict'), response({detail: '配置已变化，请重新加载配置页。'}, 409));
await button(slot, '网页登录授权').click(); await tick();
assert.match(slot.textContent, /配置已变化/); assert.equal(timers.size, 0);
assert.equal(inputFor(slot, '账户控制密钥').value, 'preserved-draft');
assert.ok(cancellations.some(r => r.url.endsWith('/conflict')));
answers.push(response({id: 'unsafe', status: 'pending', user_code: 'ABCD-EFGH',
  activation_url: 'https://open-design.ai.attacker.example/login',
  expires_at: new Date(now + 60000).toISOString(), poll_interval_seconds: 3}));
await button(slot, '网页登录授权').click();
assert.match(slot.textContent, /授权地址无效/);
assert.equal(nodes(slot).find(e => e.tag === 'a').hidden, true);
assert.equal(timers.size, 0);
assert.ok(cancellations.some(r => r.url.endsWith('/unsafe')));
answers.push(authorization('before-navigation'));
await button(slot, '网页登录授权').click(); windowEvents.pagehide();
// A page restored from the browser's back/forward cache can start a new login.
answers.push(authorization('after-navigation'));
await button(slot, '网页登录授权').click();
assert.equal(timers.size, 1);
await button(slot, '取消授权').click();
assert.equal(timers.size, 0);
assert.equal(inputFor(slot, '账户控制密钥').value, 'preserved-draft');
assert.equal(requests.filter(r => r.method === 'PUT').length, 0);
""")
