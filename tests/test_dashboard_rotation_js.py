"""Run the dashboard's actual card renderer and timers against period-account data."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_sub2api_collapsed_accounts_rotate_without_mixing_period_totals() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed to exercise dashboard JavaScript")
    dashboard = Path(__file__).resolve().parents[1] / "src/llm_usage/static/index.html"
    script = r"""
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
const other = platformCard(fixture('kimi'), 'compact', 'other');
assert.equal(activeTimers.size, 0, 'other period platforms retain their existing layout');
assert.equal(other.querySelector('.rotation-indicator'), null);
const singlePlatform = fixture('sub2api');
singlePlatform.entries = singlePlatform.entries.filter(entry => entry.plan === '账户1');
assert.equal(platformCard(singlePlatform, 'compact', 'single')
             .querySelector('.rotation-indicator'), null);

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
    result = subprocess.run(
        [node, "-e", script, str(dashboard)], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
