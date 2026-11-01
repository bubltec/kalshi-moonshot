/** The shape of admin.json, as written by src/kalshi_moonshot/exporter.py (build_admin). Dollars live here, which is why it is served only behind the login. */
export interface AdminLane {
  lane: string;
  mode: 'live' | 'paper';
  retired: boolean;
  bets: number;
  settled: number;
  win_rate: number | null;
  staked_usd: number;
  pnl_usd: number;
  daily_pnl_usd: Record<string, number>;
  daily: Record<string, { settled: number; wins: number; staked_usd: number; pnl_usd: number }>;
  settles: [number, number, number, number][];
  last_bet: string;
  exits: Record<string, { sales: number; pnl_usd: number }>;
}

export interface AdminData {
  generated: string;
  lanes: AdminLane[];
  settings?: AdminSettings;
  control?: ControlState;
}

/** What the bot publishes for the settings editor: each tunable field's bounds (overrides.py) and each lane's value now. */
export interface Tunable {
  kind: 'float' | 'int' | 'bool';
  low: number;
  high: number;
  label: string;
  unit: string;
  /** "up": a larger value risks more money. The page warns on a change that adds risk. */
  risk: 'up' | 'down' | 'neutral';
}
export type Value = number | boolean;
export interface HistoryEntry {
  revision: number;
  at: string;
  by: string;
  lane: string;
  changes: Record<string, { from: Value | null; to: Value }>;
}
export interface ControlState {
  applied_revision: number | null;
  status: { revision: number; state: 'applied' | 'rejected'; errors: string[]; at: string } | null;
  document: { revision: number; history: HistoryEntry[] } | null;
}
export interface AdminSettings {
  schema: Record<string, Tunable>;
  lanes: Record<string, Record<string, Value>>;
}
