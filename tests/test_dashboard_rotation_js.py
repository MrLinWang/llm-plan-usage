"""Run the dashboard's actual card renderer and timers in Node against a fake DOM."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[1] / "src/llm_usage/static/index.html"

# Shared scaffold: load the page's real formatting and card-rendering code by function-name
# slices (without its polling loop), then drive platformCard() with a minimal fake DOM and
# global stubs. setInterval only records callbacks; tests fire them by hand.
_HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[1], 'utf8');
vm.runInThisContext(html.slice(html.indexOf('function p2('), html.indexOf('// ---- Sub2API')));
vm.runInThisContext(html.slice(html.indexOf('function dataRow('),
                              html.indexOf('function pageFitsViewport(')));

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.events = {};
    this.className = '';
    this.hidden = false;
    this.parent = null;
    this.style = {setProperty() {}};
    this.classList = {
      contains: name => this.className.split(' ').includes(name),
      add: name => { if (!this.classList.contains(name)) this.className += ' ' + name; },
      remove: name => {
        this.className = this.className.split(' ').filter(n => n !== name).join(' ');
      },
      toggle: (name, value) => {
        const add = value === undefined ? !this.classList.contains(name) : value;
        if (add) this.classList.add(name); else this.classList.remove(name);
      }
    };
  }
  set textContent(value) {
    this._text = String(value);
    this.children.forEach(child => { child.parent = null; });
    this.children = [];
  }
  get textContent() { return (this._text || '') + this.children.map(c => c.textContent).join(''); }
  appendChild(child) { child.parent = this; this.children.push(child); return child; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this.events[name] = callback; }
  click() { this.events.click({stopPropagation() {}}); }
  matches(selector) {
    if (selector === ':hover') return Boolean(this.hovered);
    return selector.startsWith('.')
      ? this.classList.contains(selector.slice(1)) : this.tag === selector;
  }
  querySelectorAll(selector) {
    return this.children.flatMap(child => [
      ...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)
    ]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  contains(element) {
    return this === element || this.children.some(child => child.contains(element));
  }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this);
    this.parent = null;
  }
}
global.document = {createElement: tag => new Element(tag), activeElement: null, hidden: false,
                   documentElement: {dataset: {}}};
let reducedMotion = false;
global.window = {matchMedia: () => ({matches: reducedMotion})};
global.entryTableTpl = {content: {cloneNode() {
  const fragment = new Element('fragment');
  const table = fragment.appendChild(new Element('table'));
  table.appendChild(new Element('tbody'));
  return fragment;
}}};
global.platformsEl = new Element('div');
global.getComputedStyle = () => ({gridTemplateColumns: '1fr 1fr'});
global.expandedPlatformKeys = new Set();
global.setPlatformExpanded = (key, expanded) => {
  if (expanded) expandedPlatformKeys.add(key); else expandedPlatformKeys.delete(key);
};
global.closeKeyPopovers = () => {};
let refits = 0;
global.refitLayout = () => { refits++; };
global.ENTRY_ROTATE_MS = 5000;
global.entryRotationTimers = [];
const activeTimers = new Map();
let nextTimer = 0;
global.setInterval = (callback, delay) => {
  const id = ++nextTimer;
  activeTimers.set(id, {callback, delay});
  return id;
};
global.clearInterval = id => activeTimers.delete(id);

function reset() {
  activeTimers.clear();
  entryRotationTimers = [];
  expandedPlatformKeys.clear();
  document.hidden = false;
  document.activeElement = null;
  reducedMotion = false;
  refits = 0;
}
function fixture(name) {
  const entries = [];
  for (let account = 1; account <= 5; account++) {
    for (const [label, factor] of [['每日', 1], ['每周', 10], ['每月', 100]]) {
      entries.push({label, plan: '账户' + account, used: account * factor,
                    limit: 100 * factor, remaining: (100 - account) * factor,
                    percent: account, reset_at: null, reset_kind: null, unit: '$'});
    }
  }
  return {name, display_name: '自定义名称', entries, error: null, warning: null};
}
function planNames(body) {
  return body.querySelectorAll('.plan-head').map(row => row.textContent)
             .filter(name => name !== '全部合计');
}
function assertDailySummary(body) {
  const summaries = body.querySelectorAll('.summary-row');
  assert.equal(summaries.length, 1);
  assert.equal(summaries[0].children[0].textContent, '每日');
  assert.equal(summaries[0].children[1].children[0].textContent, '$15');
  assert.equal(summaries[0].children[2].textContent, '$500');
}
function headOf(card) { return card.children[0]; }
function pagerOf(root) { return root.querySelector('.rotation-indicator'); }
function isDataRow(row) {
  return !['bar-row', 'plan-head', 'summary-row'].some(name => row.classList.contains(name));
}
// Window labels of the entry rows (no bars, plan headers or total rows).
function entryLabels(body) {
  return body.children.filter(isDataRow).map(row => row.children[0].textContent);
}
function summaryLabels(body) {
  return body.querySelectorAll('.summary-row').map(row => row.children[0].textContent);
}
function remainders(body) {
  return body.querySelectorAll('.remaining-cell').map(cell => cell.children[0].textContent);
}
function windowEntry(label, unit, extra) {
  return Object.assign({label, plan: null, used: 10, limit: 100, remaining: 90, percent: 10,
                        reset_at: null, reset_kind: null, unit}, extra);
}
function platform(entries, extra) {
  return Object.assign({name: 'volcengine-coding', display_name: '火山方舟', entries,
                        error: null, warning: null}, extra);
}
// Click the switch button `steps` times, recording [button text, entry labels] before each.
function walk(card, steps) {
  const pager = pagerOf(card);
  const body = card.querySelector('tbody');
  const seen = [];
  for (let i = 0; i < steps; i++) {
    seen.push([pager.textContent, entryLabels(body)]);
    pager.click();
  }
  return seen;
}
"""


def _run_dashboard_script(body: str, dashboard: Path | None = None) -> None:
    """Run the harness plus ``body`` in Node against ``dashboard`` (default: shipped page)."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed to exercise dashboard JavaScript")
    result = subprocess.run(
        [node, "-e", _HARNESS + body, str(dashboard or DASHBOARD)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


_SUB2API_ROTATION = r"""
// Base providers, independent instances and legacy payloads share the same behavior.
for (const name of ['sub2api', 'sub2api#2', 'llm-gateway#2']) {
  for (const mode of ['normal', 'compact', 'ultra']) {
    reset();
    const platform = fixture(name);
    const card = platformCard(platform, mode, 'card');
    const body = card.querySelector('tbody');
    const indicator = card.querySelector('.rotation-indicator');
    const toggle = card.querySelector('.entry-toggle');
    const slots = mode === 'normal' ? 2 : 1;
    assert.ok(indicator, name + '/' + mode + ' must expose rotation controls');
    assert.equal(indicator.textContent, slots === 2 ? '1–2/5' : '1/5');
    assert.match(indicator.title, /账号/);
    assert.equal(activeTimers.size, 1);
    const timer = [...activeTimers.values()][0];
    assert.equal(timer.delay, 5000);
    assert.deepEqual(planNames(body), slots === 2 ? ['账户1', '账户2'] : ['账户1']);
    assert.equal(body.querySelectorAll('.rotating-slot').length, slots * 2);
    assertDailySummary(body);
    timer.callback();
    assert.deepEqual(planNames(body), slots === 2 ? ['账户3', '账户4'] : ['账户2']);
    assertDailySummary(body);
    indicator.click();
    assert.deepEqual(planNames(body), slots === 2 ? ['账户5', '账户1'] : ['账户3']);
    assertDailySummary(body);

    const beforePause = body.textContent;
    card.hovered = true;
    timer.callback();
    card.hovered = false;
    document.hidden = true;
    timer.callback();
    document.hidden = false;
    document.activeElement = indicator;
    timer.callback();
    document.activeElement = null;
    assert.equal(body.textContent, beforePause, 'hover, hidden tab and focus pause rotation');

    toggle.click();
    assert.ok(card.classList.contains('is-expanded'));
    assert.ok(expandedPlatformKeys.has('card'));
    assert.ok(indicator.hidden);
    timer.callback();
    assert.equal(body.textContent, beforePause, 'expanded cards must not rotate');
    const grid = card.querySelector('.expanded-entry-grid');
    assert.deepEqual(planNames(grid), ['账户1', '账户2', '账户3', '账户4', '账户5']);
    const summaries = grid.querySelectorAll('.summary-row');
    assert.deepEqual(summaries.map(row => row.children[0].textContent),
                     mode === 'normal' ? ['每日', '每周', '每月'] : ['每日']);
    assert.deepEqual(summaries.map(row => row.children[1].children[0].textContent),
                     mode === 'normal' ? ['$15', '$150', '$1500'] : ['$15']);
    toggle.click();
    assert.ok(!card.classList.contains('is-expanded'));
    assert.ok(!indicator.hidden);
    assert.equal(refits, 2);
    timer.callback();
    assert.notEqual(body.textContent, beforePause, 'collapsed cards resume rotation');
    assertDailySummary(body);
  }
}

reset();
reducedMotion = true;
const reduced = platformCard(fixture('sub2api'), 'ultra', 'reduced');
assert.equal(activeTimers.size, 0);
const reducedBody = reduced.querySelector('tbody');
reduced.querySelector('.rotation-indicator').click();
assert.deepEqual(planNames(reducedBody), ['账户2'], 'manual switching remains available');

reset();
// Other period platforms and single-account Sub2API page manually instead of rotating.
const other = platformCard(fixture('kimi'), 'compact', 'other');
assert.equal(pagerOf(other).textContent, '1–2/15');
const singlePlatform = fixture('sub2api');
singlePlatform.entries = singlePlatform.entries.filter(entry => entry.plan === '账户1');
assert.equal(pagerOf(platformCard(singlePlatform, 'compact', 'single')).textContent, '1–2/3');
assert.equal(activeTimers.size, 0, 'manual paging never starts rotation timers');

reset();
const independent = fixture('other');
independent.entries = shortestWindowEntries(independent.entries).map(entry => ({
  ...entry, label: entry.plan, plan: null
}));
const flat = platformCard(independent, 'compact', 'flat');
const flatBody = flat.querySelector('tbody');
assert.equal(flatBody.querySelector('.rotating-slot').children[0].textContent, '账户1');
[...activeTimers.values()][0].callback();
assert.equal(flatBody.querySelector('.rotating-slot').children[0].textContent, '账户2');
assert.equal(flatBody.querySelector('.summary-row').children[1].children[0].textContent, '$15');

reset();
for (const mode of ['normal', 'compact', 'ultra']) {
  renderPlatformsAtMode([fixture('sub2api')], mode);
  assert.equal(activeTimers.size, 1, 'density re-fit must clear previous card timers');
}
"""


def test_sub2api_collapsed_accounts_rotate_without_mixing_period_totals() -> None:
    _run_dashboard_script(_SUB2API_ROTATION)


_PERIOD_PAGING = r"""
const windows = ['5小时', '每周', '每月'].map(label => windowEntry(label, '%'));
{
  reset();
  const card = platformCard(platform(windows), 'compact', 'compact');
  const pager = pagerOf(card);
  const body = card.querySelector('tbody');
  const toggle = card.querySelector('.entry-toggle');
  assert.deepEqual(headOf(card).children.map(child => child.className),
                   ['platform-name', 'rotation-indicator', 'entry-toggle', 'platform-state ok']);
  assert.equal(pager.type, 'button');
  assert.equal(pager.attributes['aria-label'], '切换下一组用量');
  assert.equal(pager.textContent, '1–2/3');
  assert.equal(pager.title, '正在显示第 1–2 条，共 3 条；点击切换');
  assert.deepEqual(entryLabels(body), ['5小时', '每周']);
  assert.equal(body.querySelectorAll('.rotating-slot').length, 0, 'default view is unchanged');
  assert.equal(toggle.textContent, '+1');
  pager.click();
  assert.equal(pager.textContent, '3/3');
  assert.equal(pager.title, '正在显示第 3 条，共 3 条；点击切换');
  assert.deepEqual(entryLabels(body), ['每月']);
  assert.equal(body.querySelectorAll('.rotating-slot').length, 2, 'switched rows animate in');
  assert.equal(toggle.textContent, '+1', 'the expand toggle keeps its text');
  pager.click();
  assert.equal(pager.textContent, '1–2/3');
  assert.deepEqual(entryLabels(body), ['5小时', '每周']);
  assert.equal(activeTimers.size, 0, 'manual paging never rotates on its own');
  assert.equal(refits, 0, 'switching must not rebuild the page');
}
{
  reset();
  assert.deepEqual(walk(platformCard(platform(windows), 'ultra', 'ultra'), 4), [
    ['1/3', ['5小时']], ['2/3', ['每周']], ['3/3', ['每月']], ['1/3', ['5小时']]
  ]);
  assert.equal(pagerOf(platformCard(platform(windows), 'normal', 'normal')), null);
  const kimi = platform(windows.slice(0, 2), {name: 'kimi', display_name: 'Kimi Code'});
  assert.deepEqual(walk(platformCard(kimi, 'ultra', 'kimi'), 3),
                   [['1/2', ['5小时']], ['2/2', ['每周']], ['1/2', ['5小时']]]);
  assert.equal(pagerOf(platformCard(kimi, 'compact', 'kimi')), null, 'nothing is hidden');
}
{
  // Multi-plan period cards keep plan headers and the fixed shortest-window total.
  reset();
  const card = platformCard(fixture('kimi'), 'compact', 'plans');
  const pager = pagerOf(card);
  const body = card.querySelector('tbody');
  assert.equal(pager.textContent, '1–2/15');
  assert.deepEqual(planNames(body), ['账户1']);
  assertDailySummary(body);
  pager.click();
  assert.equal(pager.textContent, '3–4/15');
  assert.deepEqual(planNames(body), ['账户1', '账户2']);
  assert.deepEqual(entryLabels(body), ['每月', '每日']);
  assertDailySummary(body);
  assert.equal(card.querySelector('.entry-toggle').textContent, '+13');
}
{
  // Mixed units: credit windows plus a remaining-only wallet row.
  reset();
  const wallet = windowEntry('余额用量', '$', {used: 0, limit: null, remaining: 42,
                                              percent: null});
  const mixed = platform([windowEntry('5小时', 'credits'), wallet],
                         {name: 'opendesign', display_name: 'OpenDesign'});
  assert.deepEqual(walk(platformCard(mixed, 'ultra', 'mixed'), 3),
                   [['1/2', ['5小时']], ['2/2', ['余额用量']], ['1/2', ['5小时']]]);
  assert.equal(pagerOf(platformCard(mixed, 'compact', 'mixed')), null);
}
{
  reset();
  expandedPlatformKeys.add('expanded');
  const card = platformCard(platform(windows), 'compact', 'expanded');
  const pager = pagerOf(card);
  assert.ok(card.classList.contains('is-expanded'));
  assert.ok(pager.hidden, 'expanded cards hide the switch button');
  pager.click();
  assert.equal(pager.textContent, '1–2/3', 'clicks are ignored while expanded');
  card.querySelector('.entry-toggle').click();
  const body = card.querySelector('tbody');
  assert.ok(!pager.hidden);
  assert.equal(card.querySelector('.expanded-entry-grid'), null);
  assert.deepEqual(entryLabels(body), ['5小时', '每周']);
  assert.equal(refits, 1);
  pager.click();
  assert.deepEqual(entryLabels(body), ['每月']);
  assert.equal(refits, 1);
}
{
  reset();
  const failed = platform(windows, {error: '网络错误'});
  assert.equal(pagerOf(platformCard(failed, 'ultra', 'failed')), null);
  // Any card rebuild (refresh, resize, expand/collapse) starts from the default view again.
  renderPlatformsAtMode([platform(windows)], 'compact');
  pagerOf(platformsEl).click();
  assert.equal(pagerOf(platformsEl).textContent, '3/3');
  renderPlatformsAtMode([platform(windows)], 'compact');
  assert.equal(pagerOf(platformsEl).textContent, '1–2/3');
  assert.deepEqual(entryLabels(platformsEl.querySelector('tbody')), ['5小时', '每周']);
}
"""


def test_period_cards_page_hidden_entries_manually() -> None:
    """Collapsed period/mixed-unit cards page through hidden windows via the head button."""
    _run_dashboard_script(_PERIOD_PAGING)


_ULTRA_TOTAL_PAGING = r"""
const wallets = [1, 2, 3].map(n => windowEntry('余额用量', '$', {
  plan: '账号' + n, used: 0, limit: null, remaining: n * 10, percent: null
}));
const balances = platform(wallets, {name: 'opendesign', display_name: 'OpenDesign'});
{
  reset();
  const card = platformCard(balances, 'ultra', 'wallets');
  const pager = pagerOf(card);
  const body = card.querySelector('tbody');
  assert.equal(card.querySelector('.entry-toggle').textContent, '+3');
  assert.equal(pager.title, '正在显示全部合计；点击查看第 1 条');
  const seen = [];
  for (let i = 0; i < 5; i++) {
    seen.push([pager.textContent, summaryLabels(body), entryLabels(body), remainders(body),
               body.querySelectorAll('.rotating-slot').length]);
    pager.click();
  }
  // One row at a time: the total, then each entry, then back to the total.
  assert.deepEqual(seen, [
    ['合计', ['全部合计'], [], ['$60'], 0],
    ['1/3', [], ['余额用量'], ['$10'], 2],
    ['2/3', [], ['余额用量'], ['$20'], 2],
    ['3/3', [], ['余额用量'], ['$30'], 2],
    ['合计', ['全部合计'], [], ['$60'], 2],
  ]);
  assert.equal(activeTimers.size, 0, 'manual paging never rotates on its own');
  assert.equal(refits, 0);
}
{
  reset();
  expandedPlatformKeys.add('wallets');
  assert.ok(pagerOf(platformCard(balances, 'ultra', 'wallets')).hidden);
}
{
  reset();
  assert.equal(pagerOf(platformCard(balances, 'normal', 'wallets')), null);
  const compact = platformCard(balances, 'compact', 'wallets');
  assert.equal(headOf(compact).querySelectorAll('.rotation-indicator').length, 1);
  assert.equal(pagerOf(compact).textContent, '1/3');
  assert.equal(activeTimers.size, 1, 'compact keeps the automatic rotation');
}
"""


def test_ultra_total_only_cards_switch_between_total_and_entries() -> None:
    """Ultra cards that only show 全部合计 cycle total → each entry → total, one row each."""
    _run_dashboard_script(_ULTRA_TOTAL_PAGING)


_SUB2API_TWO_ACCOUNTS = r"""
const pair = fixture('sub2api');
pair.entries = pair.entries.filter(entry => ['账户1', '账户2'].includes(entry.plan));
{
  reset();
  const card = platformCard(pair, 'normal', 'pair');
  const pager = pagerOf(card);
  const body = card.querySelector('tbody');
  assert.equal(headOf(card).querySelectorAll('.rotation-indicator').length, 1);
  assert.equal(card.querySelector('.entry-toggle').textContent, '+4');
  const seen = [];
  for (let i = 0; i < 4; i++) {
    const summaries = body.querySelectorAll('.summary-row');
    assert.deepEqual(summaries.map(row => row.children[0].textContent), ['每日']);
    assert.equal(summaries[0].children[1].children[0].textContent, '$3');
    seen.push([pager.textContent, planNames(body), entryLabels(body)]);
    pager.click();
  }
  // The default view is both accounts' daily rows; then each account's remaining windows.
  assert.deepEqual(seen, [
    ['1–2/6', ['账户1', '账户2'], ['每日', '每日']],
    ['3–4/6', ['账户1'], ['每周', '每月']],
    ['5–6/6', ['账户2'], ['每周', '每月']],
    ['1–2/6', ['账户1', '账户2'], ['每日', '每日']],
  ]);
  assert.equal(activeTimers.size, 0, 'manual paging never rotates on its own');
  assert.equal(refits, 0);
}
for (const mode of ['compact', 'ultra']) {
  reset();
  assert.equal(pagerOf(platformCard(pair, mode, 'pair')).textContent, '1/2');
  assert.equal(activeTimers.size, 1, mode + ' keeps the automatic account rotation');
}
"""


def test_sub2api_two_accounts_normal_pages_remaining_windows() -> None:
    """Two Sub2API accounts in normal density page through their weekly/monthly rows."""
    _run_dashboard_script(_SUB2API_TWO_ACCOUNTS)
