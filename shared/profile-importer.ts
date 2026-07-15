import type { ProfileKind, TraderConfig } from './types';

export interface ExternalStrategyTranslation {
  name: string;
  description: string;
  kind: ProfileKind;
  config: Partial<TraderConfig>;
}

export type ExternalStrategyTranslationResult =
  | { ok: true; data: ExternalStrategyTranslation }
  | { ok: false; message: string };

const unquote = (value: string): string =>
  value.trim().replace(/^['"]|['"]$/g, '').trim();

function scalar(input: string, key: string): string | null {
  const escaped = key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const m = input.match(new RegExp(`^\\s*${escaped}\\s*:\\s*(.+?)\\s*$`, 'im'));
  if (!m) return null;
  const raw = m[1].split(/\s+#/, 1)[0];
  return unquote(raw);
}

function numberScalar(input: string, key: string): number | null {
  const v = scalar(input, key);
  if (v == null || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function durationSeconds(value: string | null): number | null {
  if (!value) return null;
  const m = unquote(value).match(/^([0-9]+(?:\.[0-9]+)?)(ms|s|m|h)?$/i);
  if (!m) return null;
  const n = Number(m[1]);
  if (!Number.isFinite(n)) return null;
  const unit = (m[2] || 's').toLowerCase();
  if (unit === 'ms') return n / 1000;
  if (unit === 'm') return n * 60;
  if (unit === 'h') return n * 3600;
  return n;
}

function durationScalarSeconds(input: string, key: string): number | null {
  return durationSeconds(scalar(input, key));
}

function priceToCents(price: number | null): number | null {
  if (price == null) return null;
  const cents = Math.round(price * 100);
  if (!Number.isFinite(cents)) return null;
  return Math.max(1, Math.min(99, cents));
}

function maxRuleSize(input: string): number | null {
  const sizes = Array.from(input.matchAll(/^\s*size\s*:\s*([0-9]+)\s*$/gim))
    .map((m) => Number(m[1]))
    .filter((n) => Number.isFinite(n) && n > 0);
  return sizes.length ? Math.max(...sizes) : null;
}

function ruleSection(input: string, name: string): string {
  const lines = input.split(/\r?\n/);
  const start = lines.findIndex((line) =>
    new RegExp(`^\\s*-\\s*name\\s*:\\s*${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*$`, 'i').test(line));
  if (start < 0) return '';
  const out: string[] = [];
  for (let i = start + 1; i < lines.length; i += 1) {
    if (/^\s*-\s*name\s*:/i.test(lines[i])) break;
    out.push(lines[i]);
  }
  return out.join('\n');
}

function conditionDuration(input: string, field: string, op: string): number | null {
  const escapedField = field.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const escapedOp = op.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp(
    `field\\s*:\\s*${escapedField}[\\s\\S]{0,160}?op\\s*:\\s*['"]?${escapedOp}['"]?[\\s\\S]{0,160}?value\\s*:\\s*([^\\s\\r\\n]+)`,
    'i',
  );
  return durationSeconds(input.match(re)?.[1] ?? null);
}

function conditionNumber(input: string, field: string, op: string): number | null {
  const escapedField = field.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const escapedOp = op.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp(
    `field\\s*:\\s*${escapedField}[\\s\\S]{0,160}?op\\s*:\\s*['"]?${escapedOp}['"]?[\\s\\S]{0,160}?value\\s*:\\s*([^\\s\\r\\n]+)`,
    'i',
  );
  const match = input.match(re);
  if (!match) return null;
  const n = Number(unquote(match[1]));
  return Number.isFinite(n) ? n : null;
}

function period(input: string, pattern: RegExp): number | null {
  const m = input.match(pattern);
  if (!m) return null;
  const n = Number(m[1]);
  return Number.isFinite(n) ? n : null;
}

function isBtcMaCrossover(input: string): boolean {
  const lc = input.toLowerCase();
  return (
    lc.includes('ema_12_1m') &&
    lc.includes('sma_20_1m') &&
    lc.includes('sma_50_5m') &&
    lc.includes('buy_yes') &&
    lc.includes('buy_no')
  );
}

export function translateExternalStrategyProfile(input: string): ExternalStrategyTranslationResult {
  const text = input.trim();
  if (!text) return { ok: false, message: 'Profile text is empty' };

  const platform = scalar(text, 'platform')?.toLowerCase();
  if (platform && platform !== 'kalshi') {
    return { ok: false, message: `Unsupported strategy platform "${platform}"` };
  }

  const seriesTicker = (scalar(text, 'series_ticker') || '').toUpperCase();
  const symbol = (scalar(text, 'symbol') || '').toUpperCase();
  const isBtc15m = seriesTicker === 'KXBTC15M' || symbol === 'BTC-USD';
  if (!isBtc15m || !isBtcMaCrossover(text)) {
    return {
      ok: false,
      message: 'Only BTC Kalshi 15m EMA/SMA crossover strategy profiles are supported for paste import right now.',
    };
  }

  const priceFloor = numberScalar(text, 'price_floor') ?? 0.05;
  const priceCeiling = numberScalar(text, 'price_ceiling') ?? 0.59;
  const minEntrySeconds = conditionDuration(text, 'time_to_expiry', '>') ?? 120;
  const timeStop = ruleSection(text, 'time_stop_loss');
  const forceClose = ruleSection(text, 'settle_at_close');
  const timeStopPrice = conditionNumber(timeStop, 'price', '<=') ?? 0.1;
  const timeStopSeconds = conditionDuration(timeStop, 'time_to_expiry', '<=') ?? 60;
  const forceExitSeconds = conditionDuration(forceClose, 'time_to_expiry', '<=') ?? 5;
  const riskMaxPosition = numberScalar(text, 'max_position');
  const size = Math.max(1, Math.round(maxRuleSize(text) ?? riskMaxPosition ?? 1));
  const pollSec = Math.max(
    2,
    Math.round(durationScalarSeconds(text, 'interval') ?? durationScalarSeconds(text, 'refresh') ?? 10),
  );

  const fastEma = period(text, /ema_([0-9]+)_1m/i) ?? 12;
  const slowSma = period(text, /sma_([0-9]+)_1m/i) ?? 20;
  const smaFields = Array.from(text.matchAll(/sma_([0-9]+)_([0-9]+)m/gi))
    .map((m) => ({ period: Number(m[1]), timeframe: Number(m[2]) }))
    .filter((m) => Number.isFinite(m.period) && Number.isFinite(m.timeframe));
  const trend = smaFields.find((m) => m.timeframe > 1) ?? smaFields.at(-1);
  const trendSma = trend?.period ?? 50;
  const trendTimeframe = trend?.timeframe ?? 5;

  return {
    ok: true,
    data: {
      name: `Imported ${seriesTicker || 'KXBTC15M'} strategy`,
      description: 'Imported Kalshi BTC 15m EMA/SMA crossover strategy. Backtest and paper-trade before enabling live orders.',
      kind: 'crypto15m',
      config: {
        crypto15mStrategyMode: 'btc_ma_crossover',
        crypto15mAssets: ['BTC'],
        crypto15mOrderSize: size,
        crypto15mMaxConcurrent: 1,
        crypto15mPollSec: pollSec,
        crypto15mTimeDelayMin: 15,
        crypto15mDirectionMode: 'favorite',
        crypto15mDirectionalEnabled: true,
        crypto15mPairsEnabled: false,
        crypto15mUseRules: false,
        crypto15mRules: [],
        crypto15mEntryThreshold: priceFloor,
        crypto15mStrictThreshold: false,
        crypto15mEntryMax: priceCeiling,
        crypto15mMinDeltaPct: 0,
        crypto15mIndicatorDetect: true,
        crypto15mSpotWs: true,
        crypto15mEntryStyle: 'maker',
        crypto15mMakerCancelMin: 2,
        crypto15mEntryDiff: 0,
        crypto15mExitThreshold: 0,
        crypto15mTakeProfitCents: 0,
        crypto15mStopLossPct: 0,
        crypto15mFastEmaPeriod: fastEma,
        crypto15mSlowSmaPeriod: slowSma,
        crypto15mTrendSmaPeriod: trendSma,
        crypto15mTrendTimeframeMin: trendTimeframe,
        crypto15mMinEntryCents: priceToCents(priceFloor) ?? 5,
        crypto15mMaxEntryCents: priceToCents(priceCeiling) ?? 59,
        crypto15mMinEntrySecondsLeft: Math.round(minEntrySeconds),
        crypto15mTimeStopCents: priceToCents(timeStopPrice) ?? 10,
        crypto15mTimeStopSecondsLeft: Math.round(timeStopSeconds),
        crypto15mForceExitSecondsLeft: Math.round(forceExitSeconds),
      },
    },
  };
}
