import assert from 'node:assert/strict';
import { rmSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import * as esbuild from 'esbuild';

const outFile = join(process.cwd(), '.tmp-profile-importer-test.mjs');

const sample = `version: 1
platform: kalshi
strategy: custom
market:
    series_ticker: KXBTC15M
risk:
    max_position: 5
    price_floor: 0.05
    price_ceiling: 0.59
loop:
    interval: 10
edge:
    btc:
        provider: coinbase
        symbol: BTC-USD
        fields:
            - price
            - ema_12_1m
            - sma_20_1m
            - sma_50_5m
        refresh: 10s
rules:
    - name: bullish_entry
      when:
        all:
            - field: position_size
              op: ==
              value: 0
            - field: time_to_expiry
              op: '>'
              value: 2m
            - field: price
              op: <=
              value: 0.5
            - field: edge.btc.ema_12_1m
              op: '>'
              value_field: edge.btc.sma_20_1m
            - field: edge.btc.price
              op: '>'
              value_field: edge.btc.sma_50_5m
      action: buy_yes
      size: 5
    - name: bearish_entry
      when:
        all:
            - field: position_size
              op: ==
              value: 0
            - field: time_to_expiry
              op: '>'
              value: 2m
            - field: price
              op: '>='
              value: 0.5
            - field: edge.btc.ema_12_1m
              op: <
              value_field: edge.btc.sma_20_1m
            - field: edge.btc.price
              op: <
              value_field: edge.btc.sma_50_5m
      action: buy_no
      size: 5
    - name: time_stop_loss
      when:
        all:
            - field: price
              op: <=
              value: 0.1
            - field: time_to_expiry
              op: <=
              value: 1m
      action: sell_all
    - name: settle_at_close
      when:
        all:
            - field: time_to_expiry
              op: <=
              value: 5s
      action: sell_all
`;

await esbuild.build({
  entryPoints: ['shared/profile-importer.ts'],
  outfile: outFile,
  bundle: true,
  platform: 'node',
  format: 'esm',
  logLevel: 'silent',
});

try {
  const mod = await import(`${pathToFileURL(outFile).href}?v=${Date.now()}`);
  const result = mod.translateExternalStrategyProfile(sample);

  assert.equal(result.ok, true, result.message);
  assert.equal(result.data.kind, 'crypto15m');
  assert.equal(result.data.name, 'Imported KXBTC15M strategy');

  const cfg = result.data.config;
  assert.equal(cfg.crypto15mStrategyMode, 'btc_ma_crossover');
  assert.deepEqual(cfg.crypto15mAssets, ['BTC']);
  assert.equal(cfg.crypto15mOrderSize, 5);
  assert.equal(cfg.crypto15mMaxConcurrent, 1);
  assert.equal(cfg.crypto15mPollSec, 10);
  assert.equal(cfg.crypto15mEntryThreshold, 0.05);
  assert.equal(cfg.crypto15mEntryMax, 0.59);
  assert.equal(cfg.crypto15mMinEntryCents, 5);
  assert.equal(cfg.crypto15mMaxEntryCents, 59);
  assert.equal(cfg.crypto15mMinEntrySecondsLeft, 120);
  assert.equal(cfg.crypto15mTimeStopCents, 10);
  assert.equal(cfg.crypto15mTimeStopSecondsLeft, 60);
  assert.equal(cfg.crypto15mForceExitSecondsLeft, 5);
  assert.equal(cfg.crypto15mFastEmaPeriod, 12);
  assert.equal(cfg.crypto15mSlowSmaPeriod, 20);
  assert.equal(cfg.crypto15mTrendSmaPeriod, 50);
  assert.equal(cfg.crypto15mTrendTimeframeMin, 5);
  assert.equal(cfg.crypto15mIndicatorDetect, true);
  assert.equal(cfg.crypto15mSpotWs, true);
  assert.equal(cfg.crypto15mUseRules, false);
} finally {
  rmSync(outFile, { force: true });
}
