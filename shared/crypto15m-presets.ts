import type { TraderConfig } from './types';

export interface Crypto15mPreset {
  id: string;
  name: string;
  hint: string;
  patch: Partial<TraderConfig>;
}

const DIRECTIONAL_BASE: Partial<TraderConfig> = {
  crypto15mStrategyMode: 'directional',
  crypto15mAssets: null,
  crypto15mDirectionalEnabled: true,
  crypto15mPairsEnabled: false,
};

export const CRYPTO15M_PRESETS: Crypto15mPreset[] = [
  {
    id: 'favorite',
    name: 'Deep Favorite',
    hint: "Only the deepest favorites (95-98c) - the one band that didn't lose in collected data (small sample).",
    patch: {
      ...DIRECTIONAL_BASE,
      crypto15mDirectionMode: 'favorite',
      crypto15mEntryThreshold: 0.95,
      crypto15mEntryMax: 0.98,
      crypto15mMinDeltaPct: 0,
      crypto15mExitThreshold: 0.4,
      crypto15mEntryStyle: 'maker',
      crypto15mUseRules: false,
      crypto15mRules: [],
    },
  },
  {
    id: 'contrarian',
    name: 'Contrarian Fade',
    hint: 'Fade extreme favorites - buy the cheap side, hold to settle. Measured roughly break-even.',
    patch: {
      ...DIRECTIONAL_BASE,
      crypto15mDirectionMode: 'contrarian',
      crypto15mEntryThreshold: 0.9,
      crypto15mEntryMax: 0.98,
      crypto15mMinDeltaPct: 0,
      crypto15mExitThreshold: 0,
      crypto15mEntryStyle: 'maker',
      crypto15mUseRules: false,
      crypto15mRules: [],
    },
  },
  {
    id: 'momentum',
    name: 'Momentum (Delta-confirmed)',
    hint: 'Buy the favorite only once the underlying has already moved at least 0.2% this window.',
    patch: {
      ...DIRECTIONAL_BASE,
      crypto15mDirectionMode: 'favorite',
      crypto15mEntryThreshold: 0.80,
      crypto15mEntryMax: 0.98,
      crypto15mMinDeltaPct: 0.002,
      crypto15mExitThreshold: 0.4,
      crypto15mEntryStyle: 'maker',
      crypto15mUseRules: false,
      crypto15mRules: [],
    },
  },
  {
    id: 'fav-90-95',
    name: 'Favorite 90-95c',
    hint: 'Favorites in the 90-95c pocket. Caveat: priced off the mid - unconfirmed on real fills near close.',
    patch: {
      ...DIRECTIONAL_BASE,
      crypto15mDirectionMode: 'favorite',
      crypto15mEntryThreshold: 0.90,
      crypto15mEntryMax: 0.95,
      crypto15mMinDeltaPct: 0,
      crypto15mExitThreshold: 0.4,
      crypto15mEntryStyle: 'maker',
      crypto15mUseRules: false,
      crypto15mRules: [],
    },
  },
  {
    id: 'sniper',
    name: 'Settlement Sniper',
    hint: 'Model mode: buys whichever side the live settlement model favors when your certainty and edge thresholds are met.',
    patch: {
      ...DIRECTIONAL_BASE,
      crypto15mUseRules: false,
      crypto15mRules: [],
      crypto15mDirectionMode: 'model',
      crypto15mModelMinProb: 0.5,
      crypto15mModelMinEdgeCents: 0,
      crypto15mIndicatorDetect: true,
      crypto15mSpotWs: true,
    },
  },
  {
    id: 'btc-ma-crossover',
    name: 'BTC MA Crossover',
    hint: 'BTC-only: EMA12(1m) vs SMA20(1m), confirmed by spot vs SMA50(5m).',
    patch: {
      crypto15mStrategyMode: 'btc_ma_crossover',
      crypto15mAssets: ['BTC'],
      crypto15mOrderSize: 5,
      crypto15mMaxConcurrent: 1,
      crypto15mTimeDelayMin: 15,
      crypto15mDirectionMode: 'favorite',
      crypto15mDirectionalEnabled: true,
      crypto15mPairsEnabled: false,
      crypto15mUseRules: false,
      crypto15mRules: [],
      crypto15mEntryThreshold: 0.05,
      crypto15mStrictThreshold: false,
      crypto15mEntryMax: 0.59,
      crypto15mMinDeltaPct: 0,
      crypto15mMaxLossPct: 0.05,
      crypto15mMaxTotalPct: 0.05,
      crypto15mSessionTakeProfitUsd: 0,
      crypto15mIndicatorDetect: true,
      crypto15mSpotWs: true,
      crypto15mEntryStyle: 'maker',
      crypto15mMakerCancelMin: 2,
      crypto15mEntryDiff: 0,
      crypto15mExitThreshold: 0,
      crypto15mTakeProfitCents: 0,
      crypto15mStopLossPct: 0,
      crypto15mFastEmaPeriod: 12,
      crypto15mSlowSmaPeriod: 20,
      crypto15mTrendSmaPeriod: 50,
      crypto15mTrendTimeframeMin: 5,
      crypto15mMinEntryCents: 5,
      crypto15mMaxEntryCents: 59,
      crypto15mMinEntrySecondsLeft: 120,
      crypto15mTimeStopCents: 10,
      crypto15mTimeStopSecondsLeft: 60,
      crypto15mForceExitSecondsLeft: 5,
    },
  },
  {
    id: 'macd-trend',
    name: 'MACD Trend (rules)',
    hint: 'Experimental: enter Up when Up is favored and the 1-min underlying MACD histogram is bullish.',
    patch: {
      ...DIRECTIONAL_BASE,
      crypto15mDirectionMode: 'favorite',
      crypto15mEntryStyle: 'maker',
      crypto15mExitThreshold: 0.4,
      crypto15mMinDeltaPct: 0,
      crypto15mIndicatorDetect: true,
      crypto15mUseRules: true,
      crypto15mRules: [
        { field: 'upProb', op: '>=', value: 0.55 },
        { field: 'macdHist', op: '>', value: 0 },
        { field: 'minsLeft', op: '<=', value: 6 },
      ],
    },
  },
];

export const CRYPTO15M_BACKTEST_CHOICES: Crypto15mPreset[] = [
  {
    id: 'current',
    name: 'My current settings',
    hint: 'Replay the active 15m crypto settings without changing them.',
    patch: {},
  },
  ...CRYPTO15M_PRESETS,
];
