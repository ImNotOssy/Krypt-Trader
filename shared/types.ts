
export type KalshiEnv = 'demo' | 'production';

export type OrderStyle = 'limit_cross' | 'limit_mid' | 'market';

export type SignalSource = 'whale' | 'momentum' | 'convergence' | 'external';

/** One composed entry condition in the rule builder: `<field> <op> <value>`.
 *  field = a crypto snapshot key (favoritePrice, macdHist, rsi, minsLeft, …). */
export interface RuleCondition {
  field: string;
  op: '>=' | '<=' | '>' | '<';
  value: number;
}


export interface TraderConfig {
  kalshiEnv: KalshiEnv;
  enableTrading: boolean;

  tradeWhales: boolean;
  tradeMomentum: boolean;
  tradeConvergence: boolean;

  minEdgePtsWhale: number;
  minEdgePtsMomentum: number;
  minConfidenceWhale: number;
  minConfidenceMomentum: number;
  feeAwareEdge?: boolean;          // subtract the Kalshi taker fee from edge before the min-edge gates
  maxEntrySlippageCents?: number;  // reject entries priced more than N¢ past the signal price; 0 = off
  minMarketVolume?: number;        // skip signals on markets with lifetime volume below this; 0 = off
  maxTradeAgeMin?: number;         // only count tape trades younger than N minutes (whales + clusters)
  minEntryPriceCents: number;
  maxEntryPriceCents: number;
  maxResolutionDays?: number; // skip markets resolving more than N days out; 0 = off
  allowedMomentumSignalTypes: string[];
  allowedCategories: string[] | null;
  allowedWhaleCategories: string[] | null;
  allowedMomentumCategories: string[] | null;
  contrarianOnly: boolean;

  /** "Secret Strategy" gambling mode: ignore all gates, trade each fresh signal at gamblingTradeProbability. */
  gamblingMode: boolean;
  gamblingTradeProbability: number;

  sizingMode: 'percent' | 'fixed';
  fixedTradeUsd: number;
  baseSizeFraction: number;
  minSizeFraction: number;
  maxSizeFraction: number;
  sizingBaseEdge: number;
  sizingMaxEdge: number;
  hardMaxPositionUsd: number;
  minCashReserveFraction: number;

  orderStyle: OrderStyle;
  crossSpreadFallbackOffset: number;
  orderExpirationSec: number | null;

  maxOpenPositions: number;
  maxPositionsPerEvent: number;
  maxDailyNewPositions: number;
  unlimitedDailyNewPositions: boolean;
  maxTotalExposureFraction: number;

  tradeScanInterval: number;
  positionPollInterval: number;
  balancePollInterval: number;
  resolutionCheckInterval: number;
  whaleScanInterval: number;
  momentumScanInterval: number;
  marketRefreshInterval: number;

  maxSignalAgeSec: number;

  startBankrollUsd: number;
  stopLossOnDay: number;
  stopLossOnDayPct?: number; // daily stop as a fraction of the day-start total (0..1); tighter of the two limits binds; 0 = off
  takeProfitOnDay: number;

  tradingHoursEnabled: boolean;
  tradingHoursStart: string;
  tradingHoursEnd: string;
  tradingDays: string[];
  tradingTimezoneOffsetMin: number;

  minWhaleUsd: number;
  minEntryPriceFrac: number;

  eventWebhookUrl: string;
  statsWebhookUrl: string;
  whaleWebhookUrl: string;
  momentumWebhookUrl: string;
  statsPushInterval: number;
  statsChartWindowHours: number;
  enableDiscord: boolean;

  crypto15mEnabled?: boolean;
  crypto15mLive?: boolean;
  crypto15mSizingMode?: 'fixed' | 'balance_pct';
  crypto15mOrderSize?: number;
  crypto15mBalancePct?: number;
  crypto15mMaxLossPct?: number;
  crypto15mMaxTotalPct?: number;             // aggregate cap: total committed 15m cost ≤ this fraction of bankroll; 0 = off
  crypto15mMaxConcurrent?: number;
  crypto15mAssets?: string[] | null;        // which assets the executor may enter (null = all)
  crypto15mDirectionMode?: 'favorite' | 'contrarian' | 'model';
  crypto15mModelMinProb?: number;                // sniper: min model probability for the bought side (0.5–1)
  crypto15mModelMinEdgeCents?: number;           // sniper: min fee-adjusted edge vs the ask (¢)
  crypto15mModelFinalMinute?: boolean;           // sniper: allow final-60s entries with ≥30 settlement prints + 3σ certainty
  crypto15mModelAutopause?: boolean;             // sniper: auto-pause entries when rolling calibration drops below break-even
  crypto15mTimeDelayMin?: number;
  crypto15mEntryThreshold?: number;
  crypto15mStrictThreshold?: boolean;            // hard floor: the price actually paid must be ≥ entryThreshold (two-sided book required)
  crypto15mEntryMax?: number;
  crypto15mExitThreshold?: number;
  crypto15mStopSlippageCents?: number;
  crypto15mTakeProfitCents?: number;             // per-bet: sell when the held side reaches this price (¢); 0 = off
  crypto15mStopLossPct?: number;                 // per-bet: sell when down >= this fraction of entry cost (0..1); 0 = off
  crypto15mSessionTakeProfitUsd?: number;        // halt new 15m entries once this session's realized 15m P&L hits $this; 0 = off
  crypto15mMinRsi?: number;                      // direction-aware RSI confirmation (0–100); 0 = off
  crypto15mMinMacdHist?: number;                 // direction-aware MACD-histogram confirmation (magnitude); 0 = off
  crypto15mMinDeltaPct?: number;
  crypto15mEntryDiff?: number;
  crypto15mEntryStyle?: 'maker' | 'taker';
  crypto15mMakerCancelMin?: number;
  crypto15mHoursStartUtc?: number;
  crypto15mHoursEndUtc?: number;
  crypto15mHours?: number[] | null;         // explicit UTC hours (0-23) to trade; null = use the window above
  crypto15mRecordSignals?: boolean;
  mainRecordSignals?: boolean;             // record whale/momentum signals while the app runs (forced on when trading is enabled)
  // Perpetual futures (Kalshi margin API) — passive market-data recorder.
  // Public REST always reads production (unauthenticated) so research data
  // flows in any env; the WS accelerator follows the active env's creds.
  perpsRecordSignals?: boolean;
  perpsWsEnabled?: boolean;
  perpsSymbols?: string[];                 // prod tickers (KXBTCPERP…); demo '1' suffix handled internally
  // Perps volume farmer — maker-only two-sided quoting for the in-app volume
  // rewards. Loss-budgeted volume engine, auto-halts when measured cost per
  // $ of volume exceeds perpsFarmMaxCostBps or the daily loss cap.
  perpsFarmEnabled?: boolean;
  perpsFarmSymbol?: string;
  perpsFarmClipContracts?: number;
  perpsFarmMaxInventoryContracts?: number;
  perpsFarmDailyLossUsd?: number;
  perpsFarmDailyVolumeUsd?: number;        // 0 = no daily volume target
  perpsFarmMaxCostBps?: number;
  perpsFarmMaxFeeBps?: number;             // pre-trade gate: only farm while the charged maker fee ≤ this (0 = off)
  // Perps user strategy (rule-composed like the 15m builder; same gates run
  // in backtest, paper and live). perpsStratEnabled = paper trading;
  // perpsStratLive = REAL leveraged orders (explicit risk-ack modal in UI).
  perpsStratEnabled?: boolean;
  perpsStratLive?: boolean;
  perpsStratSymbol?: string;
  perpsStratDirection?: 'long' | 'short';
  perpsStratRules?: RuleCondition[];
  perpsStratEntryStyle?: 'taker' | 'maker';
  perpsStratContracts?: number;
  perpsStratLeverage?: number;             // hard-capped 5x
  perpsStratTpBps?: number;
  perpsStratSlBps?: number;
  perpsStratMaxHoldMin?: number;
  perpsStratExitOnRulesFail?: boolean;
  perpsStratDailyLossUsd?: number;
  perpsStratMaxNotionalUsd?: number;
  perpsStratFeeEra?: 'today' | 'jul8';     // backtest fee scenario
  crypto15mIndicatorDetect?: boolean;            // compute underlying MACD/RSI (rule fields, detection-only)
  crypto15mSpotWs?: boolean;                     // Coinbase WS spot feed (BRTI proxy) + final-minute settlement tracker
  crypto15mArbDetect?: boolean;                  // detect Up+Down ≠ $1 arbitrage
  crypto15mArbMinEdgeCents?: number;             // minimum edge (cents) to flag an arb
  crypto15mUseRules?: boolean;                   // use the composed rule-set as the entry gate
  crypto15mRules?: RuleCondition[];              // composed entry conditions (all AND-ed)
  // Pairs — temporal complement accumulation (buy YES on dips + NO on peaks;
  // matched pairs settle at exactly $1, so blended cost < ceiling = locked profit)
  crypto15mPairsEnabled?: boolean;
  crypto15mDirectionalEnabled?: boolean;         // favorite/contrarian engine; false = pairs-only mode
  crypto15mPairsCeilingCents?: number;           // max blended YES+NO cost per pair (¢)
  crypto15mPairsDipCents?: number;               // a leg buys only this far below its rolling median (¢)
  crypto15mPairsClip?: number;                   // contracts per leg
  crypto15mPairsFirstLegMinCents?: number;       // never start a pair below this ask (strong favorite = trend, not seesaw)
  crypto15mPairsFirstLegMaxCents?: number;       // never start a pair above this ask (¢)
}

export interface CredentialsState {
  env?: 'demo' | 'production';
  hasApiKey: boolean;
  hasRsaKey: boolean;
  apiKeyPreview: string;
  fingerprint: string;
}

export interface CredentialsStatusAll {
  current: 'demo' | 'production';
  demo: CredentialsState;
  production: CredentialsState;
}

export interface CredentialsInput {
  apiKey: string;
  rsaPem: string;
  env?: 'demo' | 'production';
}


/** Which engine a profile configures. Legacy profiles (no kind) are 'main'. */
export type ProfileKind = 'main' | 'crypto15m';

export interface Profile {
  id: string;
  name: string;
  description?: string;
  kind?: ProfileKind;
  createdAt: string;
  updatedAt: string;
  config: TraderConfig;
  builtin?: boolean;
}


export interface AppState {
  config: TraderConfig;
  activeProfileId: string | null;
  activeCrypto15mProfileId: string | null;
  customProfiles: Profile[];
  startMinimized: boolean;
  startWithWindows: boolean;
  enableDiscordRpc: boolean;
  acceptedDisclaimer: boolean;
  windowBounds: { x: number; y: number; width: number; height: number } | null;
}


export type BackendStatus =
  | 'stopped'
  | 'starting'
  | 'running'
  | 'restarting'
  | 'crashed';

export interface BackendInfo {
  status: BackendStatus;
  pid: number | null;
  startedAt: string | null;
  lastError: string | null;
  pythonOk: boolean;
  authOk: boolean;
}

export interface AccountSnapshot {
  cashUsd: number;
  portfolioUsd: number;
  totalUsd: number;
  balanceSyncing?: boolean;  // an order just filled or settled — ledgers catching up; totals may transiently dip
  startBankrollUsd: number;
  bankrollSource?: 'user' | 'auto' | 'live';
  roiPct: number;
  realizedPnlUsd: number;
  todayPnlUsd?: number;
  alltimePnlUsd?: number;
  todayBaselineUsd?: number | null;
  alltimeBaselineUsd?: number | null;
  todayWins?: number;
  todayLosses?: number;
  unrealizedPnlUsd: number;
  openCostUsd: number;
  feesUsd: number;
  wins: number;
  losses: number;
  winRate: number;
  pendingCount: number;
  openCount: number;
  resolvedCount: number;
  totalOpened: number;
  byEnv: { demo: AccountByEnv; production: AccountByEnv };
  sessionPnlUsd?: number;
  sessionRoiPct?: number;
  sessionBaselineUsd?: number;
  sessionStartedAt?: string;
  sessionRunId?: number;
}

export interface BotRun {
  id: number;
  kalshiEnv: KalshiEnv;
  startedAt: string;
  endedAt: string | null;
  startCashUsd: number;
  startPortfolioUsd: number;
  startTotalUsd: number;
  endCashUsd: number | null;
  endPortfolioUsd: number | null;
  endTotalUsd: number | null;
  pnlUsd: number;
  tradesOpened: number;
  tradesWon: number;
  tradesLost: number;
  isActive: boolean;
}

export interface BotRunsResponse {
  runs: BotRun[];
  activeRunId: number;
  activeRun: BotRun | null;
}

export interface AccountByEnv {
  wins: number;
  losses: number;
  realizedPnl: number;
}

export interface PnlPoint {
  at: string;
  cashUsd: number;
  portfolioUsd: number;
  totalUsd: number;
  realizedPnlUsd: number;
  openPositions: number;
}

export interface BotPosition {
  id: number;
  signalSource: SignalSource;
  signalId: number;
  ticker: string;
  eventTicker: string;
  title: string;
  category: string;
  direction: 'yes' | 'no';
  action: 'buy' | 'sell';
  targetContracts: number;
  limitPriceCents: number;
  filledContracts: number;
  avgFillPriceCents: number | null;
  costUsd: number;
  feesUsd: number;
  clientOrderId: string;
  kalshiOrderId: string | null;
  status:
    | 'submitted'
    | 'partial'
    | 'filled'
    | 'canceled'
    | 'expired'
    | 'gone'
    | 'error'
    | 'dry_run';
  confidence: number;
  edgePts: number;
  signalPriceCents: number;
  resolved: boolean;
  outcomeCorrect: number | null;
  settlementUsd: number | null;
  pnlUsd: number | null;
  /** Current price of the held side, in cents (live mark). Null until marked. */
  markPriceCents: number | null;
  /** Unrealized mark-to-market P&L for an open filled position; null when resolved/unmarked. */
  livePnlUsd: number | null;
  balanceBeforeUsd: number | null;
  kalshiEnv: KalshiEnv;
  createdAt: string;
  lastUpdated: string;
  resolvedAt: string | null;
  error: string | null;
}

export interface SignalRow {
  id: number;
  source: SignalSource;
  ticker: string;
  eventTicker: string;
  title: string;
  category: string;
  direction: 'yes' | 'no';
  priceCents: number;
  confidence: number;
  edgePts: number;
  signalType?: string;
  dollarValue?: number;
  createdAt: string;
  resolved: boolean;
  outcomeCorrect: number | null;
  pnlEstimate: number | null;
  traded: boolean;
}

export interface ScannerStats {
  whales: { total: number; sent: number; resolved: number; winRate: number };
  momentum: { total: number; sent: number; resolved: number; winRate: number };
  marketsTracked: number;
  lastWhaleScanAt: string | null;
  lastMomentumScanAt: string | null;
  lastTradeScanAt: string | null;
}

export interface LogEntry {
  ts: string;
  level: 'DEBUG' | 'INFO' | 'WARN' | 'ERROR' | 'CRITICAL';
  source: 'main' | 'backend' | 'trader' | 'whale' | 'momentum' | 'discord';
  msg: string;
}

export interface ActionResult<T = void> {
  ok: boolean;
  message?: string;
  data?: T;
}


export interface StrategyPreset {
  id: string;
  name: string;
  tagline: string;
  description: string;
  riskLabel: 'safe' | 'balanced' | 'aggressive' | 'experimental';
  badge?: 'recommended' | 'new' | 'soon' | null;
  comingSoon?: boolean;
  /** Hidden "Secret Strategy" — shown via the rainbow button, not the ranked grid. */
  secret?: boolean;
  backtest?: {
    netCents: number;
    t: number;
    n: number;
    approx?: boolean;
  } | null;
  config: TraderConfig;
}


export interface Crypto15mConstants {
  timeDelayMin: number;
  entryThreshold: number;
  exitThreshold: number;
  entryMax: number;
  minDeltaPct?: number;
  entryDiff: number;
  directionMode?: 'favorite' | 'contrarian';
  entryStyle?: 'maker' | 'taker';
  hoursStartUtc?: number;
  hoursEndUtc?: number;
  indicatorDetect?: boolean;
  arbDetect?: boolean;
  useRules?: boolean;
}

export interface Crypto15mAsset {
  asset: string;
  series: string;
  spotUsd: number | null;
  open15mUsd: number | null;
  deltaUsd: number | null;
  deltaPct?: number | null;
  hasMarket: boolean;
  ticker: string | null;
  closeTime: string | null;
  minsLeft: number | null;
  upProb: number | null;
  downProb: number | null;
  favorite: 'up' | 'down' | null;
  favoritePrice: number | null;
  entryCost: number | null;
  yesBid?: number | null;
  yesAsk?: number | null;
  inWindow: boolean;
  signal: boolean;
  openMarketCount: number;
  error: string | null;
  // timing + cross-asset correlation (optional rule-builder fields)
  hourUtc?: number | null;       // current UTC hour 0-23
  peersAgree?: number | null;    // 0..1 — fraction of other coins favoring the same side
  marketBias?: number | null;    // -1..1 — market-wide up/down lean (breadth)
  // Up+Down ≠ $1 arbitrage (market-neutral edge, detection-only)
  upAsk?: number | null;         // best ask to BUY the up/yes side (0..1)
  downAsk?: number | null;       // best ask to BUY the down/no side (0..1)
  arbEdgeCents?: number | null;  // 100 − (upAsk+downAsk)*100; > 0 = buyable arb (gross of fees)
  arbSignal?: boolean;           // arbEdgeCents >= the configured minimum
  // underlying technical indicators (MACD/RSI on the 1-min underlying; rule fields)
  macd?: number | null;          // MACD line
  macdSignal?: number | null;    // MACD signal line
  macdHist?: number | null;      // MACD histogram = macd − signal
  macdCross?: number | null;     // +1 bullish / −1 bearish / 0 no cross on this bar
  rsi?: number | null;           // Wilder RSI(14), 0..100
  // spot-vs-strike settlement model (detection-only rule fields)
  strikeUsd?: number | null;       // Kalshi strike (fallback: tracked window open)
  deltaSignedPct?: number | null;  // (spot − strike)/strike; + = above strike
  sigma1m?: number | null;         // realized 1-min return vol (fraction/√min)
  modelProb?: number | null;       // model P(up) under Kalshi's 60s-average settlement rule
  edgeNetCents?: number | null;    // best fee-adjusted model edge on either side (¢)
  settlePrints?: number;           // final-minute settlement prints already observed (0 outside it)
}

export interface Crypto15mSnapshot {
  fetchedAt: string;
  spotOk: boolean;
  spotSource: string;
  hoursOk?: boolean;
  constants: Crypto15mConstants;
  assets: Crypto15mAsset[];
}

export type Crypto15mStatusName =
  | 'dry_run' | 'submitted' | 'filled' | 'exiting'
  | 'exited' | 'settled' | 'canceled' | 'error';

export interface Crypto15mPosition {
  id: number;
  asset: string;
  series: string;
  ticker: string;
  side: 'up' | 'down' | '';
  direction: 'yes' | 'no' | '';
  strategy?: string;               // '' = directional; 'pair' = complement-accumulation leg
  targetContracts: number;
  filledContracts: number;
  entryLimitCents: number;
  avgEntryCents: number | null;
  costUsd: number;
  status: Crypto15mStatusName | string;
  exitReason: string | null;
  exitLimitCents: number | null;
  proceedsUsd: number | null;
  confidence: number;
  entryDeltaUsd: number | null;
  outcomeCorrect: number | null;
  settlementUsd: number | null;
  pnlUsd: number | null;
  resolved: boolean;
  dryRun: boolean;
  closeTime: string;
  kalshiEnv: KalshiEnv;
  createdAt: string;
  resolvedAt: string | null;
  error: string | null;
}

export interface Crypto15mStats {
  openCount: number;
  wins: number;
  losses: number;
  realizedPnlUsd: number;
  total: number;
}

export interface Crypto15mSizing {
  mode: 'fixed' | 'balance_pct';
  balancePct: number;
  maxLossPct: number;
  balanceUsd: number;
  estPriceCents: number;
  estContracts: number;
  estCostUsd: number;
  note: string;
}

export interface Crypto15mBacktest {
  n: number;
  wins: number;
  winRate: number;
  netEvCentsPerContract: number;
  totalPnlUsd: number;
  maxDrawdownUsd: number;
  contracts: number;
  windowsScanned: number;
  byAsset: Record<string, { n: number; wins: number; pnlUsd: number }>;
  equity: { at: string | null; value: number }[];
  byHourUtc: { hour: number; n: number; wins: number; pnlUsd: number }[];
  byDay: { day: string; n: number; wins: number; pnlUsd: number }[];
  trades: { ticker: string; asset: string; side: string; costCents: number; minsLeft: number | null; won: boolean; pnlUsd: number; at: string }[];
  caveats: string[];
}

export interface CollectionStats {
  c15: {
    windows: number; resolved: number; ticks: number;
    firstAt: string | null; lastAt: string | null;
    recent: { ticker: string; asset: string; favorite: string | null; favorite_price: number | null; up_won: number | null; resolved: number; close_time: string }[];
  };
  main: {
    whales: number; whalesResolved: number; alerts: number; alertsResolved: number;
    firstAt: string | null; lastAt: string | null;
    topCategories: { category: string; n: number }[];
    recent: { ticker: string; category: string; taker_side: string; price: number; dollar_value: number; outcome_correct: number | null; resolved: number; created_at: string }[];
  };
  perps: PerpsCounts;
  collecting: { c15: boolean; main: boolean; perps: boolean };
}

export interface PerpsCounts {
  ticks: number; trades: number; candles: number; funding: number;
  firstAt: string | null; lastAt: string | null;
  byTicker: { ticker: string; ticks: number; lastAt: string | null }[];
}

export interface PerpsWallet {
  env: string;
  settledUsd: number | null;        // total settled cash in the perps wallet
  availableUsd: number | null;      // free to trade (settled − margin locked)
  positionValueUsd: number | null;  // mark-to-market value of open positions
  restingMarginUsd: number | null;  // margin locked by resting orders
  maintenanceMarginUsd: number | null;
}

export interface PerpsStatus {
  recording: boolean;
  wsConnected: boolean;
  wallet?: PerpsWallet | null;       // separate perps wallet; null until a good read

  ws: {
    enabled: boolean; connected: boolean; env: string; symbols: string[];
    bufferedTicks: number; bufferedTrades: number;
    droppedTicks: number; droppedTrades: number;
    lastMsgAgeSec: number | null;
  };
  symbols: string[];
  quotes: {
    symbol: string; last: number | null; bid: number | null; ask: number | null;
    ref: number | null; fundingRate: number | null;
    nextFundingTime: string | null; tsMs: number | null;
  }[];
  counts: PerpsCounts;
  backfill: { running: boolean; done: boolean; progress: string; error: string | null };
  farmer: PerpsFarmerStatus;
  strategy: PerpsStrategyStatus;
}

export interface PerpsStrategyStatus {
  enabled: boolean;
  live: boolean;
  halted: boolean;
  haltReason: string;
  lastReason: string;                      // why-not-entering, surfaced live
  lastError: string;
  openPosition: {
    ticker: string; side: string; dryRun: boolean; contracts: number;
    entry: number; mark: number | null; unrealizedUsd: number | null;
    openedAt: string;
  } | null;
  dayPnlUsd: number;
  bars: number;
}

export interface PerpPositionRow {
  id: number;
  ticker: string;
  side: string;
  dry_run: number;
  opened_at: string;
  closed_at: string | null;
  contracts: number;
  entryUsd: number | null;
  exitUsd: number | null;
  leverage: number;
  feesUsd: number | null;
  fundingUsd: number | null;
  pnlUsd: number | null;
  exit_reason: string;
  entry_reason: string;
}

export interface PerpsFarmerStatus {
  enabled: boolean;
  running: boolean;
  halted: boolean;
  haltReason: string;
  lastError: string;
  symbol: string;
  makerFeeBps: number | null;      // measured from real fills; null = not yet measured (assumes Tier-0)
  maxFeeBps: number;               // user's fee cap; 0 = gate off
  inventoryContracts: number;
  avgEntry: number | null;
  liveOrders: { side: string; price: number; contracts: number }[];
  today: {
    fills: number; volumeUsd: number; feesUsd: number;
    realizedUsd: number; netUsd: number; costBps: number;
  };
  maintenanceWindow: boolean;
}

export interface TradingGate {
  id: string;
  label: string;
  state: 'ok' | 'blocked' | 'off';
  reason: string;
}

export interface TradingStatus {
  main: TradingGate[];
  mainFilterCounts: Record<string, number>;
  mainCandidates: number;
  mainPlaced: number;
  c15: {
    enabled: boolean;
    live: boolean;
    authed: boolean;
    env: string;
    blockReasons: Record<string, string>;
    takeProfitHalted?: boolean;
  };
}

export interface Crypto15mStatus {
  byStrategy?: { strategy: string; n: number; wins: number; losses: number; pnl_usd: number; fees_usd: number }[];
  modelCalibration?: { ok: boolean; n: number; rate: number | null; lb: number | null };
  enabled: boolean;
  live: boolean;
  liveArmed: boolean;
  liveSupported: boolean;
  authed: boolean;
  orderSize: number;
  maxConcurrent: number;
  takeProfitCents: number;          // per-bet take-profit price (¢); 0 = off
  sessionTakeProfitUsd: number;     // session take-profit target ($); 0 = off
  sessionPnlUsd: number;            // realized 15m P&L since the backend started
  takeProfitHalted: boolean;        // new entries halted because the session target was reached
  sizing: Crypto15mSizing;
  env: KalshiEnv;
  stats: Crypto15mStats;
  open: Crypto15mPosition[];
  recent: Crypto15mPosition[];
}


export interface KryptApi {
  app: {
    version: () => Promise<string>;
    openExternal: (url: string) => Promise<void>;
    showItemInFolder: (filePath: string) => Promise<void>;
    getUserDataPath: () => Promise<string>;
    factoryReset: () => Promise<ActionResult<{ deleted: Record<string, number> }>>;
    onDataReset: (cb: (payload: unknown) => void) => () => void;
  };
  state: {
    get: () => Promise<AppState>;
    onChange: (cb: (state: AppState) => void) => () => void;
    setStartMinimized: (v: boolean) => Promise<ActionResult>;
    setStartWithWindows: (v: boolean) => Promise<ActionResult>;
    setEnableDiscordRpc: (v: boolean) => Promise<ActionResult>;
    acceptDisclaimer: () => Promise<ActionResult>;
  };
  config: {
    get: () => Promise<TraderConfig>;
    update: (patch: Partial<TraderConfig>) => Promise<TraderConfig>;
    replace: (config: TraderConfig) => Promise<TraderConfig>;
    reset: () => Promise<TraderConfig>;
    listStrategies: () => Promise<StrategyPreset[]>;
    applyStrategy: (id: string) => Promise<TraderConfig>;
  };
  profiles: {
    list: () => Promise<Profile[]>;
    save: (name: string, description?: string, kind?: ProfileKind) => Promise<ActionResult<Profile>>;
    apply: (id: string) => Promise<ActionResult<TraderConfig>>;
    rename: (id: string, name: string) => Promise<ActionResult>;
    update: (id: string) => Promise<ActionResult<Profile>>;
    delete: (id: string) => Promise<ActionResult>;
    duplicate: (id: string) => Promise<ActionResult<Profile>>;
    export: (id: string) => Promise<ActionResult<string>>;
    import: (json: string) => Promise<ActionResult<Profile>>;
  };
  credentials: {
    status: () => Promise<CredentialsState>;
    statusAll: () => Promise<CredentialsStatusAll>;
    save: (input: CredentialsInput) => Promise<ActionResult>;
    test: (env?: KalshiEnv) => Promise<ActionResult<{ env: KalshiEnv; balanceUsd: number }>>;
    clear: (env?: KalshiEnv) => Promise<ActionResult>;
    onChanged: (cb: (payload: unknown) => void) => () => void;
  };
  backend: {
    info: () => Promise<BackendInfo>;
    start: () => Promise<ActionResult>;
    stop: () => Promise<ActionResult>;
    restart: () => Promise<ActionResult>;
    onInfo: (cb: (info: BackendInfo) => void) => () => void;
    runOnce: (
      action:
        | 'syncMarkets'
        | 'pollOrders'
        | 'resolveAll'
        | 'reconcilePositions'
        | 'recomputePnl'
        | 'reconcileFills'
        | 'auditPnl'
    ) => Promise<ActionResult<{ summary: string }>>;
  };
  trading: {
    setEnabled: (enabled: boolean) => Promise<ActionResult>;
    cancelAllOpen: () => Promise<ActionResult<{ canceled: number }>>;
    status: () => Promise<TradingStatus | null>;
    collection: () => Promise<CollectionStats | null>;
    exportData: () => Promise<{ dir: string; files: string[] } | null>;
    flatten: () => Promise<ActionResult<{ closed: number }>>;
  };
  data: {
    account: () => Promise<AccountSnapshot>;
    pnlSeries: (sinceHours?: number) => Promise<PnlPoint[]>;
    positions: (filter?: PositionFilter) => Promise<BotPosition[]>;
    signals: (filter?: SignalFilter) => Promise<SignalRow[]>;
    scannerStats: () => Promise<ScannerStats>;
    botRuns: (env?: KalshiEnv | null, limit?: number) => Promise<BotRunsResponse>;
    onAccount: (cb: (snap: AccountSnapshot) => void) => () => void;
    onPosition: (cb: (pos: BotPosition) => void) => () => void;
    onSignal: (cb: (sig: SignalRow) => void) => () => void;
  };
  crypto15m: {
    snapshot: () => Promise<Crypto15mSnapshot>;
    status: () => Promise<Crypto15mStatus>;
    backtest: (args?: { sinceDays?: number; config?: Partial<TraderConfig> }) => Promise<Crypto15mBacktest | null>;
    backtestMain: (args?: { sinceDays?: number; config?: Partial<TraderConfig> }) => Promise<Crypto15mBacktest | null>;
    history: (args?: { limit?: number }) => Promise<{ rows: Crypto15mPosition[] } | null>;
  };
  perps: {
    status: () => Promise<PerpsStatus | null>;
    backfill: () => Promise<{ ok: boolean } | null>;
    farmFlatten: () => Promise<{ inventoryCc: number } | null>;
    backtest: (args?: { sinceDays?: number; config?: Partial<TraderConfig> }) => Promise<Crypto15mBacktest | null>;
    history: (args?: { limit?: number }) => Promise<{ rows: PerpPositionRow[] } | null>;
    stratFlatten: () => Promise<{ closed: number } | null>;
  };
  kalshi: {
    marketUrl: (args: { eventTicker?: string; ticker?: string; env?: string }) =>
      Promise<{ url: string }>;
  };
  logs: {
    tail: (limit?: number) => Promise<LogEntry[]>;
    onAppend: (cb: (entry: LogEntry) => void) => () => void;
    clear: () => Promise<ActionResult>;
    openFolder: () => Promise<void>;
  };
  window: {
    minimize: () => void;
    maximize: () => void;
    close: () => void;
    isMaximized: () => Promise<boolean>;
    onMaximizeChange: (cb: (max: boolean) => void) => () => void;
  };
}

export interface PositionFilter {
  status?: BotPosition['status'][];
  resolved?: boolean | null;
  signalSource?: SignalSource | null;
  limit?: number;
}

export interface SignalFilter {
  source?: SignalSource | null;
  minConfidence?: number;
  minEdge?: number;
  resolved?: boolean | null;
  limit?: number;
}

declare global {
  interface Window {
    krypt: KryptApi;
  }
}
