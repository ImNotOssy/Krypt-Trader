import { Fragment, useEffect, useRef, useState } from 'react';
import { Download, FileJson, FlaskConical, FolderPlus, Play } from 'lucide-react';
import {
  Area, AreaChart, Bar, BarChart, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import type {
  CollectionStats, Crypto15mBacktest, Crypto15mOptimizationResult,
  TraderConfig,
} from '@shared/types';
import { CRYPTO15M_BACKTEST_CHOICES as C15_STRATS } from '@shared/crypto15m-presets';
import { backtestInputKey, isStaleBacktestResult } from '@shared/backtest-state';
import { Card, Page, Switch } from '../components/common';
import { useApp } from '../state/AppStateProvider';
import { useToast } from '../state/ToastProvider';
import { cls, fmtUsd } from '../utils/format';

type Engine = 'crypto15m' | 'main' | 'perps';
type MainOptMode = 'whale' | 'momentum' | 'combined';

const WINDOWS = [7, 14, 30, 60];

export function BacktestPage() {
  const [engine, setEngine] = useState<Engine>('crypto15m');
  const [stratSel, setStratSel] = useState('current');
  const [mainSel, setMainSel] = useState('current');
  const [mainOptMode, setMainOptMode] = useState<MainOptMode>('whale');
  const [days, setDays] = useState(30);
  const [busy, setBusy] = useState(false);
  const [optBusy, setOptBusy] = useState(false);
  const [res, setRes] = useState<Crypto15mBacktest | null>(null);
  const [optRes, setOptRes] = useState<Crypto15mOptimizationResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [optErr, setOptErr] = useState<string | null>(null);
  const latestBacktestKeyRef = useRef('');
  const runSeqRef = useRef(0);
  const optSeqRef = useRef(0);
  const { config, state } = useApp();
  const profiles = state?.customProfiles ?? [];
  const [coll, setColl] = useState<CollectionStats | null>(null);
  const [showData, setShowData] = useState(false);
  const [expandedTrade, setExpandedTrade] = useState<string | null>(null);
  const rejectionEntries = res?.rejectionBreakdown?.length
    ? res.rejectionBreakdown.slice(0, 8)
    : res?.rejections
      ? Object.entries(res.rejections).slice(0, 8).map(([reason, count]) => ({
          reason,
          count,
          pct: (res.rejectionTotal ?? 0) > 0 ? count / (res.rejectionTotal ?? 1) : 0,
        }))
      : [];
  const hasSideSummary = !!res?.bySide && (res.bySide.YES.n > 0 || res.bySide.NO.n > 0);
  const hasEntryBuckets = !!res?.entryPriceBuckets?.some((b) => b.n > 0);
  const hasTimeBuckets = !!res?.timeLeftBuckets?.some((b) => b.n > 0);

  const loadCollection = async () => {
    try {
      setColl(await window.krypt.trading.collection());
    } catch { /* backend down */ }
  };
  useEffect(() => { void loadCollection(); }, []);

  const toggleC15Collection = async (on: boolean) => {
    await window.krypt.config.update({ crypto15mRecordSignals: on });
    void loadCollection();
  };

  const toggleMainCollection = async (on: boolean) => {
    await window.krypt.config.update({ mainRecordSignals: on });
    void loadCollection();
  };

  const togglePerpsCollection = async (on: boolean) => {
    await window.krypt.config.update({ perpsRecordSignals: on });
    void loadCollection();
  };

  const [exporting, setExporting] = useState(false);
  const exportData = async () => {
    setExporting(true);
    try {
      await window.krypt.trading.exportData();
    } finally {
      setExporting(false);
    }
  };

  const sliceProfile = (cfg: TraderConfig, kind: 'crypto15m' | 'main'): Partial<TraderConfig> => {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(cfg)) {
      const is15m = k.startsWith('crypto15m');
      if ((kind === 'crypto15m') === is15m) out[k] = v;
    }
    delete out.enableTrading;
    delete out.crypto15mLive;
    return out as Partial<TraderConfig>;
  };

  const resolvePatch = (): Partial<TraderConfig> => {
    if (engine === 'perps') return {}; // always the current Perpetuals-page strategy
    const sel = engine === 'crypto15m' ? stratSel : mainSel;
    if (sel.startsWith('profile:')) {
      const prof = profiles.find((pr) => pr.id === sel.slice(8));
      return prof ? sliceProfile(prof.config, engine) : {};
    }
    if (engine === 'crypto15m' && sel.startsWith('preset:')) {
      return C15_STRATS.find((st) => st.id === sel.slice(7))?.patch ?? {};
    }
    return {}; // current settings
  };

  const resolveBaseProfileMeta = (): { baseProfileId: string; baseProfileName: string } => {
    if (engine !== 'crypto15m') return { baseProfileId: '', baseProfileName: '' };
    if (stratSel.startsWith('profile:')) {
      const prof = profiles.find((pr) => pr.id === stratSel.slice(8));
      return {
        baseProfileId: prof?.id ?? '',
        baseProfileName: prof?.name ?? 'Unknown profile',
      };
    }
    if (stratSel.startsWith('preset:')) {
      const preset = C15_STRATS.find((st) => st.id === stratSel.slice(7));
      return {
        baseProfileId: `preset:${preset?.id ?? stratSel.slice(7)}`,
        baseProfileName: preset?.name ?? 'Preset',
      };
    }
    return { baseProfileId: 'current-settings', baseProfileName: 'Current settings' };
  };

  const perpsConfigSlice = (cfg: TraderConfig | null): Partial<TraderConfig> | null => {
    if (!cfg) return null;
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(cfg)) {
      if (k.startsWith('perps')) out[k] = v;
    }
    return out as Partial<TraderConfig>;
  };

  const currentConfigForKey = (): Partial<TraderConfig> | null => {
    if (!config) return null;
    if (engine === 'crypto15m' && stratSel === 'current') return sliceProfile(config, 'crypto15m');
    if (engine === 'main' && mainSel === 'current') return sliceProfile(config, 'main');
    if (engine === 'perps') return perpsConfigSlice(config);
    return null;
  };

  const selectedBacktestValue =
    engine === 'crypto15m' ? stratSel : engine === 'main' ? mainSel : 'current';
  const activeBacktestKey = backtestInputKey({
    engine,
    selection: selectedBacktestValue,
    days,
    patch: resolvePatch(),
    currentConfig: currentConfigForKey(),
    optimizerMode: engine === 'main' ? mainOptMode : null,
  });

  useEffect(() => {
    latestBacktestKeyRef.current = activeBacktestKey;
    runSeqRef.current += 1;
    optSeqRef.current += 1;
    setBusy(false);
    setOptBusy(false);
    setRes(null);
    setOptRes(null);
    setErr(null);
    setOptErr(null);
    setExpandedTrade(null);
  }, [activeBacktestKey]);

  const run = async () => {
    const runKey = activeBacktestKey;
    const seq = ++runSeqRef.current;
    setBusy(true);
    setErr(null);
    setRes(null);
    setExpandedTrade(null);
    try {
      const patch = resolvePatch();
      const r = engine === 'crypto15m'
        ? await window.krypt.crypto15m.backtest({ sinceDays: days, config: patch })
        : engine === 'perps'
          ? await window.krypt.perps.backtest({ sinceDays: days, config: patch })
          : await window.krypt.crypto15m.backtestMain({ sinceDays: days, config: patch });
      if (seq !== runSeqRef.current || isStaleBacktestResult(runKey, latestBacktestKeyRef.current)) return;
      setRes(r);
      setExpandedTrade(null);
      if (!r) setErr('Engine not running — start the app backend first.');
    } catch (e: any) {
      if (seq !== runSeqRef.current || isStaleBacktestResult(runKey, latestBacktestKeyRef.current)) return;
      setErr(e?.message || String(e));
    } finally {
      if (seq === runSeqRef.current) setBusy(false);
    }
  };

  const runOptimization = async () => {
    if (engine === 'perps') return;
    const runKey = activeBacktestKey;
    const seq = ++optSeqRef.current;
    setOptBusy(true);
    setOptErr(null);
    setOptRes(null);
    try {
      const patch = resolvePatch();
      const r = engine === 'main'
        ? await window.krypt.crypto15m.optimizeMain({
            sinceDays: days,
            config: patch,
            sourceMode: mainOptMode,
            fixedRiskUsd: 1,
            minTrades: 10,
            topN: 12,
            bootstrapSamples: 120,
          })
        : await window.krypt.crypto15m.optimize({
            sinceDays: days,
            config: patch,
            ...resolveBaseProfileMeta(),
            minTrades: 10,
            topN: 12,
            bootstrapSamples: 120,
          });
      if (seq !== optSeqRef.current || isStaleBacktestResult(runKey, latestBacktestKeyRef.current)) return;
      setOptRes(r);
      if (!r) setOptErr('Engine not running — start the app backend first.');
    } catch (e: any) {
      if (seq !== optSeqRef.current || isStaleBacktestResult(runKey, latestBacktestKeyRef.current)) return;
      setOptErr(e?.message || String(e));
    } finally {
      if (seq === optSeqRef.current) setOptBusy(false);
    }
  };

  const downloadTrades = () => {
    if (!res?.trades?.length) return;
    downloadCsv(`krypt-${engine}-backtest-trades.csv`, tradesToCsv(res.trades));
  };

  return (
    <Page
      title="Backtest"
      subtitle="While the bot runs it records every 15m market tick and every whale/momentum signal it sees, with outcomes. Test any strategy against that collected history — same entry gates as live, real Kalshi fees — and see WHEN the profit happens, not just how much."
    >
      <Card header={<div className="text-xs uppercase tracking-wider text-krypt-muted">Setup</div>}>
        <div className="flex flex-wrap items-end gap-4">
          <Field label="Engine">
            <Chips
              options={[['crypto15m', '15m Crypto'], ['main', 'Main engine (whales + momentum)'], ['perps', 'Perpetuals']]}
              value={engine}
              onPick={(v) => setEngine(v as Engine)}
            />
          </Field>
          {engine === 'crypto15m' && (
            <Field label="Strategy / profile">
              <select
                value={stratSel}
                onChange={(e) => setStratSel(e.target.value)}
                className="rounded-md border border-krypt-border bg-krypt-surface2 px-2.5 py-1.5 text-xs text-white outline-none focus:border-krypt-purple/60"
              >
                <option value="current">Current settings</option>
                <optgroup label="Presets">
                  {C15_STRATS.filter((st) => st.id !== 'current').map((st) => (
                    <option key={st.id} value={`preset:${st.id}`}>{st.name}</option>
                  ))}
                </optgroup>
                {profiles.some((pr) => pr.kind === 'crypto15m') && (
                  <optgroup label="My profiles">
                    {profiles.filter((pr) => pr.kind === 'crypto15m').map((pr) => (
                      <option key={pr.id} value={`profile:${pr.id}`}>{pr.name}</option>
                    ))}
                  </optgroup>
                )}
              </select>
            </Field>
          )}
          {engine === 'perps' && (
            <Field label="Strategy">
              <div className="rounded-md border border-krypt-border bg-krypt-surface2 px-2.5 py-1.5 text-xs text-krypt-dim">
                Your Perpetuals-page strategy (rules, TP/SL, leverage, fee era) — edit it there, test it here.
              </div>
            </Field>
          )}
          {engine === 'main' && (
            <Field label="Strategy / profile">
              <select
                value={mainSel}
                onChange={(e) => setMainSel(e.target.value)}
                className="rounded-md border border-krypt-border bg-krypt-surface2 px-2.5 py-1.5 text-xs text-white outline-none focus:border-krypt-purple/60"
              >
                <option value="current">Current settings (Strategies-page gates)</option>
                {profiles.some((pr) => pr.kind === 'main') && (
                  <optgroup label="My profiles">
                    {profiles.filter((pr) => pr.kind === 'main').map((pr) => (
                      <option key={pr.id} value={`profile:${pr.id}`}>{pr.name}</option>
                    ))}
                  </optgroup>
                )}
              </select>
            </Field>
          )}
          {engine === 'main' && (
            <Field label="Optimizer">
              <Chips
                options={[['whale', 'Whales'], ['momentum', 'Momentum'], ['combined', 'Combined']]}
                value={mainOptMode}
                onPick={(v) => setMainOptMode(v as MainOptMode)}
              />
            </Field>
          )}
          <Field label="Window">
            <Chips
              options={WINDOWS.map((d) => [String(d), `${d}d`] as [string, string])}
              value={String(days)}
              onPick={(v) => setDays(Number(v))}
            />
          </Field>
          <button
            onClick={() => void run()}
            disabled={busy}
            className="inline-flex items-center gap-2 rounded-md border border-krypt-purple/40 bg-krypt-purple/10 px-4 py-2 text-xs font-semibold text-krypt-purple transition-colors hover:bg-krypt-purple/20 disabled:opacity-50"
          >
            {busy ? <FlaskConical className="h-3.5 w-3.5 animate-pulse" /> : <Play className="h-3.5 w-3.5" />}
            {busy ? 'Replaying…' : 'Run backtest'}
          </button>
          {engine !== 'perps' && (
            <button
              onClick={() => void runOptimization()}
              disabled={optBusy}
              className="inline-flex items-center gap-2 rounded-md border border-krypt-border bg-krypt-surface2 px-4 py-2 text-xs font-semibold text-krypt-dim transition-colors hover:text-white disabled:opacity-50"
            >
              <FlaskConical className={cls('h-3.5 w-3.5', optBusy && 'animate-pulse')} />
              {optBusy ? 'Optimizing…' : engine === 'main' ? 'Run main optimization' : 'Run optimization'}
            </button>
          )}
        </div>
        {err && <p className="mt-2 text-xs text-krypt-loss">{err}</p>}
        {optErr && <p className="mt-2 text-xs text-krypt-loss">{optErr}</p>}
      </Card>

      <div className="mt-4">
        <Card header={<div className="text-xs uppercase tracking-wider text-krypt-muted">Data collection</div>}>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="rounded-lg bg-krypt-surface2/50 p-3">
              <Switch
                checked={config?.crypto15mRecordSignals ?? true}
                onChange={(v) => void toggleC15Collection(v)}
                label="Collect 15m crypto data"
                description="Records every 15-minute market's ticks and outcome while the app is open — no trading, no orders, works with the 15m executor fully off. This is the dataset 15m backtests run on."
              />
              <p className="mt-1.5 text-[11px] text-krypt-dim">
                {coll ? `${coll.c15.windows.toLocaleString()} windows · ${coll.c15.ticks.toLocaleString()} ticks` : '…'}
                {coll?.c15.lastAt ? ` · last ${coll.c15.lastAt.slice(5, 16)} UTC` : ''}
              </p>
            </div>
            <div className="rounded-lg bg-krypt-surface2/50 p-3">
              <Switch
                checked={config?.mainRecordSignals ?? true}
                onChange={(v) => void toggleMainCollection(v)}
                label="Collect whale + momentum signals"
                description="Records whale prints and momentum clusters while the app is open — nothing is bought. Note: if Start Trading is ON, scanning stays on regardless (the engine can't follow signals it never sees)."
              />
              <p className="mt-1.5 text-[11px] text-krypt-dim">
                {coll ? `${coll.main.whales.toLocaleString()} whale signals · ${coll.main.alerts.toLocaleString()} momentum` : '…'}
                {coll?.main.lastAt ? ` · last ${coll.main.lastAt.slice(5, 16)} UTC` : ''}
              </p>
            </div>
            <div className="rounded-lg bg-krypt-surface2/50 p-3">
              <Switch
                checked={config?.perpsRecordSignals ?? true}
                onChange={(v) => void togglePerpsCollection(v)}
                label="Collect perpetuals data"
                description="Records 1-second perp quotes, the trade tape, 1m candles and funding rates while the app is open — no orders, no margin. This is the dataset the upcoming perps strategies will be backtested on (see the Perpetuals page)."
              />
              <p className="mt-1.5 text-[11px] text-krypt-dim">
                {coll?.perps ? `${coll.perps.ticks.toLocaleString()} quote ticks · ${coll.perps.trades.toLocaleString()} trades · ${coll.perps.candles.toLocaleString()} candles · ${coll.perps.funding.toLocaleString()} funding` : '…'}
                {coll?.perps?.lastAt ? ` · last ${coll.perps.lastAt.slice(5, 16)} UTC` : ''}
              </p>
            </div>
            <div className="rounded-lg bg-krypt-surface2/50 p-3">
              <div className="text-sm text-white">Coinbase spot candles</div>
              <p className="mt-0.5 text-xs text-krypt-muted">
                Direct-imported candles used for indicator warm-up and spot context.
              </p>
              <p className="mt-1.5 text-[11px] text-krypt-dim">
                {coll?.coinbase ? `${coll.coinbase.candles.toLocaleString()} candles` : '…'}
                {coll?.coinbase?.lastAt ? ` · last ${coll.coinbase.lastAt.slice(5, 16)} UTC` : ''}
              </p>
            </div>
          </div>
          <button
            onClick={() => void exportData()}
            disabled={exporting}
            className="mr-2 mt-3 rounded-md border border-krypt-border bg-krypt-surface2 px-3 py-1.5 text-xs text-krypt-dim transition-colors hover:text-white disabled:opacity-50"
          >
            {exporting ? 'Exporting…' : 'Export CSV (all collected data)'}
          </button>
          <button
            onClick={() => setShowData((v) => !v)}
            className="mt-3 rounded-md border border-krypt-border bg-krypt-surface2 px-3 py-1.5 text-xs text-krypt-dim transition-colors hover:text-white"
          >
            {showData ? 'Hide collected data' : 'View collected data'}
          </button>
          {showData && coll && (
            <div className="mt-3 grid gap-4 lg:grid-cols-3">
              <div>
                <div className="mb-1 text-[10px] uppercase tracking-wide text-krypt-dim">Latest 15m windows</div>
                <table className="w-full text-[11px]">
                  <thead><tr className="text-left text-krypt-dim">
                    <th className="py-0.5 pr-2 font-normal">Asset</th>
                    <th className="py-0.5 pr-2 font-normal">Closes</th>
                    <th className="py-0.5 pr-2 font-normal">Favorite</th>
                    <th className="py-0.5 font-normal">Result</th>
                  </tr></thead>
                  <tbody>
                    {coll.c15.recent.map((r) => (
                      <tr key={r.ticker} className="border-t border-krypt-border/50">
                        <td className="py-1 pr-2 font-mono text-white">{r.asset}</td>
                        <td className="py-1 pr-2 text-krypt-dim">{r.close_time?.slice(5, 16)}</td>
                        <td className="py-1 pr-2 text-krypt-dim">
                          {r.favorite ?? '—'}{r.favorite_price != null ? ` @ ${Math.round(r.favorite_price * 100)}¢` : ''}
                        </td>
                        <td className="py-1">
                          {!r.resolved ? <span className="text-krypt-dim">open</span>
                            : r.up_won ? <span className="text-krypt-win">UP won</span>
                              : <span className="text-krypt-loss">DOWN won</span>}
                        </td>
                      </tr>
                    ))}
                    {coll.c15.recent.length === 0 && (
                      <tr><td colSpan={4} className="py-2 text-krypt-dim">Nothing yet — leave the app open with collection on.</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
              <div>
                <div className="mb-1 text-[10px] uppercase tracking-wide text-krypt-dim">Latest whale signals</div>
                <table className="w-full text-[11px]">
                  <thead><tr className="text-left text-krypt-dim">
                    <th className="py-0.5 pr-2 font-normal">Category</th>
                    <th className="py-0.5 pr-2 font-normal">Side</th>
                    <th className="py-0.5 pr-2 font-normal">Price</th>
                    <th className="py-0.5 pr-2 font-normal">Size</th>
                    <th className="py-0.5 font-normal">Result</th>
                  </tr></thead>
                  <tbody>
                    {coll.main.recent.map((r, i) => (
                      <tr key={`${r.ticker}-${i}`} className="border-t border-krypt-border/50">
                        <td className="py-1 pr-2 text-white">{r.category}</td>
                        <td className="py-1 pr-2 font-mono text-krypt-dim">{r.taker_side}</td>
                        <td className="py-1 pr-2 text-krypt-dim">{Math.round((r.price ?? 0) * 100)}¢</td>
                        <td className="py-1 pr-2 text-krypt-dim">${Math.round(r.dollar_value ?? 0).toLocaleString()}</td>
                        <td className="py-1">
                          {!r.resolved ? <span className="text-krypt-dim">open</span>
                            : r.outcome_correct ? <span className="text-krypt-win">won</span>
                              : <span className="text-krypt-loss">lost</span>}
                        </td>
                      </tr>
                    ))}
                    {coll.main.recent.length === 0 && (
                      <tr><td colSpan={5} className="py-2 text-krypt-dim">Nothing yet — signals record while the app is open.</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
              <div>
                <div className="mb-1 text-[10px] uppercase tracking-wide text-krypt-dim">Coinbase candles</div>
                <table className="w-full text-[11px]">
                  <thead><tr className="text-left text-krypt-dim">
                    <th className="py-0.5 pr-2 font-normal">Asset</th>
                    <th className="py-0.5 pr-2 font-normal">Frame</th>
                    <th className="py-0.5 pr-2 text-right font-normal">Candles</th>
                    <th className="py-0.5 font-normal">Latest</th>
                  </tr></thead>
                  <tbody>
                    {coll.coinbase.byAsset.map((r) => (
                      <tr key={`${r.asset}-${r.timeframe_sec}`} className="border-t border-krypt-border/50">
                        <td className="py-1 pr-2 font-mono text-white">{r.asset}</td>
                        <td className="py-1 pr-2 text-krypt-dim">{r.timeframe_sec >= 3600 ? `${r.timeframe_sec / 3600}h` : `${r.timeframe_sec / 60}m`}</td>
                        <td className="py-1 pr-2 text-right font-mono text-krypt-dim">{r.candles.toLocaleString()}</td>
                        <td className="py-1 text-krypt-dim">{r.lastAt?.slice(5, 16) ?? '—'}</td>
                      </tr>
                    ))}
                    {coll.coinbase.byAsset.length === 0 && (
                      <tr><td colSpan={4} className="py-2 text-krypt-dim">No Coinbase candles imported yet.</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </Card>
      </div>

      {res && res.windowsScanned === 0 && (
        <Card className="mt-4">
          <div className="py-4 text-center">
            <div className="text-sm font-semibold text-white">No collected data yet</div>
            <p className="mx-auto mt-1 max-w-md text-xs leading-relaxed text-krypt-dim">
              This backtest runs on history YOUR bot collects while it runs. Leave the app open
              (monitor mode is enough — no live trading needed) and it records every
              {engine === 'crypto15m'
                ? ' 15-minute market with its outcome. Make sure the 15m Crypto toggle is on and the environment is Live/production (demo 15m markets are frozen).'
                : engine === 'perps'
                  ? ' perpetual quote, trade and 1-minute candle. Turn on "Collect perpetuals data" on the Perpetuals page and leave the app open; candles start accruing immediately.'
                  : ' whale and momentum signal it sees, with outcomes. Data starts accruing immediately.'}
              {' '}Check back after a few hours; the charts get sharper every day it runs.
            </p>
          </div>
        </Card>
      )}
      {engine !== 'perps' && optRes && (
        <OptimizationResults result={optRes} />
      )}
      {res && res.windowsScanned > 0 && res.n === 0 && (
        <Card className="mt-4">
          <div className="text-xs leading-relaxed text-krypt-muted">
            <span className="font-semibold text-krypt-warn">
              0 trades over {res.windowsScanned.toLocaleString()} {engine === 'crypto15m' ? 'windows' : 'bars'} scanned.
            </span>{' '}
            {engine === 'perps'
              ? ((config?.perpsStratRules?.length ?? 0) === 0
                  ? 'Your perps strategy has no entry rules, so it never enters — that’s why the backtest is empty. Open the Perpetuals page, add at least one entry rule in the Strategy builder, then re-run here (the backtest replays those exact rules, TP/SL, leverage and fee era).'
                  : 'Your entry rules never matched anywhere in this window. Loosen them or widen the date range on the Perpetuals page, then re-run.')
              : 'No entries triggered in this window — loosen the entry gates or widen the date range.'}
          </div>
          {rejectionEntries.length > 0 && (
            <div className="mt-3 flex flex-wrap gap-1.5">
              {rejectionEntries.map(({ reason, count, pct }) => (
                <span key={reason} className="rounded bg-krypt-surface2 px-1.5 py-0.5 font-mono text-[10px] text-krypt-dim">
                  {reason}: <span className="text-white">{count.toLocaleString()}</span>
                  <span className="ml-1 text-krypt-muted">{(pct * 100).toFixed(1)}%</span>
                </span>
              ))}
            </div>
          )}
        </Card>
      )}
      {res && res.windowsScanned > 0 && (
        <>
          <div className="mt-4 grid grid-cols-2 gap-2 text-xs sm:grid-cols-6">
            <Stat label="Trades" value={`${res.n}`} sub={`${res.windowsScanned} ${engine === 'crypto15m' ? 'windows' : 'signals'} scanned`} />
            <Stat label="Win rate" value={res.n ? `${(res.winRate * 100).toFixed(1)}%` : '—'} />
            <Stat label="Edge / contract" value={`${res.netEvCentsPerContract.toFixed(2)}¢`} tone={res.netEvCentsPerContract >= 0 ? 'good' : 'bad'} />
            <Stat label="Total P&L" value={fmtUsd(res.totalPnlUsd, { sign: true })} tone={res.totalPnlUsd >= 0 ? 'good' : 'bad'} />
            <Stat label="Max drawdown" value={fmtUsd(res.maxDrawdownUsd)} tone="bad" />
            <Stat label="Days traded" value={`${res.byDay.length}`} />
          </div>
          {res.tradeStats && <TradeStatsGrid stats={res.tradeStats} />}
          {res.dataset && <DatasetManifestBar dataset={res.dataset} />}

          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            <Card header={<ChartHead title="Equity over time" hint="Cumulative P&L in trade order — a healthy strategy climbs steadily; one big lucky step means one event carried it." />}>
              <div className="h-48">
                <ResponsiveContainer>
                  <AreaChart data={res.equity.map((e, i) => ({ i, at: e.at ? e.at.slice(5, 16) : String(i), value: e.value }))}>
                    <XAxis dataKey="at" tick={{ fontSize: 9 }} minTickGap={40} stroke="#555" />
                    <YAxis tick={{ fontSize: 9 }} width={40} stroke="#555" />
                    <Tooltip contentStyle={{ background: '#111', border: '1px solid #333', fontSize: 11 }} formatter={(v: number) => fmtUsd(v, { sign: true })} />
                    <Area dataKey="value" stroke="#a78bfa" fill="#a78bfa22" strokeWidth={1.5} />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </Card>

            <Card header={<ChartHead title="P&L by hour of day (UTC)" hint="The 'first 12 hours print, last 12 lose' check — green bars earn, red bars bleed. Thin bars (low n) are noise, not signal." />}>
              <div className="h-48">
                <ResponsiveContainer>
                  <BarChart data={res.byHourUtc}>
                    <XAxis dataKey="hour" tick={{ fontSize: 9 }} stroke="#555" />
                    <YAxis tick={{ fontSize: 9 }} width={40} stroke="#555" />
                    <Tooltip
                      contentStyle={{ background: '#111', border: '1px solid #333', fontSize: 11 }}
                      formatter={(v: number, _n, item: any) => [`${fmtUsd(v, { sign: true })} (${item?.payload?.wins}/${item?.payload?.n})`, 'P&L']}
                    />
                    <Bar dataKey="pnlUsd">
                      {res.byHourUtc.map((b) => (
                        <Cell key={b.hour} fill={b.pnlUsd >= 0 ? '#34d399' : '#f87171'} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </Card>
          </div>

          <div className="mt-4">
            <Card header={<ChartHead title="P&L by day" hint="The 'prints one day, sucks the next' check — consistency across days matters more than the total." />}>
              <div className="h-40">
                <ResponsiveContainer>
                  <BarChart data={res.byDay.map((d) => ({ ...d, label: d.day.slice(5) }))}>
                    <XAxis dataKey="label" tick={{ fontSize: 9 }} stroke="#555" />
                    <YAxis tick={{ fontSize: 9 }} width={40} stroke="#555" />
                    <Tooltip
                      contentStyle={{ background: '#111', border: '1px solid #333', fontSize: 11 }}
                      formatter={(v: number, _n, item: any) => [`${fmtUsd(v, { sign: true })} (${item?.payload?.wins}/${item?.payload?.n})`, 'P&L']}
                    />
                    <Bar dataKey="pnlUsd">
                      {res.byDay.map((d) => (
                        <Cell key={d.day} fill={d.pnlUsd >= 0 ? '#34d399' : '#f87171'} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </Card>
          </div>

          {(hasSideSummary || hasEntryBuckets || hasTimeBuckets) && (
            <div className="mt-4 grid gap-4 lg:grid-cols-3">
              {hasSideSummary && res.bySide && (
                <Card header={<ChartHead title="Bullish vs bearish" hint="Separate branch performance for YES and NO entries." />}>
                  <SideSummaryTable bySide={res.bySide} />
                </Card>
              )}
              {hasEntryBuckets && res.entryPriceBuckets && (
                <Card header={<ChartHead title="Entry price buckets" hint="Performance by actual simulated fill price." />}>
                  <BucketTable rows={res.entryPriceBuckets} />
                </Card>
              )}
              {hasTimeBuckets && res.timeLeftBuckets && (
                <Card header={<ChartHead title="Time-left buckets" hint="Performance by minutes remaining at entry." />}>
                  <BucketTable rows={res.timeLeftBuckets} />
                </Card>
              )}
            </div>
          )}

          {Object.keys(res.byAsset).length > 0 && (
            <div className="mt-3 flex flex-wrap gap-1.5">
              {Object.entries(res.byAsset).map(([a, st]) => (
                <span key={a} className="rounded bg-krypt-surface2 px-1.5 py-0.5 font-mono text-[10px] text-krypt-dim">
                  {a} {st.wins}/{st.n} <span className={st.pnlUsd >= 0 ? 'text-krypt-win' : 'text-krypt-loss'}>{fmtUsd(st.pnlUsd, { sign: true })}</span>
                </span>
              ))}
            </div>
          )}
          {res.n > 0 && rejectionEntries.length > 0 && (
            <div className="mt-3 flex flex-wrap gap-1.5">
              {rejectionEntries.map(({ reason, count, pct }) => (
                <span key={reason} className="rounded bg-krypt-surface2 px-1.5 py-0.5 font-mono text-[10px] text-krypt-dim">
                  {reason}: <span className="text-white">{count.toLocaleString()}</span>
                  <span className="ml-1 text-krypt-muted">{(pct * 100).toFixed(1)}%</span>
                </span>
              ))}
            </div>
          )}

          {res.trades.length > 0 && (
            <Card
              className="mt-4"
              header={(
                <div className="flex items-center justify-between gap-3">
                  <ChartHead title="Trade-level results" hint="One simulated row per entry, with pricing, indicators, fees and outcome." />
                  <button
                    onClick={downloadTrades}
                    className="inline-flex shrink-0 items-center gap-1 rounded-md border border-krypt-border bg-krypt-surface2 px-2.5 py-1 text-[11px] text-krypt-dim transition-colors hover:text-white"
                  >
                    <Download className="h-3.5 w-3.5" />
                    CSV
                  </button>
                </div>
              )}
            >
              <TradeTable
                trades={res.trades}
                expanded={expandedTrade}
                onToggle={(id) => setExpandedTrade((cur) => (cur === id ? null : id))}
              />
            </Card>
          )}

          <ul className="mt-3 space-y-0.5">
            {res.caveats.map((c, i) => (
              <li key={i} className="text-[10px] leading-relaxed text-krypt-warn/80">⚠ {c}</li>
            ))}
          </ul>
        </>
      )}
    </Page>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-1 text-[10px] uppercase tracking-wide text-krypt-dim">{label}</div>
      {children}
    </div>
  );
}

function Chips({ options, value, onPick }: {
  options: [string, string][]; value: string; onPick: (v: string) => void;
}) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {options.map(([v, label]) => (
        <button
          key={v}
          onClick={() => onPick(v)}
          className={cls(
            'rounded-md border px-2.5 py-1 text-xs transition-colors',
            v === value
              ? 'border-krypt-purple/60 bg-krypt-purple/15 text-krypt-purple'
              : 'border-krypt-border bg-krypt-surface2 text-krypt-dim hover:text-white',
          )}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: 'good' | 'bad' }) {
  return (
    <div className="rounded-lg bg-krypt-surface2/60 p-2">
      <div className="text-[10px] uppercase tracking-wide text-krypt-dim">{label}</div>
      <div className={cls('font-mono text-sm', tone === 'good' ? 'text-krypt-win' : tone === 'bad' ? 'text-krypt-loss' : 'text-white')}>{value}</div>
      {sub && <div className="text-[10px] text-krypt-dim">{sub}</div>}
    </div>
  );
}

function DatasetManifestBar({ dataset }: { dataset: NonNullable<Crypto15mBacktest['dataset']> }) {
  const range = dataset.firstTimestamp && dataset.lastTimestamp
    ? `${dataset.firstTimestamp.slice(0, 10)} to ${dataset.lastTimestamp.slice(0, 10)}`
    : 'empty';
  const rows = dataset.inSampleRowCount !== dataset.rowCount
    ? `${dataset.rowCount.toLocaleString()} rows / ${dataset.inSampleRowCount.toLocaleString()} in sample`
    : `${dataset.rowCount.toLocaleString()} rows`;
  return (
    <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md border border-krypt-border bg-krypt-surface2/40 px-2 py-1.5 text-[10px] text-krypt-dim">
      <span className="uppercase tracking-wide text-krypt-muted">Dataset</span>
      <code className="break-all text-white">{dataset.datasetId}</code>
      <span>{rows}</span>
      <span>{dataset.windows.toLocaleString()} windows</span>
      <span>{dataset.assets.join(', ') || 'no assets'}</span>
      <span>{range}</span>
      <span title={dataset.sha256}>sha {dataset.sha256.slice(0, 12)}</span>
    </div>
  );
}

function OptimizationResults({ result }: { result: Crypto15mOptimizationResult }) {
  const best = result.ranked[0];
  return (
    <Card
      className="mt-4"
      header={<ChartHead title="Optimization" hint="Grid search with train / validation / test splits, bootstrap robustness and walk-forward selection." />}
    >
      <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-6">
        <Stat label="Grid" value={`${result.gridSize}`} sub={`${result.minTrades} min trades`} />
        <Stat label="Eligible" value={`${result.ranked.filter((r) => r.eligible).length}`} />
        <Stat label="Best val edge" value={best ? `${best.splits.validation.netEvCentsPerContract.toFixed(2)}¢` : '—'} tone={(best?.splits.validation.netEvCentsPerContract ?? 0) >= 0 ? 'good' : 'bad'} />
        <Stat label="Best test edge" value={best ? `${best.splits.test.netEvCentsPerContract.toFixed(2)}¢` : '—'} tone={(best?.splits.test.netEvCentsPerContract ?? 0) >= 0 ? 'good' : 'bad'} />
        <Stat label="Bootstrap +" value={best ? `${(best.bootstrap.probabilityPositive * 100).toFixed(0)}%` : '—'} tone={(best?.bootstrap.probabilityPositive ?? 0) >= 0.6 ? 'good' : 'bad'} />
        <Stat label="Walk fwd" value={`${result.walkForward.summary.netEvCentsPerContract.toFixed(2)}¢`} tone={result.walkForward.summary.netEvCentsPerContract >= 0 ? 'good' : 'bad'} />
      </div>
      <div className="mt-4 grid gap-4 lg:grid-cols-[1.4fr,1fr]">
        <OptimizationTable rows={result.ranked} />
        <OptimizationHeatmap result={result} />
      </div>
      <div className="mt-4">
        <WalkForwardTable result={result} />
      </div>
      {result.research && (
        <ResearchLayer result={result} />
      )}
      <ul className="mt-3 space-y-0.5">
        {result.caveats.map((c) => (
          <li key={c} className="text-[10px] leading-relaxed text-krypt-warn/80">⚠ {c}</li>
        ))}
      </ul>
    </Card>
  );
}

function ResearchLayer({ result }: { result: Crypto15mOptimizationResult }) {
  const research = result.research!;
  const toast = useToast();
  const [exporting, setExporting] = useState(false);
  const comparison = research.winnerLoserComparisons[0];
  const exportExperiment = async () => {
    setExporting(true);
    try {
      const r = await window.krypt.crypto15m.exportExperiment({ optimization: result });
      if (r?.ok) toast.success(r.message || 'Exported AI experiment ZIP');
      else toast.error(r?.message || 'Could not export AI experiment ZIP');
    } finally {
      setExporting(false);
    }
  };
  return (
    <div className="mt-5 border-t border-krypt-border pt-4">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <ChartHead title="AI research" hint={research.summary} />
        <div className="flex flex-wrap items-center gap-2">
          <button
            onClick={() => void exportExperiment()}
            disabled={exporting}
            className="inline-flex items-center gap-1.5 rounded-md border border-krypt-border bg-krypt-surface px-2.5 py-1.5 text-[10px] font-semibold text-krypt-dim transition-colors hover:text-white disabled:opacity-60"
          >
            <Download className="h-3 w-3" />
            {exporting ? 'Exporting ZIP...' : 'Export ZIP'}
          </button>
          <div className="rounded-md border border-krypt-warn/30 bg-krypt-warn/10 px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-krypt-warn">
            {research.status === 'ok' ? 'Human approval required' : 'Rejected'}
          </div>
        </div>
      </div>
      {research.status !== 'ok' && (
        <div className="mb-4 rounded-md border border-krypt-loss/30 bg-krypt-loss/10 px-3 py-2 text-xs text-krypt-loss">
          {research.status}
        </div>
      )}
      <div className="grid gap-4 xl:grid-cols-[1.15fr,0.85fr]">
        <FeatureAttributionTable rows={research.featureAttribution} />
        <div className="space-y-4">
          {comparison && <WinnerLoserComparison comparison={comparison} />}
          <SuggestedExperiments experiments={research.suggestedExperiments} />
        </div>
      </div>
      <CandidateProfiles profiles={research.candidateProfiles} />
      <ul className="mt-3 space-y-0.5">
        {research.caveats.map((c) => (
          <li key={c} className="text-[10px] leading-relaxed text-krypt-warn/80">{c}</li>
        ))}
      </ul>
    </div>
  );
}

function FeatureAttributionTable({ rows }: { rows: NonNullable<Crypto15mOptimizationResult['research']>['featureAttribution'] }) {
  return (
    <div className="overflow-x-auto">
      <div className="mb-1 text-xs uppercase tracking-wider text-krypt-muted">Feature attribution</div>
      <table className="min-w-[680px] w-full text-[10px]">
        <thead>
          <tr className="text-left text-krypt-dim">
            <th className="py-1 pr-2 font-normal">Feature</th>
            <th className="py-1 pr-2 font-normal">Best</th>
            <th className="py-1 pr-2 font-normal">Worst</th>
            <th className="py-1 pr-2 text-right font-normal">Lift</th>
            <th className="py-1 pr-2 text-right font-normal">Best test</th>
            <th className="py-1 font-normal">Takeaway</th>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 8).map((r) => (
            <tr key={r.feature} className="border-t border-krypt-border/50">
              <td className="py-1 pr-2 text-white">{r.label}</td>
              <td className="py-1 pr-2 font-mono text-krypt-dim">{fmtParamValue(r.bestValue)}</td>
              <td className="py-1 pr-2 font-mono text-krypt-dim">{fmtParamValue(r.worstValue)}</td>
              <td className={cls('py-1 pr-2 text-right font-mono', r.liftCents >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}>
                {r.liftCents.toFixed(2)}¢
              </td>
              <td className={cls('py-1 pr-2 text-right font-mono', r.bestTestEdgeCents >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}>
                {r.bestTestEdgeCents.toFixed(2)}¢
              </td>
              <td className="py-1 text-krypt-dim">{r.takeaway}</td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr><td colSpan={6} className="py-2 text-krypt-dim">No parameter had enough variation to attribute.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function WinnerLoserComparison({ comparison }: { comparison: NonNullable<Crypto15mOptimizationResult['research']>['winnerLoserComparisons'][number] }) {
  return (
    <div>
      <div className="mb-1 text-xs uppercase tracking-wider text-krypt-muted">Winner / loser</div>
      <div className="rounded-md border border-krypt-border bg-krypt-surface2/40 p-3 text-xs">
        <div className="flex flex-wrap items-center gap-2 font-mono">
          <span className="text-krypt-win">{comparison.winnerId}</span>
          <span className="text-krypt-dim">vs</span>
          <span className="text-krypt-loss">{comparison.loserId}</span>
        </div>
        <p className="mt-1 text-[11px] leading-relaxed text-krypt-dim">{comparison.explanation}</p>
        <div className="mt-2 grid grid-cols-2 gap-2 text-[10px]">
          <Stat label="Val delta" value={`${comparison.metricDeltas.validationEdgeCents.toFixed(2)}¢`} tone={comparison.metricDeltas.validationEdgeCents >= 0 ? 'good' : 'bad'} />
          <Stat label="Test delta" value={`${comparison.metricDeltas.testEdgeCents.toFixed(2)}¢`} tone={comparison.metricDeltas.testEdgeCents >= 0 ? 'good' : 'bad'} />
        </div>
        <div className="mt-2 flex flex-wrap gap-1 font-mono text-[10px] text-krypt-dim">
          {Object.entries(comparison.parameterDeltas).map(([k, v]) => (
            <span key={k} className="rounded border border-krypt-border px-1.5 py-0.5">
              {fmtParamName(k)}: {v.change}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}

function SuggestedExperiments({ experiments }: { experiments: NonNullable<Crypto15mOptimizationResult['research']>['suggestedExperiments'] }) {
  return (
    <div>
      <div className="mb-1 text-xs uppercase tracking-wider text-krypt-muted">Suggested experiments</div>
      <div className="space-y-2">
        {experiments.slice(0, 3).map((exp) => (
          <div key={exp.id} className="rounded-md border border-krypt-border bg-krypt-surface2/40 p-3">
            <div className="flex flex-wrap items-center gap-2 text-xs text-white">
              <span>{exp.title}</span>
              <span className="rounded border border-krypt-border px-1.5 py-0.5 text-[9px] uppercase tracking-wide text-krypt-muted">{exp.priority}</span>
            </div>
            <p className="mt-1 text-[11px] leading-relaxed text-krypt-dim">{exp.rationale}</p>
            <div className="mt-2 flex flex-wrap gap-1 font-mono text-[10px] text-krypt-dim">
              {Object.entries(exp.paramGrid).map(([k, vals]) => (
                <span key={k} className="rounded border border-krypt-border px-1.5 py-0.5">
                  {fmtParamName(k)}=[{vals.map(fmtParamValue).join(', ')}]
                </span>
              ))}
            </div>
            <div className="mt-2 text-[10px] text-krypt-muted">
              min trades {exp.minTrades} · bootstrap {exp.bootstrapSamples}
            </div>
          </div>
        ))}
        {experiments.length === 0 && <div className="text-xs text-krypt-dim">No follow-up experiments generated.</div>}
      </div>
    </div>
  );
}

function CandidateProfiles({ profiles }: { profiles: NonNullable<Crypto15mOptimizationResult['research']>['candidateProfiles'] }) {
  const toast = useToast();
  type ResearchProfile = NonNullable<Crypto15mOptimizationResult['research']>['candidateProfiles'][number];
  const saveGeneratedProfile = async (profile: ResearchProfile) => {
    const invalid = validateGeneratedResearchProfile(profile);
    if (invalid) {
      toast.error(invalid);
      return;
    }
    const r = await window.krypt.profiles.import(JSON.stringify(profile.exportProfile));
    if (r.ok) toast.success(r.message || `Saved "${profile.name}"`);
    else toast.error(r.message || 'Could not save generated profile');
  };
  const exportGeneratedProfile = (profile: ResearchProfile) => {
    const invalid = validateGeneratedResearchProfile(profile);
    if (invalid) {
      toast.error(invalid);
      return;
    }
    downloadJson(`${safeFilename(profile.name)}.kryptprofile.json`, profile.exportProfile);
    toast.success(`Exported "${profile.name}"`);
  };
  return (
    <div className="mt-4">
      <div className="mb-1 text-xs uppercase tracking-wider text-krypt-muted">Generated candidate profiles</div>
      <div className="grid gap-2 lg:grid-cols-3">
        {profiles.map((profile) => (
          <div key={profile.id} className="rounded-md border border-krypt-border bg-krypt-surface2/40 p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="text-xs font-semibold text-white">{profile.name}</div>
              <span className="rounded border border-krypt-warn/30 px-1.5 py-0.5 text-[9px] uppercase tracking-wide text-krypt-warn">
                {profile.approval.status}
              </span>
            </div>
            <div className="mt-1 font-mono text-[10px] text-krypt-muted">{profile.sourceCandidateId}</div>
            <p className="mt-2 text-[11px] leading-relaxed text-krypt-dim">{profile.description}</p>
            <div className="mt-2 grid grid-cols-2 gap-2 text-[10px]">
              <Stat label="Validation" value={`${profile.metrics.validationEdgeCents.toFixed(2)}¢`} tone={profile.metrics.validationEdgeCents >= 0 ? 'good' : 'bad'} />
              <Stat label="Test" value={`${profile.metrics.testEdgeCents.toFixed(2)}¢`} tone={profile.metrics.testEdgeCents >= 0 ? 'good' : 'bad'} />
            </div>
            <div className="mt-2 flex flex-wrap gap-1 font-mono text-[10px] text-krypt-dim">
              {profile.changedConfig.map((change) => (
                <span key={change.key} className="rounded border border-krypt-border px-1.5 py-0.5">
                  {change.label}: {fmtParamValue(change.from)} → {fmtParamValue(change.to)}
                </span>
              ))}
            </div>
            {profile.preservedSummary.length > 0 && (
              <div className="mt-2">
                <div className="text-[9px] uppercase tracking-wide text-krypt-muted">Preserved</div>
                <div className="mt-1 flex flex-wrap gap-1 text-[10px] text-krypt-dim">
                  {profile.preservedSummary.map((item) => (
                    <span key={item} className="rounded border border-krypt-border px-1.5 py-0.5">{item}</span>
                  ))}
                </div>
              </div>
            )}
            <div className="mt-2 text-[10px] leading-relaxed text-krypt-warn/80">
              {profile.deployment.liveAllowed ? 'Live deployment allowed' : 'Live deployment blocked'} · {profile.approval.reason}
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              <button
                onClick={() => void saveGeneratedProfile(profile)}
                className="inline-flex items-center gap-1.5 rounded-md border border-krypt-purple/40 bg-krypt-purple/10 px-2.5 py-1.5 text-[10px] font-semibold text-krypt-purple transition-colors hover:bg-krypt-purple/20"
              >
                <FolderPlus className="h-3 w-3" />
                Save profile
              </button>
              <button
                onClick={() => exportGeneratedProfile(profile)}
                className="inline-flex items-center gap-1.5 rounded-md border border-krypt-border bg-krypt-surface px-2.5 py-1.5 text-[10px] font-semibold text-krypt-dim transition-colors hover:text-white"
              >
                <Download className="h-3 w-3" />
                Export JSON
              </button>
            </div>
          </div>
        ))}
        {profiles.length === 0 && <div className="text-xs text-krypt-dim">No candidate profiles generated.</div>}
      </div>
    </div>
  );
}

function OptimizationTable({ rows }: { rows: Crypto15mOptimizationResult['ranked'] }) {
  return (
    <div className="overflow-x-auto">
      <table className="min-w-[760px] w-full text-[10px]">
        <thead>
          <tr className="text-left text-krypt-dim">
            <th className="py-1 pr-2 font-normal">Rank</th>
            <th className="py-1 pr-2 font-normal">Params</th>
            <th className="py-1 pr-2 text-right font-normal">Train</th>
            <th className="py-1 pr-2 text-right font-normal">Validation</th>
            <th className="py-1 pr-2 text-right font-normal">Test</th>
            <th className="py-1 pr-2 text-right font-normal">Boot CI</th>
            <th className="py-1 text-right font-normal">State</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r.candidateId} className="border-t border-krypt-border/50">
              <td className="py-1 pr-2 font-mono text-white">#{i + 1}</td>
              <td className="py-1 pr-2 font-mono text-krypt-dim">
                {Object.entries(r.params).map(([k, v]) => (
                  <span key={k} className="mr-2 whitespace-nowrap">
                    {fmtParamName(k)}={fmtParamValue(v)}
                  </span>
                ))}
              </td>
              <td className={cls('py-1 pr-2 text-right font-mono', r.splits.train.netEvCentsPerContract >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}>
                {r.splits.train.netEvCentsPerContract.toFixed(2)}¢ <span className="text-krypt-dim">n={r.splits.train.n}</span>
              </td>
              <td className={cls('py-1 pr-2 text-right font-mono', r.splits.validation.netEvCentsPerContract >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}>
                {r.splits.validation.netEvCentsPerContract.toFixed(2)}¢ <span className="text-krypt-dim">n={r.splits.validation.n}</span>
              </td>
              <td className={cls('py-1 pr-2 text-right font-mono', r.splits.test.netEvCentsPerContract >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}>
                {r.splits.test.netEvCentsPerContract.toFixed(2)}¢ <span className="text-krypt-dim">n={r.splits.test.n}</span>
              </td>
              <td className="py-1 pr-2 text-right font-mono text-krypt-dim">
                {r.bootstrap.ciLow.toFixed(2)} to {r.bootstrap.ciHigh.toFixed(2)}¢
              </td>
              <td className="py-1 text-right">
                {r.eligible ? <span className="text-krypt-win">eligible</span> : <span className="text-krypt-warn">low n</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function OptimizationHeatmap({ result }: { result: Crypto15mOptimizationResult }) {
  const cells = result.heatmap.cells;
  const scores = cells.map((c) => c.score);
  const lo = scores.length ? Math.min(...scores) : 0;
  const hi = scores.length ? Math.max(...scores) : 0;
  return (
    <div>
      <div className="mb-2 text-xs uppercase tracking-wider text-krypt-muted">Sensitivity heatmap</div>
      <div className="grid grid-cols-2 gap-1 sm:grid-cols-3">
        {cells.map((c) => {
          const pct = hi === lo ? 0.5 : (c.score - lo) / (hi - lo);
          const bg = c.score >= 0
            ? `rgba(52, 211, 153, ${0.12 + pct * 0.34})`
            : `rgba(248, 113, 113, ${0.16 + (1 - pct) * 0.28})`;
          return (
            <div
              key={`${String(c.x)}-${String(c.y)}`}
              className="rounded-md border border-krypt-border p-2"
              style={{ background: bg }}
            >
              <div className="text-[10px] text-krypt-dim">
                {fmtParamName(result.heatmap.xParam)}={fmtParamValue(c.x)}
              </div>
              <div className="text-[10px] text-krypt-dim">
                {fmtParamName(result.heatmap.yParam)}={fmtParamValue(c.y)}
              </div>
              <div className={cls('mt-1 font-mono text-sm', c.score >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}>
                {c.score.toFixed(2)}¢
              </div>
              <div className="font-mono text-[10px] text-krypt-muted">n={c.n}</div>
            </div>
          );
        })}
        {cells.length === 0 && <div className="text-xs text-krypt-dim">No heatmap cells.</div>}
      </div>
    </div>
  );
}

function WalkForwardTable({ result }: { result: Crypto15mOptimizationResult }) {
  return (
    <div className="overflow-x-auto">
      <div className="mb-1 text-xs uppercase tracking-wider text-krypt-muted">Walk-forward folds</div>
      <table className="w-full text-[10px]">
        <thead>
          <tr className="text-left text-krypt-dim">
            <th className="py-1 pr-2 font-normal">Fold</th>
            <th className="py-1 pr-2 font-normal">Selected</th>
            <th className="py-1 pr-2 text-right font-normal">Train edge</th>
            <th className="py-1 pr-2 text-right font-normal">Out-of-sample edge</th>
            <th className="py-1 text-right font-normal">P&L</th>
          </tr>
        </thead>
        <tbody>
          {result.walkForward.folds.map((f) => (
            <tr key={f.fold} className="border-t border-krypt-border/50">
              <td className="py-1 pr-2 font-mono text-white">{f.fold}</td>
              <td className="py-1 pr-2 font-mono text-krypt-dim">{f.selectedCandidateId ?? '—'}</td>
              <td className="py-1 pr-2 text-right font-mono text-krypt-dim">
                {f.train ? `${f.train.netEvCentsPerContract.toFixed(2)}¢ n=${f.train.n}` : '—'}
              </td>
              <td className={cls('py-1 pr-2 text-right font-mono', f.metrics.netEvCentsPerContract >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}>
                {f.metrics.netEvCentsPerContract.toFixed(2)}¢ <span className="text-krypt-dim">n={f.metrics.n}</span>
              </td>
              <td className={cls('py-1 text-right font-mono', f.metrics.pnlUsd >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}>
                {fmtUsd(f.metrics.pnlUsd, { sign: true })}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function fmtParamName(k: string): string {
  return k
    .replace(/^crypto15m_/, '')
    .replace(/_/g, ' ');
}

function fmtProfileKey(k: string): string {
  return k
    .replace(/^crypto15m/, '15m')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .toLowerCase();
}

function fmtParamValue(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(fmtParamValue).join(', ')}]`;
  if (v === null || v === undefined) return '—';
  return typeof v === 'number' ? String(Number.isInteger(v) ? v : Number(v.toFixed(4))) : String(v);
}

function validateGeneratedResearchProfile(profile: NonNullable<Crypto15mOptimizationResult['research']>['candidateProfiles'][number]): string | null {
  const exportProfile = profile.exportProfile;
  const config = exportProfile.profile.config;
  if (exportProfile.profile.configMode !== 'full') {
    return 'Generated profile is a patch, not a full importable profile.';
  }
  if (config.enableTrading !== false) {
    return 'Generated profile must keep live trading disabled.';
  }
  if (profile.kind === 'crypto15m') {
    if (!config.crypto15mStrategyMode) {
      return 'Generated profile is missing strategy mode.';
    }
    if (!Array.isArray(config.crypto15mAssets)) {
      return 'Generated profile is missing asset restrictions.';
    }
    if (config.crypto15mLive !== false) {
      return 'Generated profile must keep crypto 15m live trading disabled.';
    }
    if (
      config.crypto15mStrategyMode === 'btc_ma_crossover'
      && (!config.crypto15mFastEmaPeriod || !config.crypto15mSlowSmaPeriod || !config.crypto15mTrendSmaPeriod || !config.crypto15mTrendTimeframeMin)
    ) {
      return 'BTC MA profile is missing moving-average settings.';
    }
  }
  if (profile.kind === 'main' && typeof config.tradeWhales !== 'boolean' && typeof config.tradeMomentum !== 'boolean') {
    return 'Generated main profile is missing source gates.';
  }
  return null;
}

function ChartHead({ title, hint }: { title: string; hint: string }) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wider text-krypt-muted">{title}</div>
      <div className="mt-0.5 text-[10px] normal-case tracking-normal text-krypt-dim">{hint}</div>
    </div>
  );
}

function TradeStatsGrid({ stats }: { stats: NonNullable<Crypto15mBacktest['tradeStats']> }) {
  return (
    <div className="mt-2 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4 lg:grid-cols-8">
      <Stat label="Win streak" value={`${stats.longestWinningStreak}`} />
      <Stat label="Loss streak" value={`${stats.longestLosingStreak}`} tone={stats.longestLosingStreak > 2 ? 'bad' : undefined} />
      <Stat label="Avg win" value={fmtUsd(stats.averageWinUsd, { sign: true })} tone="good" />
      <Stat label="Avg loss" value={fmtUsd(stats.averageLossUsd, { sign: true })} tone="bad" />
      <Stat label="Profit factor" value={fmtRatio(stats.profitFactor)} tone={(stats.profitFactor ?? 0) >= 1 ? 'good' : 'bad'} />
      <Stat label="Median" value={fmtUsd(stats.medianTradeUsd, { sign: true })} tone={stats.medianTradeUsd >= 0 ? 'good' : 'bad'} />
      <Stat label="Largest loss" value={fmtUsd(stats.largestLossUsd, { sign: true })} tone="bad" />
      <Stat label="Recovery" value={fmtRatio(stats.recoveryFactor)} tone={(stats.recoveryFactor ?? 0) >= 1 ? 'good' : 'bad'} />
    </div>
  );
}

function SideSummaryTable({ bySide }: { bySide: NonNullable<Crypto15mBacktest['bySide']> }) {
  return (
    <CompactStatsTable
      rows={[
        { label: 'Buy YES', ...bySide.YES },
        { label: 'Buy NO', ...bySide.NO },
      ]}
    />
  );
}

function BucketTable({ rows }: { rows: NonNullable<Crypto15mBacktest['entryPriceBuckets']> }) {
  return <CompactStatsTable rows={rows} />;
}

function CompactStatsTable({ rows }: { rows: ({ label: string } & NonNullable<Crypto15mBacktest['entryPriceBuckets']>[number])[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[11px]">
        <thead>
          <tr className="text-left text-krypt-dim">
            <th className="py-1 pr-2 font-normal">Bucket</th>
            <th className="py-1 pr-2 text-right font-normal">Trades</th>
            <th className="py-1 pr-2 text-right font-normal">Win</th>
            <th className="py-1 pr-2 text-right font-normal">Avg</th>
            <th className="py-1 text-right font-normal">P&L</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.label} className="border-t border-krypt-border/50">
              <td className="py-1 pr-2 text-white">{r.label}</td>
              <td className="py-1 pr-2 text-right font-mono text-krypt-dim">{r.n}</td>
              <td className="py-1 pr-2 text-right font-mono text-krypt-dim">{r.n ? `${(r.winRate * 100).toFixed(1)}%` : '—'}</td>
              <td className={cls('py-1 pr-2 text-right font-mono', r.avgPnlUsd >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}>
                {r.n ? fmtUsd(r.avgPnlUsd, { sign: true }) : '—'}
              </td>
              <td className={cls('py-1 text-right font-mono', r.pnlUsd >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}>
                {r.n ? fmtUsd(r.pnlUsd, { sign: true }) : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TradeTable({ trades, expanded, onToggle }: {
  trades: Crypto15mBacktest['trades'];
  expanded: string | null;
  onToggle: (id: string) => void;
}) {
  return (
    <div className="max-h-[520px] overflow-auto">
      <table className="min-w-[1380px] w-full text-[10px]">
        <thead className="sticky top-0 bg-krypt-surface text-left text-krypt-dim">
          <tr>
            <th className="py-1 pr-2 font-normal">Time</th>
            <th className="py-1 pr-2 font-normal">Ticker</th>
            <th className="py-1 pr-2 font-normal">Side</th>
            <th className="py-1 pr-2 text-right font-normal">Sec</th>
            <th className="py-1 pr-2 text-right font-normal">YES/NO ask</th>
            <th className="py-1 pr-2 text-right font-normal">Entry</th>
            <th className="py-1 pr-2 text-right font-normal">Lots</th>
            <th className="py-1 pr-2 text-right font-normal">Spot</th>
            <th className="py-1 pr-2 text-right font-normal">EMA12</th>
            <th className="py-1 pr-2 text-right font-normal">SMA20</th>
            <th className="py-1 pr-2 text-right font-normal">SMA50</th>
            <th className="py-1 pr-2 text-right font-normal">EMA-SMA</th>
            <th className="py-1 pr-2 text-right font-normal">Spot-SMA50</th>
            <th className="py-1 pr-2 text-right font-normal">Exit</th>
            <th className="py-1 pr-2 text-right font-normal">Fees</th>
            <th className="py-1 pr-2 text-right font-normal">P&L</th>
            <th className="py-1 text-right font-normal">Outcome</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((t, i) => {
            const id = `${t.ticker}-${t.at}-${i}`;
            const open = expanded === id;
            return (
              <Fragment key={id}>
                <tr
                  onClick={() => onToggle(id)}
                  className="cursor-pointer border-t border-krypt-border/50 hover:bg-krypt-surface2/50"
                >
                  <td className="py-1 pr-2 font-mono text-krypt-dim">{String(t.at || '').slice(5, 19)}</td>
                  <td className="py-1 pr-2 font-mono text-white">{t.ticker}</td>
                  <td className={cls('py-1 pr-2 font-mono', t.entrySide === 'YES' ? 'text-krypt-win' : 'text-krypt-loss')}>
                    {t.entrySide ?? t.side} <span className="text-krypt-dim">{t.signalType ?? ''}</span>
                  </td>
                  <td className="py-1 pr-2 text-right font-mono text-krypt-dim">{fmtMaybe(t.secondsLeft, 0)}</td>
                  <td className="py-1 pr-2 text-right font-mono text-krypt-dim">{fmtCents(t.yesAskCents)}/{fmtCents(t.noAskCents)}</td>
                  <td className="py-1 pr-2 text-right font-mono text-white">{fmtCents(t.entryPriceCents ?? t.costCents)}</td>
                  <td className="py-1 pr-2 text-right font-mono text-krypt-dim">{t.contracts ?? '—'}</td>
                  <td className="py-1 pr-2 text-right font-mono text-krypt-dim">{fmtMaybe(t.spotUsd, 1)}</td>
                  <td className="py-1 pr-2 text-right font-mono text-krypt-dim">{fmtMaybe(t.ema12_1m, 1)}</td>
                  <td className="py-1 pr-2 text-right font-mono text-krypt-dim">{fmtMaybe(t.sma20_1m, 1)}</td>
                  <td className="py-1 pr-2 text-right font-mono text-krypt-dim">{fmtMaybe(t.sma50_5m, 1)}</td>
                  <td className={cls('py-1 pr-2 text-right font-mono', (t.emaSpreadPct ?? 0) >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}>
                    {fmtSignedPct(t.emaSpreadPct)}
                  </td>
                  <td className={cls('py-1 pr-2 text-right font-mono', (t.trendDistancePct ?? 0) >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}>
                    {fmtSignedPct(t.trendDistancePct)}
                  </td>
                  <td className="py-1 pr-2 text-right font-mono text-krypt-dim">{t.exitReason ?? '—'} {fmtCents(t.exitPriceCents)}</td>
                  <td className="py-1 pr-2 text-right font-mono text-krypt-dim">{fmtUsd(t.feesUsd ?? 0)}</td>
                  <td className={cls('py-1 pr-2 text-right font-mono', t.pnlUsd >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}>
                    {fmtUsd(t.pnlUsd, { sign: true })}
                  </td>
                  <td className="py-1 text-right font-mono text-krypt-dim">{t.upWon == null ? '—' : t.upWon ? 'YES' : 'NO'}</td>
                </tr>
                {open && (
                  <tr className="border-t border-krypt-border/40 bg-krypt-surface2/30">
                    <td colSpan={17} className="px-2 py-2">
                      <div className="grid gap-1 font-mono text-[10px] text-krypt-dim sm:grid-cols-2">
                        {(t.timeline?.length ? t.timeline : [
                          `${t.at} - ${t.signalType ?? 'signal'} entry at ${fmtCents(t.entryPriceCents ?? t.costCents)}`,
                          `${t.exitReason ?? 'exit'} - ${fmtUsd(t.pnlUsd, { sign: true })} after fees`,
                        ]).map((line) => (
                          <div key={line}>{line}</div>
                        ))}
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function fmtRatio(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—';
  if (v > 9999) return '∞';
  return v.toFixed(2);
}

function fmtMaybe(v: number | null | undefined, digits = 2): string {
  return typeof v === 'number' && Number.isFinite(v) ? v.toFixed(digits) : '—';
}

function fmtCents(v: number | null | undefined): string {
  return typeof v === 'number' && Number.isFinite(v) ? `${v.toFixed(v % 1 ? 1 : 0)}¢` : '—';
}

function fmtSignedPct(v: number | null | undefined): string {
  return typeof v === 'number' && Number.isFinite(v) ? `${v >= 0 ? '+' : ''}${(v * 100).toFixed(3)}%` : '—';
}

function csvCell(v: unknown): string {
  if (v == null) return '';
  const s = Array.isArray(v) ? v.join(' | ') : String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function tradesToCsv(trades: Crypto15mBacktest['trades']): string {
  const headers: (keyof Crypto15mBacktest['trades'][number])[] = [
    'at', 'ticker', 'asset', 'entrySide', 'signalType', 'secondsLeft',
    'yesAskCents', 'noAskCents', 'entryPriceCents', 'contracts', 'spotUsd',
    'ema12_1m', 'sma20_1m', 'sma50_5m', 'emaSpreadPct', 'trendDistancePct',
    'exitReason', 'exitPriceCents', 'feesUsd', 'pnlUsd', 'upWon',
  ];
  return [
    headers.join(','),
    ...trades.map((t) => headers.map((h) => csvCell(t[h])).join(',')),
  ].join('\n');
}

function downloadCsv(filename: string, csv: string) {
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function downloadJson(filename: string, data: unknown) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function safeFilename(name: string): string {
  return name.replace(/[^a-z0-9-_]+/gi, '_').replace(/^_+|_+$/g, '') || 'krypt-profile';
}
