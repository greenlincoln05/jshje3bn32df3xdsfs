const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');
const html = fs.readFileSync('src/btcbot/dashboard.html', 'utf8');
const source = html.slice(
  html.indexOf('// ---------------------------------------------------------------- perps (paper)'),
  html.indexOf('async function loadSettings() {'),
);

function makeContext(elements, checked) {
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const el = (id) => (elements[id] ??= { value: '', innerHTML: '', textContent: '', hidden: true, className: '' });
  const document = {
    querySelectorAll: (sel) => (sel === '#perp-strategies input:checked' ? checked || [] : []),
  };
  const context = vm.createContext({ $: el, $q: (sel) => el(sel.replace('#', '').replace(' tbody', '')), esc, document, Number, String });
  vm.runInContext(source, context);
  return context;
}

// perpPayload(): comma lists parsed and trimmed, checked strategy boxes collected.
{
  const elements = {
    'perp-db-select': { value: 'history-KXBTC15M-prod-20260101T000000Z.sqlite' },
    'perp-account': { value: '750' },
    'perp-leverages': { value: ' 1, 2 ,3' },
    'perp-fundings': { value: '0,0.0001' },
    'perp-split': { value: '0.7' },
    'perp-fee-bps': { value: '12' },
    'perp-slippage-bps': { value: '1' },
    'perp-maintenance-frac': { value: '0.9' },
  };
  const checked = [{ value: 'flat' }, { value: 'hold' }];
  const ctx = makeContext(elements, checked);
  const payload = vm.runInContext('JSON.stringify(perpPayload())', ctx);
  assert.equal(payload, JSON.stringify({
    db: 'history-KXBTC15M-prod-20260101T000000Z.sqlite', account: '750',
    leverages: ['1', '2', '3'], fundings: ['0', '0.0001'], strategies: ['flat', 'hold'],
    split: '0.7', fee_bps: '12', slippage_bps: '1', maintenance_frac: '0.9',
  }));
}

// renderPerpReport(): spec summary, returns table, and verdicts render from a saved/completed report shape.
{
  const elements = {};
  const ctx = makeContext(elements, []);
  const report = {
    source: 'Coinbase BTC-USD 1m candles (spot_candles)', bars: 4320, train_end: '2026-01-03T00:00:00+00:00',
    spec: {
      taker_fee_bps: '12.0', maintenance_frac: '0.9', funding_cap: '0.02', funding_deadband: '0.0001',
      half_spread_bps: '1.0', liq_slippage_bps: '25.0',
    },
    rows: [{
      segment: 'test', strategy: 'flat', leverage: '1', funding_8h: '0', return_pct: 0, max_drawdown_pct: 0,
      trades: 0, fees_paid: '0.00', funding_paid: '0.00', liquidations: 0, exposure_pct: 0,
    }],
    verdicts: [{ strategy: 'flat', leverage: '1', funding_8h: '0', verdict: 'insufficient data: 2 test days, need at least 30 before any verdict' }],
  };
  vm.runInContext('renderPerpReport(REPORT)', Object.assign(ctx, { REPORT: report }));
  assert.match(elements['perp-spec-summary'].innerHTML, /taker fee 12\.0 bps/);
  assert.match(elements['perp-table'].innerHTML, /<td>test<\/td><td>flat<\/td>/);
  assert.match(elements['perp-table'].innerHTML, /0\.00%/);
  assert.match(elements['perp-verdicts'].innerHTML, /insufficient data/);
  assert.equal(elements['perp-results'].hidden, false);
  assert.equal(elements['perp-disclaimer'].hidden, false);
}

console.log('Dashboard perps tab: payload parsing and report rendering checks passed');
