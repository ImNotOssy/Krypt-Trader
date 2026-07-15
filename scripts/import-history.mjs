import { ensureVenv, run, VENV_PY } from './python-utils.mjs';

ensureVenv();
run(VENV_PY, ['historical_data.py', ...process.argv.slice(2)]);
