import { AppFooter, AppFrame, AppHeader, AppMain, AppSection, Badge, Button, Card, Cluster, ErrorBox, Loading, Notice, Stack, Tab, Tabs, TabsList, TabsPanel, Text } from '@bubltec/uiux';
import { useCallback, useEffect, useState } from 'react';
import { Settings } from './Settings';
import type { AdminData, AdminLane } from './types';

const usd = (n: number) => `${n < 0 ? '-' : n > 0 ? '+' : ''}$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const tone = (n: number) => (n > 0 ? 'pos' : n < 0 ? 'neg' : undefined);
const laneName = (lane: string) => lane.replaceAll('_', ' ');

/** A lane's cumulative P&L over the shared window, one step per settled bet. The time axis is the same for every lane, so a lane that stopped ends where it stopped. */
function Curve({ lane, from, to }: { lane: AdminLane; from: number; to: number }) {
  const inside = lane.settles.filter(([ts]) => ts >= from);
  if (inside.length === 0) return <p className="legend">no settled bets in this window</p>;
  let run = 0;
  const pts: [number, number][] = [[from, 0]];
  for (const [ts, pnl] of inside) { pts.push([ts, run]); run += pnl; pts.push([ts, run]); }
  const ys = pts.map((q) => q[1]);
  const lo = Math.min(0, ...ys), hi = Math.max(0, ...ys), span = hi - lo || 1, width = to - from || 1;
  const X = (ts: number) => ((ts - from) / width) * 100, Y = (v: number) => 40 - ((v - lo) / span) * 40;
  const last = pts[pts.length - 1]!;
  return (
    <svg className="spark" viewBox="0 0 100 40" preserveAspectRatio="none" role="img" aria-label={`cumulative P&L ${usd(run)} over ${inside.length} settled bets`}>
      <line x1="0" x2="100" y1={Y(0)} y2={Y(0)} />
      <polyline points={pts.map(([ts, v]) => `${X(ts)},${Y(v)}`).join(' ')} />
      <line className="end" x1={X(last[0])} x2={X(last[0])} y1="0" y2="40" />
    </svg>
  );
}

/** One window for every lane, so rows compare like with like: the last N hours up to when the file was made (0 = all history). */
const WINDOWS = [{ value: '24', label: '24 hours', hours: 24 }, { value: '168', label: '7 days', hours: 168 }, { value: '720', label: '30 days', hours: 720 }, { value: '0', label: 'All', hours: 0 }];

interface Row { settled: number; wins: number; staked_usd: number; pnl_usd: number }
function inWindow(l: AdminLane, from: number): Row {
  const row = { settled: 0, wins: 0, staked_usd: 0, pnl_usd: 0 };
  for (const [ts, pnl, stake, win] of l.settles) {
    if (ts < from) continue;
    row.settled += 1; row.wins += win; row.staked_usd += stake; row.pnl_usd += pnl;
  }
  return row;
}
/** [from, to] in epoch seconds for a window; "all" starts at the first settled bet of any lane. */
function span(data: AdminData, hours: number): [number, number] {
  const to = Date.parse(data.generated) / 1000;
  if (hours > 0) return [to - hours * 3600, to];
  const first = Math.min(to, ...data.lanes.flatMap((l) => l.settles.slice(0, 1).map((s) => s[0])));
  return [first, to];
}

function LaneTable({ lanes, from }: { lanes: AdminLane[]; from: number }) {
  const rows = lanes.map((l) => ({ l, r: inWindow(l, from) }));
  const totals = rows.reduce((a, { r }) => ({ staked: a.staked + r.staked_usd, pnl: a.pnl + r.pnl_usd, bets: a.bets + r.settled }), { staked: 0, pnl: 0, bets: 0 });
  return (
    <div className="table-wrap">
      <table className="lanes">
        <thead>
          <tr><th>lane</th><th>mode</th><th>settled</th><th>win rate</th><th>staked</th><th>P&amp;L</th><th>return</th></tr>
        </thead>
        <tbody>
          {rows.map(({ l, r }) => (
            <tr key={`${l.lane}-${l.mode}`}>
              <td>{laneName(l.lane)} {l.retired ? <Badge tone="warn">retired</Badge> : null} {r.settled === 0 ? <Badge tone="neutral">no settled bets in this window, last {l.last_bet}</Badge> : null}</td>
              <td>{l.mode}</td>
              <td>{r.settled}</td>
              <td>{r.settled ? `${((r.wins / r.settled) * 100).toFixed(0)}%` : '-'}</td>
              <td>{usd(r.staked_usd).replace(/^\+/, '')}</td>
              <td className={tone(r.pnl_usd)}>{usd(r.pnl_usd)}</td>
              <td className={tone(r.pnl_usd)}>{r.staked_usd > 0 ? `${((r.pnl_usd / r.staked_usd) * 100).toFixed(1)}%` : '-'}</td>
            </tr>
          ))}
          <tr>
            <td><strong>total</strong></td><td /><td>{totals.bets}</td><td />
            <td>{usd(totals.staked).replace(/^\+/, '')}</td>
            <td className={tone(totals.pnl)}>{usd(totals.pnl)}</td>
            <td className={tone(totals.pnl)}>{totals.staked > 0 ? `${((totals.pnl / totals.staked) * 100).toFixed(1)}%` : '-'}</td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

function LaneDetail({ lane, from, to }: { lane: AdminLane; from: number; to: number }) {
  const pnl = inWindow(lane, from).pnl_usd;
  const exits = Object.entries(lane.exits).sort((a, b) => b[1].sales - a[1].sales);
  return (
    <Card>
      <Stack>
        <Cluster>
          <Text variant="title">{laneName(lane.lane)}</Text>
          <Badge tone={lane.mode === 'live' ? 'accent' : 'neutral'}>{lane.mode}</Badge>
          {lane.retired ? <Badge tone="warn">retired</Badge> : null}
          <Badge tone={pnl >= 0 ? 'ok' : 'danger'}>{usd(pnl)}</Badge>
        </Cluster>
        <Curve lane={lane} from={from} to={to} />
        {exits.length ? (
          <div className="table-wrap">
            <table className="lanes">
              <thead><tr><th>exit rule</th><th>sales</th><th>P&amp;L</th></tr></thead>
              <tbody>
                {exits.map(([kind, e]) => (
                  <tr key={kind}><td>{kind}</td><td>{e.sales}</td><td className={tone(e.pnl_usd)}>{usd(e.pnl_usd)}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="legend">no early exits</p>
        )}
      </Stack>
    </Card>
  );
}

export function App() {
  const [data, setData] = useState<AdminData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState('live');
  const [window_, setWindow] = useState('24');
  const load = useCallback(() => {
    fetch('./admin.json', { cache: 'no-cache' })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`admin.json: ${r.status}`))))
      .then(setData)
      .catch((e: Error) => setError(e.message));
  }, []);
  useEffect(load, [load]);

  const range = data ? span(data, WINDOWS.find((w) => w.value === window_)!.hours) : ([0, 0] as [number, number]);
  const live = data?.lanes.filter((l) => l.mode === 'live') ?? [];
  const paper = data?.lanes.filter((l) => l.mode === 'paper') ?? [];
  return (
    <AppFrame>
      <AppHeader brand={<Text variant="display">Moonshot admin</Text>} />
      <AppMain>
        <AppSection>
          <Stack>
            <Notice tone="warn">Real dollar values. Changes go to the live bot, from the Settings tab.</Notice>
            {error ? <ErrorBox title="Could not load admin.json">{error}</ErrorBox> : null}
            {!data && !error ? <Loading /> : null}
            {data ? (
              <>
                <Cluster>
                  <Text variant="muted">Generated {data.generated}. Lanes table window:</Text>
                  {WINDOWS.map((w) => (
                    <Button key={w.value} variant={window_ === w.value ? 'solid' : 'ghost'} onClick={() => setWindow(w.value)}>{w.label}</Button>
                  ))}
                </Cluster>
                <Tabs value={tab} onValueChange={setTab}>
                  <TabsList>
                    <Tab value="live">Live ({live.length})</Tab>
                    <Tab value="paper">Paper ({paper.length})</Tab>
                    <Tab value="settings">Settings</Tab>
                  </TabsList>
                  <TabsPanel value="settings"><Settings data={data} reload={load} /></TabsPanel>
                  {(['live', 'paper'] as const).map((mode) => (
                    <TabsPanel key={mode} value={mode}>
                      <Stack>
                        <LaneTable lanes={mode === 'live' ? live : paper} from={range[0]} />
                        <div className="detail-grid">
                          {(mode === 'live' ? live : paper).map((l) => (
                            <LaneDetail key={`${l.lane}-${l.mode}`} lane={l} from={range[0]} to={range[1]} />
                          ))}
                        </div>
                      </Stack>
                    </TabsPanel>
                  ))}
                </Tabs>
              </>
            ) : null}
          </Stack>
        </AppSection>
      </AppMain>
      <AppFooter>Kalshi Moonshot admin</AppFooter>
    </AppFrame>
  );
}
