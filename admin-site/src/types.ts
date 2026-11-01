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
  exits: Record<string, { sales: number; pnl_usd: number }>;
}

export interface AdminData {
  generated: string;
  lanes: AdminLane[];
}
