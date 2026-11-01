/** The shape of public.json, as written by src/kalshi_moonshot/exporter.py (build_public). Counts only: there is no money field to read. */
export interface PublicLane {
  lane: string;
  mode: 'live' | 'paper';
  bets: number;
  settled: number;
  pending: number;
  held_won: number;
  held_lost: number;
  cashed_out: number;
  win_rate: number | null;
  markets: Record<string, number>;
}

export interface PublicData {
  as_of: string;
  min_settled_for_rate: number;
  lanes: PublicLane[];
}
