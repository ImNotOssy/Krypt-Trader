import { app, BrowserWindow, ipcMain, shell } from 'electron';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import type {
  ActionResult,
  AppState,
  BotPosition,
  CredentialsInput,
  CredentialsState,
  PositionFilter,
  Profile,
  ProfileKind,
  RuleCondition,
  SignalFilter,
  StrategyPreset,
  TraderConfig,
} from '../shared/types';
import { setStartWithWindows } from './system/autostart';
import { pythonBackend } from './system/python-backend';
import * as store from './system/settings-store';
import { findStrategy, listStrategies } from './system/strategies';


const ok = <T>(data?: T, message?: string): ActionResult<T> => ({
  ok: true,
  data,
  message,
});
const err = (message: string): ActionResult => ({ ok: false, message });

// Profiles store a full config snapshot, but applying one only patches the
// slice for its engine — so a main profile never disturbs the 15m crypto
// settings and vice-versa, and neither re-arms the env / live switches.
// Webhook URLs are excluded too: profiles can be exported/imported and shared,
// so applying one must never silently redirect (or inject) a Discord webhook
// that exfiltrates balance/P&L/positions.
const MAIN_EXCLUDE = new Set([
  'kalshiEnv', 'enableTrading',
  'eventWebhookUrl', 'statsWebhookUrl', 'whaleWebhookUrl', 'momentumWebhookUrl',
]);
const CRYPTO_ARM_EXCLUDE = new Set(['crypto15mEnabled', 'crypto15mLive']);

// Per-source category lists are preset-only: no Settings control writes them,
// so a custom profile must neither carry them invisibly nor leave a previous
// preset's hidden filter active. They are excluded from profile slices and
// explicitly reset when a main profile is applied (built-in strategies still
// own them via their full preset config).
const PRESET_ONLY = new Set(['allowedWhaleCategories', 'allowedMomentumCategories']);

const isCrypto15mKey = (k: string): boolean => k.startsWith('crypto15m');

function profileSlice(config: TraderConfig, kind: ProfileKind): Partial<TraderConfig> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(config)) {
    const crypto = isCrypto15mKey(k);
    if (kind === 'crypto15m') {
      if (crypto && !CRYPTO_ARM_EXCLUDE.has(k)) out[k] = v;
    } else if (!crypto && !MAIN_EXCLUDE.has(k) && !PRESET_ONLY.has(k)) {
      out[k] = v;
    }
  }
  return out as Partial<TraderConfig>;
}

// Applying a built-in strategy resets the strategy-tunable gates to the
// preset, but must never factory-reset the user's personal/operational
// settings: webhooks (MAIN_EXCLUDE — same reasoning as profiles above),
// notifications, bankroll, daily risk limits, trading hours, and loop
// intervals are not part of any preset's edge, and no preset declares them.
const STRATEGY_PRESERVE: string[] = [
  ...MAIN_EXCLUDE,
  'enableDiscord', 'statsPushInterval', 'statsChartWindowHours',
  'startBankrollUsd', 'stopLossOnDay', 'stopLossOnDayPct', 'takeProfitOnDay',
  'tradingHoursEnabled', 'tradingHoursStart', 'tradingHoursEnd',
  'tradingDays', 'tradingTimezoneOffsetMin',
  'tradeScanInterval', 'positionPollInterval', 'balancePollInterval',
  'resolutionCheckInterval', 'whaleScanInterval', 'momentumScanInterval',
  'marketRefreshInterval',
];

// Shared by both strategy-apply paths: reset the main engine to the preset,
// but preserve the independently-tuned 15m crypto slice, its arm switches,
// and every STRATEGY_PRESERVE key from the current config.
function applyStrategyPreset(s: StrategyPreset): AppState {
  const curCfg = store.get().config;
  const preserved: Record<string, unknown> = {};
  for (const k of STRATEGY_PRESERVE) preserved[k] = (curCfg as unknown as Record<string, unknown>)[k];
  return store.replaceConfig({
    ...s.config,
    ...profileSlice(curCfg, 'crypto15m'),
    crypto15mEnabled: curCfg.crypto15mEnabled,
    crypto15mLive: curCfg.crypto15mLive,
    ...preserved,
  } as TraderConfig);
}

// Imported profile JSON is untrusted: coerce every known key to the type of
// its DEFAULT_CONFIG counterpart and drop what doesn't fit — the backend does
// not re-coerce the main-engine gate keys, so e.g. a string minConfidenceWhale
// ("60") would crash every trade scan cycle with a float<str TypeError.
function sanitizeImportedConfig(raw: unknown): Partial<TraderConfig> {
  const out: Record<string, unknown> = {};
  if (!raw || typeof raw !== 'object') return out as Partial<TraderConfig>;
  const defaults = store.DEFAULT_CONFIG as unknown as Record<string, unknown>;
  const drop = (k: string, why: string): void => {
    appendLog({
      ts: new Date().toISOString(), level: 'WARN', source: 'main',
      msg: `profile import: dropped config key "${k}" (${why})`,
    });
  };
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    if (!(k in defaults)) { drop(k, 'unknown key'); continue; }
    if (k === 'crypto15mRules') {
      if (!Array.isArray(v)) { drop(k, 'expected an array'); continue; }
      out[k] = v
        .filter((r: any) => r && typeof r === 'object'
          && typeof r.field === 'string'
          && ['>=', '<=', '>', '<'].includes(r.op)
          && Number.isFinite(Number(r.value)))
        .map((r: any): RuleCondition => ({ field: r.field, op: r.op, value: Number(r.value) }));
      continue;
    }
    const d = defaults[k];
    if (d === null || Array.isArray(d)) {
      // string-list keys; the null-defaulted ones (allowed*Categories,
      // crypto15mAssets) also accept null = "no restriction".
      if (v === null && d === null) { out[k] = null; continue; }
      if (Array.isArray(v)) { out[k] = v.filter((x) => typeof x === 'string'); continue; }
      drop(k, 'expected a string list');
      continue;
    }
    switch (typeof d) {
      case 'number': {
        if (v === null && k === 'orderExpirationSec') { out[k] = null; break; }
        const n = typeof v === 'number' ? v
          : typeof v === 'string' && v.trim() !== '' ? Number(v) : NaN;
        if (Number.isFinite(n)) out[k] = n;
        else drop(k, 'expected a number');
        break;
      }
      case 'boolean':
        if (typeof v === 'boolean') out[k] = v;
        else if (v === 'true' || v === 'false') out[k] = v === 'true';
        else drop(k, 'expected a boolean');
        break;
      case 'string':
        if (typeof v === 'string') out[k] = v;
        else drop(k, 'expected a string');
        break;
      default:
        drop(k, 'unsupported type');
        break;
    }
  }
  return out as Partial<TraderConfig>;
}

const profileKindOf = (p: Profile): ProfileKind => (p.kind === 'crypto15m' ? 'crypto15m' : 'main');

// A manual config edit diverges from any applied strategy/profile snapshot, so
// drop the matching "active" marker — otherwise the Strategies/Profiles UI keeps
// flagging a strategy as "Active" after its settings were changed. Env / master
// switch (MAIN_EXCLUDE) and the 15m arm switches (CRYPTO_ARM_EXCLUDE) aren't part
// of a saved snapshot, so they don't clear it.
function clearActiveMarkersForPatch(patch: Partial<TraderConfig>): void {
  const keys = Object.keys(patch || {});
  const touchesMain = keys.some((k) => !isCrypto15mKey(k) && !MAIN_EXCLUDE.has(k));
  const touchesCrypto = keys.some((k) => isCrypto15mKey(k) && !CRYPTO_ARM_EXCLUDE.has(k));
  const cur = store.get();
  const next = { ...cur };
  let changed = false;
  if (touchesMain && cur.activeProfileId) { next.activeProfileId = null; changed = true; }
  if (touchesCrypto && cur.activeCrypto15mProfileId) { next.activeCrypto15mProfileId = null; changed = true; }
  if (changed) store.save(next);
}

function broadcastState(state: AppState): void {
  for (const win of BrowserWindow.getAllWindows()) {
    if (!win.isDestroyed()) {
      win.webContents.send('state:changed', state);
    }
  }
}

function genId(): string {
  return `p_${Date.now().toString(36)}_${Math.floor(Math.random() * 1e6).toString(36)}`;
}

// 'ok' = backend confirmed the new config; 'not_running' = nothing to push
// (config re-syncs on backend:ready); 'failed' = the backend is up but did
// not acknowledge the change — the engine may still run the OLD config.
type PushResult = 'ok' | 'not_running' | 'failed';

async function pushConfigToBackend(): Promise<PushResult> {
  if (!pythonBackend.isRunning()) return 'not_running';
  const state = store.get();
  try {
    await pythonBackend.request('setConfig', { config: state.config });
    return 'ok';
  } catch (e) {
    return 'failed';
  }
}

export function registerIpc(): void {
  ipcMain.handle('app:version', () => app.getVersion());
  ipcMain.handle('app:openExternal', async (_e, url: string) => {
    if (typeof url === 'string' && /^(https?|mailto):/i.test(url)) {
      await shell.openExternal(url);
    }
  });
  ipcMain.handle('app:showItemInFolder', async (_e, p: string) => {
    shell.showItemInFolder(p);
  });
  ipcMain.handle('app:getUserDataPath', () => app.getPath('userData'));

  ipcMain.handle('state:get', () => store.get());
  ipcMain.handle('state:setStartMinimized', (_e, v: boolean) => {
    const next = store.save({ ...store.get(), startMinimized: !!v });
    broadcastState(next);
    return ok();
  });
  ipcMain.handle('state:setStartWithWindows', (_e, v: boolean) => {
    setStartWithWindows(!!v);
    const next = store.save({ ...store.get(), startWithWindows: !!v });
    broadcastState(next);
    return ok();
  });
  ipcMain.handle('state:setEnableDiscordRpc', async () => {
    return ok();
  });
  ipcMain.handle('state:acceptDisclaimer', () => {
    const next = store.save({ ...store.get(), acceptedDisclaimer: true });
    broadcastState(next);
    return ok();
  });

  ipcMain.handle('config:get', () => store.get().config);
  ipcMain.handle('config:update', async (_e, patch: Partial<TraderConfig>) => {
    store.patchConfig(patch);
    clearActiveMarkersForPatch(patch);
    const next = store.get();
    broadcastState(next);
    await pushConfigToBackend();
    return next.config;
  });
  ipcMain.handle('config:replace', async (_e, cfg: TraderConfig) => {
    store.replaceConfig(cfg);
    // A full replace no longer matches any saved strategy/profile snapshot.
    const next = store.save({
      ...store.get(), activeProfileId: null, activeCrypto15mProfileId: null,
    });
    broadcastState(next);
    await pushConfigToBackend();
    return next.config;
  });
  ipcMain.handle('config:reset', async () => {
    const next = store.resetConfig();
    broadcastState(next);
    await pushConfigToBackend();
    return next.config;
  });
  ipcMain.handle('config:listStrategies', () => listStrategies());
  ipcMain.handle('config:applyStrategy', async (_e, id: string) => {
    const s = findStrategy(id);
    if (!s || s.comingSoon) return store.get().config;
    // Applying a main-engine strategy must NOT change the environment, master
    // kill-switch, the independently-tuned 15m crypto engine, or the user's
    // personal settings (mirrors the profiles:apply strategy branch).
    const next = applyStrategyPreset(s);
    const stateNext = store.save({ ...store.get(), activeProfileId: id });
    broadcastState(stateNext);
    await pushConfigToBackend();
    return next.config;
  });

  ipcMain.handle('profiles:list', () => store.get().customProfiles);
  ipcMain.handle('profiles:save', (_e, name: string, description?: string, kind?: ProfileKind) => {
    if (!name?.trim()) return err('Profile name required');
    const cur = store.get();
    const now = new Date().toISOString();
    const pkind: ProfileKind = kind === 'crypto15m' ? 'crypto15m' : 'main';
    const profile: Profile = {
      id: genId(),
      name: name.trim(),
      description: description?.trim() || undefined,
      kind: pkind,
      createdAt: now,
      updatedAt: now,
      config: { ...cur.config },
    };
    const next = store.save({
      ...cur,
      customProfiles: [...cur.customProfiles, profile],
      ...(pkind === 'crypto15m'
        ? { activeCrypto15mProfileId: profile.id }
        : { activeProfileId: profile.id }),
    });
    broadcastState(next);
    return ok(profile, `Saved ${pkind === 'crypto15m' ? '15m crypto ' : ''}profile "${profile.name}"`);
  });
  ipcMain.handle('profiles:apply', async (_e, id: string) => {
    const cur = store.get();
    const p = cur.customProfiles.find((x) => x.id === id);
    if (!p) {
      const s = findStrategy(id);
      if (s?.comingSoon) return err(`"${s.name}" is coming soon`);
      if (s) {
        // Strategy presets are main-engine. Reset the main config to the
        // strategy, but preserve the current 15m crypto settings, env/switch,
        // and the user's personal settings (STRATEGY_PRESERVE).
        const next = applyStrategyPreset(s);
        const stateNext = store.save({ ...store.get(), activeProfileId: id });
        broadcastState(stateNext);
        await pushConfigToBackend();
        return ok(next.config, `Applied "${s.name}"`);
      }
      return err('Profile not found');
    }
    const pkind = profileKindOf(p);
    // Patch only this engine's slice so the other engine is left untouched.
    const slice = profileSlice(p.config, pkind);
    if (pkind === 'main') {
      // Custom profiles never carry the preset-only per-source category
      // filters (see PRESET_ONLY) — clear any hidden filter a previously
      // applied built-in preset left behind so the config matches the UI.
      slice.allowedWhaleCategories = null;
      slice.allowedMomentumCategories = null;
    }
    const next = store.patchConfig(slice);
    const stateNext = store.save({
      ...store.get(),
      ...(pkind === 'crypto15m'
        ? { activeCrypto15mProfileId: id }
        : { activeProfileId: id }),
    });
    broadcastState(stateNext);
    await pushConfigToBackend();
    return ok(next.config, `Applied profile "${p.name}"`);
  });
  ipcMain.handle('profiles:rename', (_e, id: string, name: string) => {
    if (!name?.trim()) return err('Name required');
    const cur = store.get();
    const idx = cur.customProfiles.findIndex((p) => p.id === id);
    if (idx < 0) return err('Profile not found');
    const updated = [...cur.customProfiles];
    updated[idx] = { ...updated[idx], name: name.trim(), updatedAt: new Date().toISOString() };
    const next = store.save({ ...cur, customProfiles: updated });
    broadcastState(next);
    return ok();
  });
  ipcMain.handle('profiles:update', (_e, id: string) => {
    const cur = store.get();
    const idx = cur.customProfiles.findIndex((p) => p.id === id);
    if (idx < 0) return err('Profile not found');
    const updated = [...cur.customProfiles];
    updated[idx] = {
      ...updated[idx],
      config: { ...cur.config },
      updatedAt: new Date().toISOString(),
    };
    const next = store.save({ ...cur, customProfiles: updated });
    broadcastState(next);
    return ok(updated[idx]);
  });
  ipcMain.handle('profiles:delete', (_e, id: string) => {
    const cur = store.get();
    const next = store.save({
      ...cur,
      customProfiles: cur.customProfiles.filter((p) => p.id !== id),
      activeProfileId: cur.activeProfileId === id ? null : cur.activeProfileId,
      activeCrypto15mProfileId:
        cur.activeCrypto15mProfileId === id ? null : cur.activeCrypto15mProfileId,
    });
    broadcastState(next);
    return ok();
  });
  ipcMain.handle('profiles:duplicate', (_e, id: string) => {
    const cur = store.get();
    const orig = cur.customProfiles.find((p) => p.id === id);
    if (!orig) return err('Profile not found');
    const dup: Profile = {
      ...orig,
      id: genId(),
      name: `${orig.name} (copy)`,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    };
    const next = store.save({
      ...cur,
      customProfiles: [...cur.customProfiles, dup],
    });
    broadcastState(next);
    return ok(dup);
  });
  ipcMain.handle('profiles:export', (_e, id: string) => {
    const cur = store.get();
    const p = cur.customProfiles.find((x) => x.id === id);
    if (!p) return err('Profile not found');
    const json = JSON.stringify(
      { kryptTraderProfile: 1, profile: p },
      null,
      2,
    );
    return ok(json);
  });
  ipcMain.handle('profiles:import', (_e, json: string) => {
    try {
      const parsed = JSON.parse(json);
      if (!parsed?.profile?.config || typeof parsed.profile.config !== 'object') {
        return err('Not a Krypt Trader profile');
      }
      const cur = store.get();
      const p = parsed.profile as Profile;
      const now = new Date().toISOString();
      const dup: Profile = {
        id: genId(),
        name: typeof p.name === 'string' && p.name.trim() ? p.name.trim() : 'Imported profile',
        description: typeof p.description === 'string' ? p.description : undefined,
        kind: p.kind === 'crypto15m' ? 'crypto15m' : 'main',
        createdAt: now,
        updatedAt: now,
        // Coerce/validate the untrusted config, then normalize it over the
        // defaults — the same shape a profile loaded from disk ends up with.
        config: { ...store.DEFAULT_CONFIG, ...sanitizeImportedConfig(p.config) },
      };
      const next = store.save({ ...cur, customProfiles: [...cur.customProfiles, dup] });
      broadcastState(next);
      return ok(dup, `Imported "${dup.name}"`);
    } catch (e: any) {
      return err(`Invalid profile JSON: ${e?.message || e}`);
    }
  });

  const emptyAllCreds = () => ({
    current: 'demo' as const,
    demo: { env: 'demo' as const, hasApiKey: false, hasRsaKey: false, apiKeyPreview: '', fingerprint: '' },
    production: { env: 'production' as const, hasApiKey: false, hasRsaKey: false, apiKeyPreview: '', fingerprint: '' },
  });
  ipcMain.handle('credentials:status', async () => {
    if (!pythonBackend.isRunning()) {
      return {
        hasApiKey: false,
        hasRsaKey: false,
        apiKeyPreview: '',
        fingerprint: '',
      } satisfies CredentialsState;
    }
    const all = await pythonBackend.request('credentialStatus', {}) as any;
    if (all && all.current && all[all.current]) {
      return all[all.current] as CredentialsState;
    }
    return all as CredentialsState;
  });
  ipcMain.handle('credentials:statusAll', async () => {
    if (!pythonBackend.isRunning()) return emptyAllCreds();
    return await pythonBackend.request('credentialStatus', {});
  });
  ipcMain.handle('credentials:save', async (_e, input: CredentialsInput) => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      await pythonBackend.request('setCredentials', input);
      return ok();
    } catch (e: any) {
      return err(`${e?.message || e}`);
    }
  });
  ipcMain.handle('credentials:test', async (_e, env?: string) => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      const data = await pythonBackend.request('testCredentials', env ? { env } : {});
      return ok(data, 'Connected to Kalshi');
    } catch (e: any) {
      return err(`${e?.message || e}`);
    }
  });
  ipcMain.handle('credentials:clear', async (_e, env?: string) => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      await pythonBackend.request('clearCredentials', env ? { env } : {});
      return ok();
    } catch (e: any) {
      return err(`${e?.message || e}`);
    }
  });

  ipcMain.handle('backend:info', () => pythonBackend.info());
  ipcMain.handle('backend:start', async () => {
    await pythonBackend.start();
    return ok();
  });
  ipcMain.handle('backend:stop', async () => {
    await pythonBackend.stop();
    return ok();
  });
  ipcMain.handle('backend:restart', async () => {
    await pythonBackend.restart();
    return ok();
  });
  ipcMain.handle('backend:runOnce', async (_e, action: string) => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      const data = await pythonBackend.request('runOnce', { action });
      return ok(data, (data as any)?.summary || 'Done');
    } catch (e: any) {
      return err(`${e?.message || e}`);
    }
  });

  ipcMain.handle('trading:setEnabled', async (_e, enabled: boolean) => {
    const next = store.patchConfig({ enableTrading: !!enabled });
    broadcastState(next);
    // Don't report success the engine never confirmed: a wedged backend can
    // keep trading on the OLD setting while every UI surface shows the new one.
    const push = await pushConfigToBackend();
    if (push === 'failed') {
      return err(
        `Setting saved, but the backend did not confirm it — the engine may still be ${enabled ? 'stopped' : 'trading'}. Restart the backend to re-sync.`,
      );
    }
    if (push === 'not_running') {
      return ok(undefined, 'Backend not running — setting saved and will apply when it starts');
    }
    return ok();
  });
  ipcMain.handle('trading:cancelAllOpen', async () => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      const data = await pythonBackend.request('cancelAllOpen', {});
      return ok(data, `Canceled ${data.canceled} order(s)`);
    } catch (e: any) {
      return err(`${e?.message || e}`);
    }
  });
  ipcMain.handle('trading:flatten', async () => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      const data = await pythonBackend.request('flatten', {});
      return ok(data, `Flattened ${data.closed} order(s)`);
    } catch (e: any) {
      return err(`${e?.message || e}`);
    }
  });

  ipcMain.handle('app:factoryReset', async () => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      const data = await pythonBackend.request('factoryReset', {}) as any;
      const total = Object.values(data?.deleted || {}).reduce(
        (a: number, b: any) => a + (Number(b) || 0), 0,
      );
      return ok(data, `Cleared ${total} row(s)`);
    } catch (e: any) {
      return err(`${e?.message || e}`);
    }
  });

  ipcMain.handle('data:account', async () => {
    if (!pythonBackend.isRunning()) {
      return {
        cashUsd: 0, portfolioUsd: 0, totalUsd: 0,
        startBankrollUsd: store.get().config.startBankrollUsd,
        roiPct: 0, realizedPnlUsd: 0, unrealizedPnlUsd: 0,
        openCostUsd: 0, feesUsd: 0, wins: 0, losses: 0, winRate: 0,
        pendingCount: 0, openCount: 0, resolvedCount: 0, totalOpened: 0,
        byEnv: {
          demo: { wins: 0, losses: 0, realizedPnl: 0 },
          production: { wins: 0, losses: 0, realizedPnl: 0 },
        },
      };
    }
    return await pythonBackend.request('account', {});
  });
  ipcMain.handle('data:pnlSeries', async (_e, sinceHours?: number) => {
    if (!pythonBackend.isRunning()) return [];
    return await pythonBackend.request('pnlSeries', { sinceHours });
  });
  ipcMain.handle('data:positions', async (_e, filter?: PositionFilter) => {
    if (!pythonBackend.isRunning()) return [];
    return await pythonBackend.request('positions', filter || {});
  });
  ipcMain.handle('data:signals', async (_e, filter?: SignalFilter) => {
    if (!pythonBackend.isRunning()) return [];
    return await pythonBackend.request('signals', filter || {});
  });
  ipcMain.handle('data:scannerStats', async () => {
    if (!pythonBackend.isRunning()) {
      return {
        whales: { total: 0, sent: 0, resolved: 0, winRate: 0 },
        momentum: { total: 0, sent: 0, resolved: 0, winRate: 0 },
        marketsTracked: 0,
        lastWhaleScanAt: null,
        lastMomentumScanAt: null,
        lastTradeScanAt: null,
      };
    }
    return await pythonBackend.request('scannerStats', {});
  });
  ipcMain.handle('data:botRuns', async (_e, env?: string | null, limit?: number) => {
    if (!pythonBackend.isRunning()) {
      return { runs: [], activeRunId: 0, activeRun: null };
    }
    const params: Record<string, unknown> = {};
    if (env) params.env = env;
    if (limit) params.limit = limit;
    return await pythonBackend.request('botRuns', params);
  });

  ipcMain.handle('crypto15m:snapshot', async () => {
    if (!pythonBackend.isRunning()) {
      return {
        fetchedAt: new Date().toISOString(),
        spotOk: false,
        spotSource: 'unavailable',
        constants: {
          timeDelayMin: 8, entryThreshold: 0.95, entryMax: 0.98,
          exitThreshold: 0.4, minDeltaPct: 0, entryDiff: 0.02, directionMode: 'favorite',
          entryStyle: 'maker', hoursStartUtc: 0, hoursEndUtc: 24,
        },
        assets: [],
      };
    }
    return await pythonBackend.request('crypto15m', {});
  });
  ipcMain.handle('crypto15m:status', async () => {
    if (!pythonBackend.isRunning()) {
      // Fall back to the configured values, not literals — the engine default
      // for maxConcurrent is 3 (a stale "7" here overstated the real cap).
      const cfg = store.get().config;
      return {
        enabled: false, live: false, liveArmed: false, authed: false,
        orderSize: cfg.crypto15mOrderSize ?? 1,
        maxConcurrent: cfg.crypto15mMaxConcurrent ?? 3,
        env: 'demo',
        sizing: {
          mode: 'fixed', balancePct: 0.02, maxLossPct: 0, balanceUsd: 0,
          estPriceCents: 0, estContracts: 1, estCostUsd: 0, note: '',
        },
        stats: { openCount: 0, wins: 0, losses: 0, realizedPnlUsd: 0, total: 0 },
        open: [], recent: [],
      };
    }
    return await pythonBackend.request('crypto15mStatus', {});
  });
  ipcMain.handle('crypto15m:backtest', async (_e, args?: { sinceDays?: number }) => {
    if (!pythonBackend.isRunning()) return null;
    return await pythonBackend.request('c15Backtest', args || {});
  });
  ipcMain.handle('main:backtest', async (_e, args?: { sinceDays?: number; config?: Record<string, unknown> }) => {
    if (!pythonBackend.isRunning()) return null;
    return await pythonBackend.request('mainBacktest', args || {});
  });
  ipcMain.handle('crypto15m:history', async (_e, args?: { limit?: number }) => {
    if (!pythonBackend.isRunning()) return null;
    return await pythonBackend.request('c15History', args || {});
  });
  ipcMain.handle('backtest:export', async () => {
    if (!pythonBackend.isRunning()) return null;
    const r = await pythonBackend.request('exportResearch', {}) as { dir?: string } | null;
    if (r?.dir) shell.showItemInFolder(r.dir);
    return r;
  });
  ipcMain.handle('backtest:collection', async () => {
    if (!pythonBackend.isRunning()) return null;
    return await pythonBackend.request('collectionStats', {});
  });
  ipcMain.handle('perps:status', async () => {
    if (!pythonBackend.isRunning()) return null;
    try {
      return await pythonBackend.request('perpsStatus', {});
    } catch {
      return null;
    }
  });
  ipcMain.handle('perps:backfill', async () => {
    if (!pythonBackend.isRunning()) return null;
    return await pythonBackend.request('perpsBackfill', {});
  });
  ipcMain.handle('perps:farmFlatten', async () => {
    if (!pythonBackend.isRunning()) return null;
    return await pythonBackend.request('perpsFarmFlatten', {});
  });
  ipcMain.handle('perps:backtest', async (_e, args?: { sinceDays?: number }) => {
    if (!pythonBackend.isRunning()) return null;
    return await pythonBackend.request('perpsBacktest', args || {});
  });
  ipcMain.handle('perps:history', async (_e, args?: { limit?: number }) => {
    if (!pythonBackend.isRunning()) return null;
    return await pythonBackend.request('perpsHistory', args || {});
  });
  ipcMain.handle('perps:stratFlatten', async () => {
    if (!pythonBackend.isRunning()) return null;
    return await pythonBackend.request('perpsStratFlatten', {});
  });
  ipcMain.handle('trading:status', async () => {
    if (!pythonBackend.isRunning()) return null;
    try {
      return await pythonBackend.request('tradingStatus', {});
    } catch {
      return null;
    }
  });
  ipcMain.handle(
    'kalshi:marketUrl',
    async (_e, args?: { eventTicker?: string; ticker?: string; env?: string }) => {
      if (!pythonBackend.isRunning()) return { url: '' };
      return await pythonBackend.request('kalshiMarketUrl', args || {});
    },
  );
  ipcMain.handle('logs:tail', async (_e, _limit?: number) => {
    return logsBuffer.slice(-1 * (_limit || 500));
  });
  ipcMain.handle('logs:clear', () => {
    logsBuffer.length = 0;
    return ok();
  });
  ipcMain.handle('logs:openFolder', async () => {
    const p = join(app.getPath('userData'), 'logs');
    if (existsSync(p)) shell.openPath(p);
  });

  ipcMain.on('window:minimize', (e) => {
    BrowserWindow.fromWebContents(e.sender)?.minimize();
  });
  ipcMain.on('window:maximize', (e) => {
    const w = BrowserWindow.fromWebContents(e.sender);
    if (!w) return;
    if (w.isMaximized()) w.unmaximize();
    else w.maximize();
  });
  ipcMain.on('window:close', (e) => {
    BrowserWindow.fromWebContents(e.sender)?.close();
  });
  ipcMain.handle('window:isMaximized', (e) => {
    return BrowserWindow.fromWebContents(e.sender)?.isMaximized() ?? false;
  });
}

const MAX_LOGS = 5000;
export const logsBuffer: any[] = [];

export function appendLog(entry: any): void {
  logsBuffer.push(entry);
  if (logsBuffer.length > MAX_LOGS) logsBuffer.shift();
}

export { broadcastState };
