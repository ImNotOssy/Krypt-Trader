import { ensureVenv, run, VENV_PY } from './python-utils.mjs';

ensureVenv();
run(VENV_PY, ['optimizer.py', ...process.argv.slice(2)]);
