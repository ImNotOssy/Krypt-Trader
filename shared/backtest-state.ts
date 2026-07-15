import type { TraderConfig } from './types';

type Jsonish =
  | null
  | boolean
  | number
  | string
  | Jsonish[]
  | { [key: string]: Jsonish | undefined };

export interface BacktestInputKeyArgs {
  engine: string;
  selection: string;
  days: number;
  patch: Partial<TraderConfig>;
  currentConfig?: Partial<TraderConfig> | null;
  optimizerMode?: string | null;
}

function stable(value: unknown): Jsonish {
  if (value == null) return null;
  if (Array.isArray(value)) return value.map(stable);
  if (typeof value !== 'object') {
    if (typeof value === 'number') return Number.isFinite(value) ? value : null;
    if (typeof value === 'boolean' || typeof value === 'string') return value;
    return String(value);
  }
  const out: { [key: string]: Jsonish | undefined } = {};
  for (const key of Object.keys(value as Record<string, unknown>).sort()) {
    const v = (value as Record<string, unknown>)[key];
    if (typeof v !== 'undefined') out[key] = stable(v);
  }
  return out;
}

export function backtestInputKey(args: BacktestInputKeyArgs): string {
  return JSON.stringify(stable(args));
}

export function isStaleBacktestResult(runKey: string, currentKey: string): boolean {
  return runKey !== currentKey;
}
