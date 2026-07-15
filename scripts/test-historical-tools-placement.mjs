import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const backtest = readFileSync('src/pages/Backtest.tsx', 'utf8');
const settings = readFileSync('src/pages/Settings.tsx', 'utf8');

for (const text of ['Download Coinbase', 'Download Kalshi', 'Validate dataset']) {
  assert.equal(
    backtest.includes(text),
    false,
    `Backtest page should not render "${text}"`,
  );
  assert.equal(
    settings.includes(text),
    true,
    `Settings page should render "${text}"`,
  );
}

assert.equal(backtest.includes('Historical data'), false);
assert.equal(settings.includes('Historical data'), true);
