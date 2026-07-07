import { useEffect, useState } from 'react';
import { AlertTriangle, RadioTower, Sparkles, Tractor } from 'lucide-react';
import type { PerpPositionRow, PerpsStatus, PerpsWallet, RuleCondition, TraderConfig } from '@shared/types';
import { Card, Page, Switch, useOptimisticValue } from '../components/common';
import { useApp } from '../state/AppStateProvider';
import { cls, fmtUsd } from '../utils/format';

// Must mirror PERPS_RULE_FIELDS in python/perps_strategy.py (the sanitizer
// drops anything the engine doesn't compute).
const PERPS_RULE_FIELDS: { v: string; label: string }[] = [
  { v: 'price', label: 'Price (USD)' },
  { v: 'spreadBps', label: 'Spread (bps)' },
  { v: 'ret1mBps', label: '1m return (bps, signed)' },
  { v: 'ret5mBps', label: '5m return (bps, signed)' },
  { v: 'ret15mBps', label: '15m return (bps, signed)' },
  { v: 'ret60mBps', label: '1h return (bps, signed)' },
  { v: 'vol15mBps', label: '15m realized vol (bps)' },
  { v: 'vol60mBps', label: '1h realized vol (bps)' },
  { v: 'fromHigh60mBps', label: 'Below 1h high (bps)' },
  { v: 'fromLow60mBps', label: 'Above 1h low (bps)' },
  { v: 'volume15m', label: '15m volume (contracts)' },
  { v: 'volumeRatio', label: 'Volume ratio (15m vs 1h avg)' },
  { v: 'oiChange15mPct', label: 'Open-interest Δ 15m (%)' },
  { v: 'fundingRateBps', label: 'Funding rate (bps, last 8h)' },
  { v: 'minsToFunding', label: 'Minutes to next funding' },
  { v: 'hourUtc', label: 'Hour of day (UTC 0-23)' },
  { v: 'dowUtc', label: 'Day of week (0=Mon…6=Sun)' },
];
const RULE_OPS = ['>=', '<=', '>', '<'] as const;
const PERP_SYMBOLS = ['KXBTCPERP', 'KXETHPERP', 'KXSOLPERP', 'KXXRPPERP', 'KXDOGEPERP'];

// Strategy TEMPLATES — one-click starting points (like the 15m presets). These
// are NOT proven winners: a wide sweep of the recorded data found no profitable
// perps configuration (the ~5bps round-trip fee beats every signal). They exist
// so you can pick a coherent, understandable strategy, backtest it on YOUR data,
// and paper-trade — not to be armed blindly. Each sets direction + entry rules +
// TP/SL + a cheap maker entry; Market / size / leverage stay as you set them.
const PERPS_STRATS: { id: string; name: string; blurb: string; patch: Partial<TraderConfig> }[] = [
  { id: 'custom', name: 'My custom rules', blurb: 'Build your own from the fields below.', patch: {} },
  {
    id: 'momentum', name: 'Momentum burst',
    blurb: 'Long after a sharp 5-minute up-move (only when it’s moving enough to matter), ride it with a wide take-profit.',
    patch: {
      perpsStratDirection: 'long', perpsStratEntryStyle: 'maker',
      perpsStratRules: [{ field: 'ret5mBps', op: '>=', value: 25 }, { field: 'vol15mBps', op: '>=', value: 8 }],
      perpsStratTpBps: 40, perpsStratSlBps: 25, perpsStratMaxHoldMin: 45, perpsStratExitOnRulesFail: false,
    },
  },
  {
    id: 'dip', name: 'Dip reversal',
    blurb: 'Buy a 15-minute washout, betting on a bounce back.',
    patch: {
      perpsStratDirection: 'long', perpsStratEntryStyle: 'maker',
      perpsStratRules: [{ field: 'ret15mBps', op: '<=', value: -25 }],
      perpsStratTpBps: 40, perpsStratSlBps: 25, perpsStratMaxHoldMin: 45, perpsStratExitOnRulesFail: false,
    },
  },
  {
    id: 'breakout', name: 'Breakout',
    blurb: 'Long when price pushes to its 1-hour high with momentum behind it.',
    patch: {
      perpsStratDirection: 'long', perpsStratEntryStyle: 'maker',
      perpsStratRules: [{ field: 'fromHigh60mBps', op: '<=', value: 5 }, { field: 'ret5mBps', op: '>=', value: 8 }],
      perpsStratTpBps: 50, perpsStratSlBps: 30, perpsStratMaxHoldMin: 45, perpsStratExitOnRulesFail: false,
    },
  },
  {
    id: 'fade', name: 'Momentum fade',
    blurb: 'Short a sharp 5-minute spike, betting it mean-reverts.',
    patch: {
      perpsStratDirection: 'short', perpsStratEntryStyle: 'maker',
      perpsStratRules: [{ field: 'ret5mBps', op: '>=', value: 25 }],
      perpsStratTpBps: 30, perpsStratSlBps: 20, perpsStratMaxHoldMin: 30, perpsStratExitOnRulesFail: false,
    },
  },
  // ── regime-gated variants: only fire in a specific market state. Run a pair
  //    (e.g. Trend rider + Chop fader) in two paper shells for a crude
  //    "switch strategy by regime" behavior the single engine can't do alone. ──
  {
    id: 'trend-hivol', name: 'Trend rider (high-vol)',
    blurb: 'Long a sustained 15-min up-move, but only when volatility is high enough to clear fees.',
    patch: {
      perpsStratDirection: 'long', perpsStratEntryStyle: 'maker',
      perpsStratRules: [{ field: 'ret15mBps', op: '>=', value: 20 }, { field: 'vol15mBps', op: '>=', value: 12 }],
      perpsStratTpBps: 60, perpsStratSlBps: 35, perpsStratMaxHoldMin: 60, perpsStratExitOnRulesFail: true,
    },
  },
  {
    id: 'session-mom', name: 'Session momentum',
    blurb: 'Momentum long, but only during the active US hours (13–21 UTC).',
    patch: {
      perpsStratDirection: 'long', perpsStratEntryStyle: 'maker',
      perpsStratRules: [
        { field: 'ret5mBps', op: '>=', value: 20 },
        { field: 'hourUtc', op: '>=', value: 13 },
        { field: 'hourUtc', op: '<=', value: 21 },
      ],
      perpsStratTpBps: 40, perpsStratSlBps: 25, perpsStratMaxHoldMin: 45, perpsStratExitOnRulesFail: false,
    },
  },
  {
    id: 'funding-tilt', name: 'Funding harvest',
    blurb: 'Go long when funding is negative (shorts pay longs) — collect carry while holding.',
    patch: {
      perpsStratDirection: 'long', perpsStratEntryStyle: 'maker',
      perpsStratRules: [{ field: 'fundingRateBps', op: '<=', value: -3 }],
      perpsStratTpBps: 60, perpsStratSlBps: 40, perpsStratMaxHoldMin: 120, perpsStratExitOnRulesFail: false,
    },
  },
  {
    id: 'revert-scalp', name: 'Reversion scalp',
    blurb: 'Buy a fast 1-minute drop for a quick bounce, tight take-profit.',
    patch: {
      perpsStratDirection: 'long', perpsStratEntryStyle: 'maker',
      perpsStratRules: [{ field: 'ret1mBps', op: '<=', value: -15 }],
      perpsStratTpBps: 15, perpsStratSlBps: 12, perpsStratMaxHoldMin: 10, perpsStratExitOnRulesFail: false,
    },
  },
];

/** Perpetual futures — Kalshi's margin API.
 *
 * Today this page is the DATA side: a passive recorder streaming every
 * configured perp's quotes, trades, candles and funding rates into the local
 * research DB (the same one Backtest runs on). Strategy execution lands here
 * later — the research program (sniper filter, consistency arb) needs ~2
 * weeks of this data before anything is testable, so collection comes first.
 */
export function PerpsPage() {
  const { config } = useApp();
  const [st, setSt] = useState<PerpsStatus | null>(null);

  const load = async () => {
    try {
      setSt(await window.krypt.perps.status());
    } catch { /* backend down */ }
  };

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 3000);
    return () => clearInterval(t);
  }, []);

  const counts = st?.counts;

  return (
    <Page
      title="Perpetuals"
      subtitle="Kalshi perpetual futures (BTC, ETH, SOL…). Pick a strategy template or build your own, backtest it on your recorded candles (Backtest page → Perpetuals), then paper-trade before ever going live. Data collection runs quietly in the background — manage it on the Backtest page."
    >
      <div className="mb-4 rounded-lg border border-krypt-loss/40 bg-krypt-loss/5 p-3">
        <div className="flex items-start gap-2">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-krypt-loss" />
          <div className="text-[11px] leading-relaxed text-krypt-muted">
            <span className="font-semibold text-krypt-loss">Perpetual futures are leveraged derivatives and can lose more than you put in.</span>{' '}
            Positions can be liquidated automatically; funding payments accrue every 8 hours; fees are charged
            on notional, not margin. Our own 11-strategy audit of this venue (real backtests, adversarially
            verified) found <span className="font-semibold text-white">zero profitable configurations</span> —
            this panel exists so you can test YOUR ideas honestly, on your own recorded data, before risking a cent.
            Backtest first, paper-trade second, and only then consider going live with money you can afford to lose.
            Nothing here is financial advice.
          </div>
        </div>
      </div>

      <div className="mb-4">
        <WalletCard w={st?.wallet ?? null} />
      </div>

      <div className="mb-4 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-krypt-border/60 bg-krypt-surface2/40 px-3 py-2 text-[11px] text-krypt-dim">
        <span className="inline-flex items-center gap-1.5">
          <RadioTower className={cls('h-3 w-3', st?.wsConnected ? 'text-krypt-win' : 'text-krypt-dim')} />
          {st?.recording
            ? (st?.wsConnected ? `recording · live stream (${st.ws.env})` : 'recording · REST fallback (stream offline)')
            : 'data collection is off'}
        </span>
        {counts && <span>{counts.candles.toLocaleString()} candles · {counts.ticks.toLocaleString()} ticks</span>}
        {counts?.lastAt && <span>last capture {counts.lastAt.slice(5, 16)} UTC</span>}
        <span className="ml-auto text-krypt-dim/70">Turn collection &amp; 14-day backfill on/off on the Backtest page.</span>
      </div>

      <div className="mt-4">
        <StrategyCard st={st} onChanged={() => void load()} />
      </div>

      <div className="mt-4">
        <FarmerCard st={st} onChanged={() => void load()} />
      </div>

      <div className="mt-4">
        <Card header={<div className="text-xs uppercase tracking-wider text-krypt-muted">Markets</div>}>
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-krypt-dim">
                <th className="py-1 pr-3 font-normal">Symbol</th>
                <th className="py-1 pr-3 font-normal">Last</th>
                <th className="py-1 pr-3 font-normal">Bid / Ask</th>
                <th className="py-1 pr-3 font-normal">Index</th>
                <th className="py-1 pr-3 font-normal">Funding (8h)</th>
                <th className="py-1 font-normal">Recorded ticks</th>
              </tr>
            </thead>
            <tbody>
              {(st?.quotes ?? []).map((q) => {
                const t = counts?.byTicker.find((r) => r.ticker.replace(/1$/, '') === q.symbol);
                return (
                  <tr key={q.symbol} className="border-t border-krypt-border/50">
                    <td className="py-1.5 pr-3 font-mono text-white">{q.symbol}</td>
                    <td className="py-1.5 pr-3 font-mono text-white">{fmtPx(q.last)}</td>
                    <td className="py-1.5 pr-3 font-mono text-krypt-dim">
                      {fmtPx(q.bid)} / {fmtPx(q.ask)}
                    </td>
                    <td className="py-1.5 pr-3 font-mono text-krypt-dim">{fmtPx(q.ref)}</td>
                    <td className={cls(
                      'py-1.5 pr-3 font-mono',
                      q.fundingRate == null ? 'text-krypt-dim'
                        : q.fundingRate > 0 ? 'text-krypt-win' : q.fundingRate < 0 ? 'text-krypt-loss' : 'text-krypt-dim',
                    )}>
                      {q.fundingRate == null ? '—' : `${(q.fundingRate * 10000).toFixed(1)} bp`}
                    </td>
                    <td className="py-1.5 font-mono text-krypt-dim">{t ? t.ticks.toLocaleString() : '—'}</td>
                  </tr>
                );
              })}
              {(!st || st.quotes.length === 0) && (
                <tr><td colSpan={6} className="py-3 text-krypt-dim">
                  Waiting for the backend… quotes appear here once the live stream connects
                  (API keys required for the stream; recording falls back to REST without them).
                </td></tr>
              )}
            </tbody>
          </table>
          <p className="mt-2 text-[10px] leading-relaxed text-krypt-dim">
            Live prices come from the stream and refresh ~1/s per market. “Index” is the CF
            Benchmarks reference price — the same family of indexes the 15m markets settle on.
            Research data is always recorded from production markets, even in demo mode.
          </p>
        </Card>
      </div>
    </Page>
  );
}

function StrategyCard({ st, onChanged }: { st: PerpsStatus | null; onChanged: () => void }) {
  const { config } = useApp();
  const s = st?.strategy;
  const [showLiveModal, setShowLiveModal] = useState(false);
  const [ack, setAck] = useState(false);
  const [history, setHistory] = useState<PerpPositionRow[]>([]);
  const [flattening, setFlattening] = useState(false);
  const [presetId, setPresetId] = useState('custom');
  const [showRules, setShowRules] = useState(false);

  const update = (patch: Partial<TraderConfig>) => window.krypt.config.update(patch);

  const loadHistory = async () => {
    try {
      const h = await window.krypt.perps.history({ limit: 30 });
      if (h) setHistory(h.rows);
    } catch { /* backend down */ }
  };
  useEffect(() => {
    void loadHistory();
    const t = setInterval(() => void loadHistory(), 10000);
    return () => clearInterval(t);
  }, []);

  const [rules, setRules] = useOptimisticValue<RuleCondition[]>(
    config?.perpsStratRules ?? [],
    (next) => { void update({ perpsStratRules: next }); },
  );
  const addRule = () => setRules([...rules, { field: 'ret5mBps', op: '>', value: 10 }]);
  const removeRule = (i: number) => setRules(rules.filter((_, idx) => idx !== i));
  const patchRule = (i: number, patch: Partial<RuleCondition>) =>
    setRules(rules.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));

  const applyPreset = (p: (typeof PERPS_STRATS)[number]) => {
    setPresetId(p.id);
    if (p.id === 'custom') { setShowRules(true); return; }
    const { perpsStratRules: pr, ...rest } = p.patch;
    if (pr) setRules(pr as RuleCondition[]);
    if (Object.keys(rest).length) void update(rest);
    setShowRules(false);
  };

  const setNum = (key: keyof TraderConfig) => (e: React.ChangeEvent<HTMLInputElement>) => {
    const v = Number(e.target.value);
    if (Number.isFinite(v)) void update({ [key]: v } as Partial<TraderConfig>);
  };

  const armLive = async () => {
    await update({ perpsStratLive: true });
    setShowLiveModal(false);
    setAck(false);
    onChanged();
  };

  const flatten = async () => {
    setFlattening(true);
    try {
      await window.krypt.perps.stratFlatten();
      onChanged();
      void loadHistory();
    } finally {
      setFlattening(false);
    }
  };

  const leverage = config?.perpsStratLeverage ?? 1;

  return (
    <Card header={
      <div className="flex items-center gap-2">
        <Sparkles className="h-3.5 w-3.5 text-krypt-purple" />
        <div className="text-xs uppercase tracking-wider text-krypt-muted">Strategy builder</div>
        {s?.halted && (
          <span className="rounded-full border border-krypt-warn/40 bg-krypt-warn/10 px-2 py-0.5 text-[10px] uppercase tracking-wider text-krypt-warn">
            halted today
          </span>
        )}
        {s?.live && (
          <span className="rounded-full border border-krypt-loss/40 bg-krypt-loss/10 px-2 py-0.5 text-[10px] uppercase tracking-wider text-krypt-loss">
            LIVE — real money
          </span>
        )}
      </div>
    }>
      <p className="text-xs leading-relaxed text-krypt-dim">
        Pick a template to load a ready-made strategy, or build your own. Backtest, paper and live all run
        the <span className="text-white">identical entry/exit code</span>, so what you test is what trades —
        backtest on the Backtest page (engine: Perpetuals) first.
      </p>
      <div className="mt-3">
        <div className="mb-1.5 text-[10px] uppercase tracking-wide text-krypt-dim">Strategy template</div>
        <div className="flex flex-wrap gap-1.5">
          {PERPS_STRATS.map((p) => (
            <button
              key={p.id}
              onClick={() => applyPreset(p)}
              title={p.blurb}
              className={cls(
                'rounded-md border px-2.5 py-1 text-xs transition-colors',
                presetId === p.id
                  ? 'border-krypt-purple/60 bg-krypt-purple/10 text-white'
                  : 'border-krypt-border bg-krypt-surface2 text-krypt-dim hover:text-white',
              )}
            >
              {p.name}
            </button>
          ))}
        </div>
        {presetId !== 'custom' && (
          <p className="mt-1.5 text-[11px] leading-relaxed text-krypt-dim">
            {PERPS_STRATS.find((p) => p.id === presetId)?.blurb}{' '}
            <span className="text-krypt-warn">Template only — no perps config tested profitable in our data; backtest &amp; paper-trade before arming.</span>
          </p>
        )}
      </div>
      <div className="mt-3 grid gap-4 lg:grid-cols-2">
        <div>
          <div className="grid grid-cols-2 gap-2">
            <label className="text-[11px] text-krypt-dim">
              Market
              <select
                value={config?.perpsStratSymbol ?? 'KXBTCPERP'}
                onChange={(e) => void update({ perpsStratSymbol: e.target.value })}
                className="mt-1 w-full rounded-md border border-krypt-border bg-krypt-surface2 px-2 py-1 text-xs text-white outline-none focus:border-krypt-purple/60"
              >
                {PERP_SYMBOLS.map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
            </label>
            <label className="text-[11px] text-krypt-dim">
              Direction
              <div className="mt-1 flex gap-1.5">
                {(['long', 'short'] as const).map((d) => (
                  <button
                    key={d}
                    onClick={() => void update({ perpsStratDirection: d })}
                    className={cls(
                      'flex-1 rounded-md border px-2 py-1 text-xs uppercase transition-colors',
                      (config?.perpsStratDirection ?? 'long') === d
                        ? d === 'long'
                          ? 'border-krypt-win/60 bg-krypt-win/10 text-krypt-win'
                          : 'border-krypt-loss/60 bg-krypt-loss/10 text-krypt-loss'
                        : 'border-krypt-border bg-krypt-surface2 text-krypt-dim hover:text-white',
                    )}
                  >
                    {d}
                  </button>
                ))}
              </div>
            </label>
          </div>

          <div className="mt-3 rounded-lg border border-krypt-border bg-krypt-surface2 p-3">
            <button
              onClick={() => setShowRules((v) => !v)}
              className="flex w-full items-center justify-between text-left"
            >
              <span className="text-[11px] font-semibold uppercase tracking-wider text-krypt-dim">
                Entry rules (ALL must pass) · {rules.length} set
              </span>
              <span className="text-[11px] text-krypt-dim">{showRules ? '▾ hide' : '▸ edit'}</span>
            </button>
            {rules.length === 0 && (
              <div className="mt-2 rounded-md border border-krypt-warn/30 bg-krypt-warn/5 px-2 py-1.5 text-[11px] text-krypt-warn">
                No conditions set — the strategy never enters. Pick a template above, or “edit” to add one.
              </div>
            )}
            {showRules && (<>
            <div className="mt-2 flex flex-col gap-2">
              {rules.map((r, i) => (
                <div key={i} className="flex items-center gap-2">
                  <select
                    value={r.field}
                    onChange={(e) => patchRule(i, { field: e.target.value })}
                    className="flex-1 rounded-md border border-krypt-border bg-krypt-surface px-2 py-1 text-xs text-white outline-none"
                  >
                    {PERPS_RULE_FIELDS.map((f) => <option key={f.v} value={f.v}>{f.label}</option>)}
                  </select>
                  <select
                    value={r.op}
                    onChange={(e) => patchRule(i, { op: e.target.value as RuleCondition['op'] })}
                    className="w-16 rounded-md border border-krypt-border bg-krypt-surface px-2 py-1 text-xs text-white outline-none"
                  >
                    {RULE_OPS.map((o) => <option key={o} value={o}>{o}</option>)}
                  </select>
                  <StratValueInput value={r.value} onCommit={(n) => patchRule(i, { value: n })} />
                  <button
                    onClick={() => removeRule(i)}
                    className="px-2 py-1 text-xs text-krypt-loss"
                    title="Remove condition"
                  >
                    ✕
                  </button>
                </div>
              ))}
              <button
                onClick={addRule}
                className="self-start rounded-md border border-krypt-border bg-krypt-surface px-2.5 py-1 text-[11px] text-krypt-muted transition-colors hover:border-krypt-purple/40 hover:text-white"
              >
                + Add condition
              </button>
            </div>
            <p className="mt-2 text-[10px] text-krypt-dim">
              e.g. <span className="font-mono">5m return &gt; 10</span> (momentum),{' '}
              <span className="font-mono">below 1h high ≥ 30</span> (dip),{' '}
              <span className="font-mono">15m vol ≥ 8</span> (only trade when it moves enough to beat fees).
            </p>
            </>)}
          </div>

          <div className="mt-3 grid grid-cols-3 gap-2">
            <NumField label="Contracts" value={config?.perpsStratContracts ?? 1} min={1} max={500} onCommit={setNum('perpsStratContracts')} />
            <NumField label="Take profit (bps)" value={config?.perpsStratTpBps ?? 30} min={0} max={5000} onCommit={setNum('perpsStratTpBps')} />
            <NumField label="Stop loss (bps)" value={config?.perpsStratSlBps ?? 20} min={0} max={5000} onCommit={setNum('perpsStratSlBps')} />
            <NumField label="Max hold (min)" value={config?.perpsStratMaxHoldMin ?? 60} min={0} max={10080} onCommit={setNum('perpsStratMaxHoldMin')} />
            <NumField label="Daily loss cap ($)" value={config?.perpsStratDailyLossUsd ?? 5} min={0.5} max={10000} onCommit={setNum('perpsStratDailyLossUsd')} />
            <NumField label="Max notional ($)" value={config?.perpsStratMaxNotionalUsd ?? 100} min={5} max={100000} onCommit={setNum('perpsStratMaxNotionalUsd')} />
          </div>
          <label className="mt-2 block text-[11px] text-krypt-dim">
            Leverage: <span className={cls('font-mono', leverage > 3 ? 'text-krypt-loss' : leverage > 1.5 ? 'text-krypt-warn' : 'text-white')}>{leverage.toFixed(1)}x</span>
            {leverage > 3 && <span className="ml-2 text-krypt-loss">high liquidation risk</span>}
            <input
              type="range" min={1} max={5} step={0.5} value={leverage}
              onChange={(e) => void update({ perpsStratLeverage: Number(e.target.value) })}
              className="mt-1 w-full"
            />
          </label>
        </div>

        <div>
          <Switch
            checked={config?.perpsStratEnabled ?? false}
            onChange={(v) => { void update({ perpsStratEnabled: v, ...(v ? {} : { perpsStratLive: false }) }); onChanged(); }}
            label="Paper-trade this strategy"
            description="Runs your rules against live perp quotes and books SIMULATED fills (at the real bid/ask, with today's real fees) into the history below. No orders are placed. This is the mandatory dress rehearsal."
          />
          <div className="mt-2">
            <Switch
              checked={config?.perpsStratLive ?? false}
              onChange={(v) => {
                if (v) setShowLiveModal(true);
                else { void update({ perpsStratLive: false }); onChanged(); }
              }}
              disabled={!config?.perpsStratEnabled}
              label="Go LIVE (real leveraged orders)"
              description="Places real orders on your Kalshi perps wallet using the exact same rules. Requires paper mode on, a funded perps wallet, and your explicit risk acknowledgement. Keep the daily loss cap tight."
            />
          </div>

          <div className="mt-3 rounded-lg bg-krypt-surface2/60 p-2 text-[11px] text-krypt-dim">
            {s ? (
              <>
                <div className="flex flex-wrap gap-x-4 gap-y-1">
                  <span>status: <span className="text-white">{!s.enabled ? 'off' : s.halted ? 'halted' : s.openPosition ? 'in position' : 'scanning'}</span></span>
                  <span>day P&L: <span className={cls('font-mono', s.dayPnlUsd >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}>{fmtUsd(s.dayPnlUsd, { sign: true })}</span></span>
                  <span>bars: <span className="font-mono text-white">{s.bars}</span></span>
                </div>
                {s.enabled && !s.openPosition && s.lastReason && (
                  <div className="mt-1">last check: {s.lastReason}</div>
                )}
                {s.halted && <div className="mt-1 text-krypt-warn">⚠ {s.haltReason}</div>}
                {s.lastError && <div className="mt-1 text-krypt-warn">⚠ {s.lastError}</div>}
                {s.openPosition && (
                  <div className="mt-1 font-mono text-white">
                    {s.openPosition.dryRun ? 'PAPER' : 'LIVE'} {s.openPosition.side.toUpperCase()}{' '}
                    {s.openPosition.contracts}ct {s.openPosition.ticker} @ ${s.openPosition.entry.toFixed(4)}
                    {s.openPosition.unrealizedUsd != null && (
                      <span className={s.openPosition.unrealizedUsd >= 0 ? 'text-krypt-win' : 'text-krypt-loss'}>
                        {' '}({fmtUsd(s.openPosition.unrealizedUsd, { sign: true })})
                      </span>
                    )}
                  </div>
                )}
              </>
            ) : '…'}
          </div>
          <button
            onClick={() => void flatten()}
            disabled={flattening || !s?.openPosition}
            className="mt-2 rounded-md border border-krypt-border bg-krypt-surface2 px-3 py-1.5 text-xs text-krypt-dim transition-colors hover:text-white disabled:opacity-50"
          >
            {flattening ? 'Closing…' : 'Close position now'}
          </button>

          <div className="mt-3">
            <div className="mb-1 text-[10px] uppercase tracking-wide text-krypt-dim">Recent strategy trades</div>
            <table className="w-full text-[11px]">
              <thead><tr className="text-left text-krypt-dim">
                <th className="py-0.5 pr-2 font-normal">Mode</th>
                <th className="py-0.5 pr-2 font-normal">Side</th>
                <th className="py-0.5 pr-2 font-normal">Entry → Exit</th>
                <th className="py-0.5 pr-2 font-normal">Why out</th>
                <th className="py-0.5 font-normal">P&L</th>
              </tr></thead>
              <tbody>
                {history.filter((r) => r.closed_at).slice(0, 8).map((r) => (
                  <tr key={r.id} className="border-t border-krypt-border/50">
                    <td className="py-1 pr-2">{r.dry_run ? <span className="text-krypt-dim">paper</span> : <span className="text-krypt-loss">live</span>}</td>
                    <td className="py-1 pr-2 font-mono text-white">{r.side}</td>
                    <td className="py-1 pr-2 font-mono text-krypt-dim">
                      {r.entryUsd?.toFixed(4)} → {r.exitUsd?.toFixed(4)}
                    </td>
                    <td className="py-1 pr-2 text-krypt-dim">{r.exit_reason}</td>
                    <td className={cls('py-1 font-mono', (r.pnlUsd ?? 0) >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}>
                      {r.pnlUsd != null ? fmtUsd(r.pnlUsd, { sign: true }) : '—'}
                    </td>
                  </tr>
                ))}
                {history.filter((r) => r.closed_at).length === 0 && (
                  <tr><td colSpan={5} className="py-2 text-krypt-dim">No trades yet — enable paper mode and let it scan.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {showLiveModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-6">
          <div className="w-full max-w-lg rounded-xl border border-krypt-loss/40 bg-krypt-surface p-5">
            <div className="flex items-center gap-2 text-sm font-semibold text-krypt-loss">
              <AlertTriangle className="h-4 w-4" /> Real leveraged trading — read this
            </div>
            <ul className="mt-3 list-disc space-y-1.5 pl-5 text-xs leading-relaxed text-krypt-muted">
              <li><span className="text-white">You can lose more than your margin.</span> Leveraged perpetual futures are liquidated automatically when the market moves against you; at {leverage.toFixed(1)}x a ~{Math.round(9000 / leverage) / 100}% adverse move wipes the position.</li>
              <li><span className="text-white">Fees are on notional:</span> every taker fill currently costs 80bps of position size (dropping to ~12bps with Kalshi's fee tiers). Small edges do not survive them — check your backtest's fee-era numbers.</li>
              <li><span className="text-white">Funding accrues every 8h</span> (04/12/20 UTC) while you hold.</li>
              <li><span className="text-white">Our own audit found no profitable strategy on this venue.</span> If your backtest shows profits, it is more likely overfit than a discovery. Paper results ≠ live results.</li>
              <li>The bot trades your <span className="text-white">separate perps wallet</span>; it must be funded on Kalshi first. The daily loss cap (${(config?.perpsStratDailyLossUsd ?? 5).toFixed(0)}) halts the strategy for the day — it does not undo losses.</li>
              <li>This is experimental open-source software with no warranty. Nothing here is financial advice. You alone are responsible for your trades.</li>
            </ul>
            <label className="mt-3 flex items-start gap-2 text-xs text-krypt-muted">
              <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} className="mt-0.5" />
              I understand I can lose money quickly, including more than I planned, and I accept full responsibility.
            </label>
            <div className="mt-4 flex justify-end gap-2">
              <button
                onClick={() => { setShowLiveModal(false); setAck(false); }}
                className="rounded-md border border-krypt-border bg-krypt-surface2 px-3 py-1.5 text-xs text-krypt-dim hover:text-white"
              >
                Cancel
              </button>
              <button
                onClick={() => void armLive()}
                disabled={!ack}
                className="rounded-md border border-krypt-loss/50 bg-krypt-loss/15 px-3 py-1.5 text-xs font-semibold text-krypt-loss disabled:opacity-40"
              >
                Arm live trading
              </button>
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}

function NumField({ label, value, min, max, onCommit }: {
  label: string; value: number; min: number; max: number;
  onCommit: (e: React.ChangeEvent<HTMLInputElement>) => void;
}) {
  return (
    <label className="text-[11px] text-krypt-dim">
      {label}
      <input
        type="number" min={min} max={max} defaultValue={value} onBlur={onCommit}
        className="mt-1 w-full rounded-md border border-krypt-border bg-krypt-surface2 px-2 py-1 text-xs text-white outline-none focus:border-krypt-purple/60"
      />
    </label>
  );
}

/** Text-buffered numeric input committed on blur/Enter (the 15m builder's
 * pattern — type="number" sanitizes "0." mid-type and config echoes snap the
 * cursor). */
function StratValueInput({ value, onCommit }: { value: number; onCommit: (n: number) => void }) {
  const [text, setText] = useState(String(value));
  useEffect(() => { setText(String(value)); }, [value]);
  const commit = (): void => {
    const n = Number(text);
    if (text.trim() !== '' && !Number.isNaN(n) && n !== value) onCommit(n);
    else setText(String(value));
  };
  return (
    <input
      type="text"
      inputMode="decimal"
      value={text}
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
      className="w-24 rounded-md border border-krypt-border bg-krypt-surface px-2 py-1 font-mono text-xs text-white outline-none"
    />
  );
}

function FarmerCard({ st, onChanged }: { st: PerpsStatus | null; onChanged: () => void }) {
  const { config } = useApp();
  const f = st?.farmer;
  const [flattening, setFlattening] = useState(false);

  const toggle = async (on: boolean) => {
    await window.krypt.config.update({ perpsFarmEnabled: on });
    onChanged();
  };

  const flatten = async () => {
    setFlattening(true);
    try {
      await window.krypt.perps.farmFlatten();
      onChanged();
    } finally {
      setFlattening(false);
    }
  };

  const setNum = (key: 'perpsFarmClipContracts' | 'perpsFarmMaxInventoryContracts' | 'perpsFarmDailyLossUsd' | 'perpsFarmDailyVolumeUsd' | 'perpsFarmMaxFeeBps') =>
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const v = Number(e.target.value);
      if (Number.isFinite(v)) void window.krypt.config.update({ [key]: v });
    };

  return (
    <Card header={
      <div className="flex items-center gap-2">
        <Tractor className="h-3.5 w-3.5 text-krypt-purple" />
        <div className="text-xs uppercase tracking-wider text-krypt-muted">Volume farmer</div>
        {f?.halted && (
          <span className="rounded-full border border-krypt-warn/40 bg-krypt-warn/10 px-2 py-0.5 text-[10px] uppercase tracking-wider text-krypt-warn">
            halted today
          </span>
        )}
      </div>
    }>
      <div className="grid gap-4 lg:grid-cols-2">
        <div>
          <Switch
            checked={config?.perpsFarmEnabled ?? false}
            onChange={(v) => void toggle(v)}
            label="Farm perps volume (maker-only)"
            description="Rests one small buy and one small sell at the best bid/ask and lets the market trade through them — real two-sided liquidity, never taker, never self-matching. Generates the volume Kalshi's perps rewards pay on, while captured spread offsets maker fees. Auto-halts for the day if the measured cost per $ of volume exceeds the reward rate, or at the daily loss cap. Needs funds in the perps wallet."
          />
          <div className="mt-3 grid grid-cols-2 gap-2">
            <label className="text-[11px] text-krypt-dim">
              Clip (contracts)
              <input type="number" min={1} max={100} defaultValue={config?.perpsFarmClipContracts ?? 1}
                onBlur={setNum('perpsFarmClipContracts')}
                className="mt-1 w-full rounded-md border border-krypt-border bg-krypt-surface2 px-2 py-1 text-xs text-white outline-none focus:border-krypt-purple/60" />
            </label>
            <label className="text-[11px] text-krypt-dim">
              Max inventory (contracts)
              <input type="number" min={1} max={1000} defaultValue={config?.perpsFarmMaxInventoryContracts ?? 3}
                onBlur={setNum('perpsFarmMaxInventoryContracts')}
                className="mt-1 w-full rounded-md border border-krypt-border bg-krypt-surface2 px-2 py-1 text-xs text-white outline-none focus:border-krypt-purple/60" />
            </label>
            <label className="text-[11px] text-krypt-dim">
              Daily loss cap ($)
              <input type="number" min={0.1} step={0.5} defaultValue={config?.perpsFarmDailyLossUsd ?? 2}
                onBlur={setNum('perpsFarmDailyLossUsd')}
                className="mt-1 w-full rounded-md border border-krypt-border bg-krypt-surface2 px-2 py-1 text-xs text-white outline-none focus:border-krypt-purple/60" />
            </label>
            <label className="text-[11px] text-krypt-dim">
              Daily volume target ($, 0 = off)
              <input type="number" min={0} step={1000} defaultValue={config?.perpsFarmDailyVolumeUsd ?? 0}
                onBlur={setNum('perpsFarmDailyVolumeUsd')}
                className="mt-1 w-full rounded-md border border-krypt-border bg-krypt-surface2 px-2 py-1 text-xs text-white outline-none focus:border-krypt-purple/60" />
            </label>
            <label className="col-span-2 text-[11px] text-krypt-dim">
              Only farm when maker fee ≤ (bps, 0 = off)
              <input type="number" min={0} max={100} step={0.1} defaultValue={config?.perpsFarmMaxFeeBps ?? 0}
                onBlur={setNum('perpsFarmMaxFeeBps')}
                className="mt-1 w-full rounded-md border border-krypt-border bg-krypt-surface2 px-2 py-1 text-xs text-white outline-none focus:border-krypt-purple/60" />
              <span className="mt-1 block text-[10px] leading-snug text-krypt-dim">
                Farming costs ≈ the maker fee, and volume rewards pay only ~2 bps — so at today’s 5 bps
                fee it loses. Set this to your reward rate (e.g. 2) and the farmer stays idle until Kalshi’s
                fee is actually low enough to make volume free. 0 = farm regardless of fee.
              </span>
            </label>
          </div>
        </div>
        <div>
          <div className="grid grid-cols-3 gap-2">
            <Stat label="Volume today" value={f ? `$${f.today.volumeUsd.toLocaleString()}` : '…'} />
            <Stat label="Fees today" value={f ? fmtUsd(-f.today.feesUsd, { sign: true }) : '…'} />
            <Stat label="Net today" value={f ? fmtUsd(f.today.netUsd, { sign: true }) : '…'} />
            <Stat label="Cost / volume" value={f ? `${f.today.costBps.toFixed(1)} bp` : '…'} />
            <Stat label="Fills" value={f ? String(f.today.fills) : '…'} />
            <Stat label="Inventory" value={f ? `${f.inventoryContracts} ct` : '…'} />
            <Stat
              label="Maker fee"
              value={f ? (f.makerFeeBps != null ? `${f.makerFeeBps.toFixed(1)} bp` : '~5 bp*') : '…'}
            />
            <Stat label="Fee cap" value={f && f.maxFeeBps > 0 ? `${f.maxFeeBps} bp` : 'off'} />
          </div>
          <div className="mt-2 space-y-1 text-[11px] text-krypt-dim">
            {f?.liveOrders.map((o) => (
              <div key={o.side} className="font-mono">
                resting {o.side === 'bid' ? 'BUY' : 'SELL'} {o.contracts} @ ${o.price.toFixed(4)}
              </div>
            ))}
            {f?.halted && <div className="text-krypt-warn">⚠ {f.haltReason}</div>}
            {!f?.halted && f?.lastError && <div className="text-krypt-warn">⚠ {f.lastError}</div>}
            {f?.maintenanceWindow && <div className="text-krypt-warn">⚠ Kalshi maintenance window — standing down</div>}
            {f && f.makerFeeBps == null && f.maxFeeBps > 0 && (
              <div>* maker fee assumed at Tier-0 (5 bp) until real fills measure it</div>
            )}
          </div>
          <button
            onClick={() => void flatten()}
            disabled={flattening || !f || (f.inventoryContracts === 0 && f.liveOrders.length === 0)}
            className="mt-3 rounded-md border border-krypt-border bg-krypt-surface2 px-3 py-1.5 text-xs text-krypt-dim transition-colors hover:text-white disabled:opacity-50"
          >
            {flattening ? 'Flattening…' : 'Cancel quotes + flatten inventory'}
          </button>
          <p className="mt-2 text-[10px] leading-relaxed text-krypt-dim">
            The math: maker fee is 5 bps/side (dropping with volume tiers from ~Jul 8) minus
            ~1–3 bps captured spread. The volume bonuses pay ~2–4 bps — near break-even, so the
            farmer measures its real cost live and stops the moment farming costs more than the
            bonus pays. The perps wallet is separate from your event balance: transfer funds on
            Kalshi before arming.
          </p>
        </div>
      </div>
    </Card>
  );
}

function fmtPx(v: number | null | undefined): string {
  if (v == null) return '—';
  return `$${v.toFixed(4)}`;
}

function WalletCard({ w }: { w: PerpsWallet | null }) {
  return (
    <Card
      header={
        <div className="flex flex-wrap items-center justify-between gap-1">
          <div className="text-xs uppercase tracking-wider text-krypt-muted">Perpetuals wallet</div>
          <div className="text-[10px] text-krypt-dim">
            {w?.env ? `${w.env} · ` : ''}separate wallet from your main Kalshi cash
          </div>
        </div>
      }
    >
      {w ? (
        <>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Stat label="Balance" value={fmtUsd(w.settledUsd)} />
            <Stat label="Available to trade" value={fmtUsd(w.availableUsd)} />
            <Stat label="Open positions" value={fmtUsd(w.positionValueUsd)} />
            <Stat label="In resting orders" value={fmtUsd(w.restingMarginUsd)} />
          </div>
          <div className="mt-2 text-[11px] text-krypt-dim">
            Fund this wallet by transferring cash to Perpetuals on Kalshi — money in your main event-market
            balance can’t be traded here, and vice versa.
          </div>
        </>
      ) : (
        <div className="text-xs leading-relaxed text-krypt-dim">
          Perps wallet unavailable — this needs API keys on an account with margin (perpetuals) enabled.
          Once enabled, transfer funds into your Perpetuals wallet on Kalshi and the balance shows here.
        </div>
      )}
    </Card>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg bg-krypt-surface2/60 p-2">
      <div className="text-[10px] uppercase tracking-wide text-krypt-dim">{label}</div>
      <div className="font-mono text-sm text-white">{value}</div>
    </div>
  );
}
