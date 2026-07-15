import { existsSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { ensureVenv, run, VENV_PY, PY_DIR } from './python-utils.mjs';

ensureVenv();

console.log('>> Installing PyInstaller');
run(VENV_PY, ['-m', 'pip', 'install', 'pyinstaller>=6.6,<7', '--disable-pip-version-check']);

console.log('>> Cleaning previous build');
for (const d of ['build', 'dist']) {
  const p = join(PY_DIR, d);
  if (existsSync(p)) {
    rmSync(p, { recursive: true, force: true });
  }
}

console.log('>> Running PyInstaller (this takes ~60s)');
run(VENV_PY, [
  '-m', 'PyInstaller',
  '--noconfirm',
  '--name', 'krypt-trader-backend',
  '--console',
  '--hidden-import', 'db',
  '--hidden-import', 'scanner',
  '--hidden-import', 'trader',
  '--hidden-import', 'kalshi_api',
  '--hidden-import', 'kalshi_auth',
  '--hidden-import', 'categorize',
  '--hidden-import', 'config',
  '--hidden-import', 'webhook',
  '--hidden-import', 'kalshi_ws',
  '--hidden-import', 'crypto15m',
  '--hidden-import', 'crypto15m_trader',
  '--hidden-import', 'crypto15m_record',
  '--hidden-import', 'backtest',
  '--hidden-import', 'spot_ws',
  '--hidden-import', 'cf_ws',
  '--hidden-import', 'indicators',
  '--hidden-import', 'replay',
  '--hidden-import', 'historical_data',
  '--hidden-import', 'optimizer',
  '--hidden-import', 'ai_research',
  '--hidden-import', 'rules',
  '--hidden-import', 'kalshi_perps_api',
  '--hidden-import', 'perps_ws',
  '--hidden-import', 'perps_record',
  '--hidden-import', 'perps_farmer',
  '--hidden-import', 'perps_strategy',
  '--collect-submodules', 'cryptography',
  '--collect-submodules', 'httpx',
  '--collect-submodules', 'websockets',
  'service.py',
]);

const out = join(PY_DIR, 'dist', 'krypt-trader-backend');
if (!existsSync(out)) {
  console.error('!! PyInstaller did not produce', out);
  process.exit(1);
}
console.log('>> OK \u2014 backend bundled at python/dist/krypt-trader-backend');
