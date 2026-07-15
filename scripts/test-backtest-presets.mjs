import assert from 'node:assert/strict';
import { rmSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import * as esbuild from 'esbuild';

const outFile = join(process.cwd(), '.tmp-backtest-presets-test.mjs');

await esbuild.build({
  stdin: {
    contents: `
      export * from './shared/crypto15m-presets';
      export * from './shared/backtest-state';
    `,
    resolveDir: process.cwd(),
    sourcefile: 'backtest-presets-test-entry.ts',
    loader: 'ts',
  },
  outfile: outFile,
  bundle: true,
  platform: 'node',
  format: 'esm',
  logLevel: 'silent',
});

try {
  const mod = await import(`${pathToFileURL(outFile).href}?v=${Date.now()}`);

  const presetIds = mod.CRYPTO15M_PRESETS.map((p) => p.id);
  assert.deepEqual(presetIds, [
    'favorite',
    'contrarian',
    'momentum',
    'fav-90-95',
    'sniper',
    'btc-ma-crossover',
    'macd-trend',
  ]);

  assert.deepEqual(
    mod.CRYPTO15M_BACKTEST_CHOICES.map((p) => p.id),
    ['current', ...presetIds],
  );

  const sniper = mod.CRYPTO15M_PRESETS.find((p) => p.id === 'sniper');
  assert.equal(sniper.patch.crypto15mModelMinProb, 0.5);
  assert.equal(sniper.patch.crypto15mModelMinEdgeCents, 0);

  for (const preset of mod.CRYPTO15M_PRESETS.filter((p) => p.id !== 'btc-ma-crossover')) {
    assert.equal(preset.patch.crypto15mStrategyMode, 'directional', `${preset.id} resets strategy mode`);
    assert.equal(preset.patch.crypto15mAssets, null, `${preset.id} resets asset filter`);
    assert.equal(preset.patch.crypto15mDirectionalEnabled, true, `${preset.id} keeps directional engine on`);
    assert.equal(preset.patch.crypto15mPairsEnabled, false, `${preset.id} keeps pairs off`);
  }

  const currentKey = mod.backtestInputKey({
    engine: 'crypto15m',
    selection: 'current',
    days: 30,
    patch: {},
    currentConfig: { crypto15mDirectionMode: 'favorite', crypto15mEntryThreshold: 0.95 },
  });
  const presetKey = mod.backtestInputKey({
    engine: 'crypto15m',
    selection: 'preset:momentum',
    days: 30,
    patch: { crypto15mDirectionMode: 'favorite', crypto15mMinDeltaPct: 0.002 },
    currentConfig: null,
  });
  const windowKey = mod.backtestInputKey({
    engine: 'crypto15m',
    selection: 'current',
    days: 60,
    patch: {},
    currentConfig: { crypto15mDirectionMode: 'favorite', crypto15mEntryThreshold: 0.95 },
  });

  assert.notEqual(currentKey, presetKey);
  assert.notEqual(currentKey, windowKey);
  assert.equal(mod.isStaleBacktestResult(currentKey, currentKey), false);
  assert.equal(mod.isStaleBacktestResult(currentKey, presetKey), true);
} finally {
  rmSync(outFile, { force: true });
}
