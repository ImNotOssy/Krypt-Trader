import { useEffect, useRef, useState } from 'react';
import { FlaskConical } from 'lucide-react';
import type { Crypto15mBacktest, TraderConfig } from '@shared/types';
import { backtestInputKey, isStaleBacktestResult } from '@shared/backtest-state';
import { useApp } from '../state/AppStateProvider';
import { cls, fmtUsd } from '../utils/format';

/** "Test this strategy on my data" — replays the CURRENT saved 15m config
 * through the live entry gates over the app's own recorded ticks. The number
 * users see is what the engine would actually have traded, fees included. */
export function BacktestPanel() {
  const { config } = useApp();
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<Crypto15mBacktest | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const latestBacktestKeyRef = useRef('');
  const runSeqRef = useRef(0);
  const activeBacktestKey = backtestInputKey({
    engine: 'crypto15m',
    selection: 'current',
    days: 60,
    patch: {},
    currentConfig: crypto15mConfigSlice(config),
  });

  useEffect(() => {
    latestBacktestKeyRef.current = activeBacktestKey;
    runSeqRef.current += 1;
    setBusy(false);
    setRes(null);
    setErr(null);
  }, [activeBacktestKey]);

  const rejectionEntries = res?.rejectionBreakdown?.length
    ? res.rejectionBreakdown.slice(0, 6)
    : res?.rejections
      ? Object.entries(res.rejections).slice(0, 6).map(([reason, count]) => ({
          reason,
          count,
          pct: (res.rejectionTotal ?? 0) > 0 ? count / (res.rejectionTotal ?? 1) : 0,
        }))
      : [];

  const run = async () => {
    const runKey = activeBacktestKey;
    const seq = ++runSeqRef.current;
    setBusy(true);
    setErr(null);
    setRes(null);
    try {
      const r = await window.krypt.crypto15m.backtest({ sinceDays: 60 });
      if (seq !== runSeqRef.current || isStaleBacktestResult(runKey, latestBacktestKeyRef.current)) return;
      setRes(r);
      if (!r) setErr('Engine not running — start the app backend first.');
    } catch (e: any) {
      if (seq !== runSeqRef.current || isStaleBacktestResult(runKey, latestBacktestKeyRef.current)) return;
      setErr(e?.message || String(e));
    } finally {
      if (seq === runSeqRef.current) setBusy(false);
    }
  };

  return (
    <div className="mt-3 rounded-xl border border-krypt-border bg-krypt-surface2/40 p-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-white">Test this strategy on my data</div>
          <p className="mt-0.5 text-[11px] leading-relaxed text-krypt-dim">
            Replays your current 15m settings (direction mode, thresholds, custom rules) through the
            <span className="text-white"> live entry gates</span> over every market this app has recorded
            and seen settle — taker fills at the recorded ask, Kalshi fees included, held to settlement.
          </p>
        </div>
        <button
          onClick={() => void run()}
          disabled={busy}
          className="inline-flex shrink-0 items-center gap-2 rounded-md border border-krypt-purple/40 bg-krypt-purple/10 px-3 py-1.5 text-xs text-krypt-purple transition-colors hover:bg-krypt-purple/20 disabled:opacity-50"
        >
          <FlaskConical className="h-3.5 w-3.5" />
          {busy ? 'Replaying…' : 'Run backtest'}
        </button>
      </div>
      {err && <p className="mt-2 text-[11px] text-krypt-loss">{err}</p>}
      {res && (
        <div className="mt-3">
          <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-5">
            <Stat label="Trades" value={`${res.n}`} sub={`${res.windowsScanned} windows scanned`} />
            <Stat label="Win rate" value={res.n ? `${(res.winRate * 100).toFixed(1)}%` : '—'} />
            <Stat
              label="Edge / contract" value={`${res.netEvCentsPerContract.toFixed(2)}¢`}
              tone={res.netEvCentsPerContract >= 0 ? 'good' : 'bad'}
            />
            <Stat
              label={`Total @ ${res.contracts} lots`} value={fmtUsd(res.totalPnlUsd, { sign: true })}
              tone={res.totalPnlUsd >= 0 ? 'good' : 'bad'}
            />
            <Stat label="Max drawdown" value={fmtUsd(res.maxDrawdownUsd)} tone="bad" />
          </div>
          {res.dataset && <DatasetManifestBar dataset={res.dataset} />}
          {Object.keys(res.byAsset).length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {Object.entries(res.byAsset).map(([a, st]) => (
                <span key={a} className="rounded bg-krypt-surface2 px-1.5 py-0.5 font-mono text-[10px] text-krypt-dim">
                  {a} {st.wins}/{st.n} <span className={st.pnlUsd >= 0 ? 'text-krypt-win' : 'text-krypt-loss'}>{fmtUsd(st.pnlUsd, { sign: true })}</span>
                </span>
              ))}
            </div>
          )}
          {rejectionEntries.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {rejectionEntries.map(({ reason, count, pct }) => (
                <span key={reason} className="rounded bg-krypt-surface2 px-1.5 py-0.5 font-mono text-[10px] text-krypt-dim">
                  {reason}: <span className="text-white">{count.toLocaleString()}</span>
                  <span className="ml-1 text-krypt-muted">{(pct * 100).toFixed(1)}%</span>
                </span>
              ))}
            </div>
          )}
          <ul className="mt-2 space-y-0.5">
            {res.caveats.map((c, i) => (
              <li key={i} className="text-[10px] leading-relaxed text-krypt-warn/80">⚠ {c}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function crypto15mConfigSlice(config: TraderConfig | null): Partial<TraderConfig> | null {
  if (!config) return null;
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(config)) {
    if (k.startsWith('crypto15m') && k !== 'crypto15mLive') out[k] = v;
  }
  return out as Partial<TraderConfig>;
}

function DatasetManifestBar({ dataset }: { dataset: NonNullable<Crypto15mBacktest['dataset']> }) {
  const rows = dataset.inSampleRowCount !== dataset.rowCount
    ? `${dataset.rowCount.toLocaleString()} rows / ${dataset.inSampleRowCount.toLocaleString()} in sample`
    : `${dataset.rowCount.toLocaleString()} rows`;
  return (
    <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md border border-krypt-border bg-krypt-surface2/40 px-2 py-1.5 text-[10px] text-krypt-dim">
      <span className="uppercase tracking-wide text-krypt-muted">Dataset</span>
      <code className="break-all text-white">{dataset.datasetId}</code>
      <span>{rows}</span>
      <span>{dataset.windows.toLocaleString()} windows</span>
      <span title={dataset.sha256}>sha {dataset.sha256.slice(0, 12)}</span>
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
