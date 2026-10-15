"""Exercise dashboard formatting in Node without browser or network dependencies."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_wallet_rows_and_totals_keep_balance_semantics() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed to exercise dashboard JavaScript")
    dashboard = Path(__file__).resolve().parents[1] / "src/llm_usage/static/index.html"
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[1], 'utf8');
// Load the page's actual formatting/aggregation and row renderer, without its polling loop.
vm.runInThisContext(html.slice(html.indexOf('function p2('),
                              html.indexOf('// ---- LLM Gateway')));
vm.runInThisContext(html.slice(html.indexOf('function dataRow('),
                              html.indexOf('function barRow(')));
global.document = {
  createElement(tag) {
    return {
      tag, children: [], attributes: {}, dataset: {}, textContent: '',
      appendChild(child) { this.children.push(child); },
      setAttribute(name, value) { this.attributes[name] = value; }
    };
  }
};
function wallet(balance, unit = '$') {
  return {label: '余额', used: 0, limit: null, percent: null, remaining: balance, unit};
}
function checkRow(entry, fullBalance, compactBalance) {
  const cells = dataRow(entry).children;
  assert.equal(cells[1].children[0].textContent, '-');
  assert.equal(cells[1].children[1].textContent, '-/-');
  assert.equal(cells[1].attributes['aria-label'], '已用 -，限额 -');
  assert.equal(cells[2].textContent, '-');
  assert.equal(cells[3].children[0].textContent, fullBalance);
  assert.equal(cells[3].children[1].textContent, compactBalance);
  assert.equal(cells[3].attributes['aria-label'], '剩余 ' + fullBalance);
}
checkRow(wallet(1250), '$1250', '$1.25k');
checkRow(wallet(0), '$0', '$0');
checkRow(wallet(-0.5), '$-0.5', '$-0.5');
const total = totalEntry([wallet(1250), wallet(250)]);
assert.equal(total.label, '全部合计');
assert.equal(total.remaining, 1500);
assert.equal(total.limit, null);
assert.equal(total.percent, null);
checkRow(total, '$1500', '$1.5k');
assert.equal(totalEntry([wallet(1), wallet(-0.5)]).remaining, 0.5);
assert.equal(isBalanceOnly(wallet(0, 'tokens')), true);
const mixed = totalEntry([wallet(1), wallet(2, 'tokens')]);
assert.equal(mixed.balance_only, false);
assert.equal(mixed.remaining, null);
// Existing usage and known quotas retain their formatting, even with zero consumption.
const usage = {...wallet(50), label: '每日', remaining: null};
assert.equal(dataRow(usage).children[1].children[0].textContent, '$0');
assert.equal(dataRow(usage).children[2].textContent, '∞');
const quota = {...wallet(100), limit: 100, percent: 0};
assert.equal(isBalanceOnly(quota), false);
assert.equal(dataRow(quota).children[2].textContent, '$100');
"""
    subprocess.run([node, "-e", script, str(dashboard)], check=True, capture_output=True, text=True)
