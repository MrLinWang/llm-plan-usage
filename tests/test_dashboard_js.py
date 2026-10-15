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
                              html.indexOf('// ---- Sub2API')));
vm.runInThisContext(html.slice(html.indexOf('function dataRow('),
                              html.indexOf('function appendEntries(')));
vm.runInThisContext(html.slice(html.indexOf('function appendSummaryRows('),
                              html.indexOf('function appendTotalBlock(')));
global.document = {
  createElement(tag) {
    return {
      tag, children: [], attributes: {}, dataset: {}, style: {}, textContent: '',
      classList: {add() {}},
      appendChild(child) { this.children.push(child); },
      setAttribute(name, value) { this.attributes[name] = value; }
    };
  }
};
function wallet(balance, unit = '$') {
  return {label: '余额用量', used: 0, limit: null, percent: null, remaining: balance, unit};
}
function checkRow(entry, fullBalance, compactBalance, maximum = '-') {
  const cells = dataRow(entry).children;
  assert.equal(cells[1].children[0].textContent, '-');
  assert.equal(cells[1].children[1].textContent, '-/' + maximum);
  assert.equal(cells[1].attributes['aria-label'],
               '已用 -，' + (maximum === '-' ? '限额 ' : '最大余额 ') + maximum);
  assert.equal(cells[2].textContent, maximum);
  assert.equal(cells[3].children[0].textContent, fullBalance);
  assert.equal(cells[3].children[1].textContent, compactBalance);
  assert.equal(cells[3].attributes['aria-label'], '剩余 ' + fullBalance);
}
checkRow(wallet(1250), '$1250', '$1.25k');
assert.equal(dataRow(wallet(1250)).children[0].textContent, '余额用量');
const legacyWallet = {...wallet(1250), label: '余额'};
assert.equal(isBalanceOnly(legacyWallet), true);
assert.equal(dataRow(legacyWallet).children[0].textContent, '余额用量');
checkRow(legacyWallet, '$1250', '$1.25k');
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
const quota = {...wallet(100), label: '每日', limit: 100, percent: 0};
assert.equal(isBalanceOnly(quota), false);
assert.equal(dataRow(quota).children[2].textContent, '$100');

const scaled = {...wallet(25), limit: 100, percent: 75};
checkRow(scaled, '$25', '$25', '$100');
const scaledTotal = totalEntry([scaled, {...scaled, remaining: 75, percent: 25}]);
assert.equal(scaledTotal.remaining, 100);
assert.equal(scaledTotal.limit, 200);
assert.equal(scaledTotal.percent, 50);
checkRow(scaledTotal, '$100', '$100', '$200');
const partial = totalEntry([scaled, wallet(10)]);
assert.equal(partial.remaining, 35);
assert.equal(partial.limit, null);
assert.equal(partial.percent, null);
checkRow(partial, '$35', '$35');
checkRow({...scaled, remaining: 150, percent: 0}, '$150', '$150', '$100');
checkRow({...scaled, remaining: -2, percent: 100}, '$-2', '$-2', '$100');
assert.equal(totalEntry([scaled, scaled]).percent, 75);
const full = {...scaled, remaining: 100, percent: 0};
assert.equal(totalEntry([full, full]).percent, 0);
assert.equal(totalEntry([{...full, remaining: 0}, {...full, remaining: 0}]).percent, 100);
function checkBar(percent, balance, color, text, row) {
  const wrap = (row || barRow(percent, balance)).children[0].children[0];
  const fill = wrap.children[0].children[0];
  assert.equal(fill.className, 'bar-fill ' + color);
  assert.equal(fill.style.width, percent + '%');
  assert.equal(wrap.children[1].textContent, text);
}
checkBar(25, true, 'green', '25%');
checkBar(80, true, 'yellow', '80%');
checkBar(95, true, 'red', '95%');
checkBar(0, true, 'green', '0%');
checkBar(100, true, 'red', '100%');
checkBar(95, false, 'red', '95%');
assert.equal(barRow(null, true).children[0].children.length, 0);
const summary = document.createElement('tbody');
appendSummaryRows(summary, scaledTotal);
assert.equal(summary.children.length, 2);
checkBar(50, true, 'green', '50%', summary.children[1]);
const fullBalanceRows = document.createElement('tbody');
appendEntryRows(fullBalanceRows, full);
checkBar(0, true, 'green', '0%', fullBalanceRows.children[1]);
const fullBalanceTotals = document.createElement('tbody');
appendSummaryRows(fullBalanceTotals, totalEntry([full, full]));
checkBar(0, true, 'green', '0%', fullBalanceTotals.children[1]);
"""
    subprocess.run([node, "-e", script, str(dashboard)], check=True, capture_output=True, text=True)
