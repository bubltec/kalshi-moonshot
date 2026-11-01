import { AppFooter, AppFrame, AppHeader, AppMain, AppSection, Badge, Card, Cluster, ErrorBox, Loading, Notice, Stack, Tab, Tabs, TabsList, TabsPanel, Text } from '@bubltec/uiux';
import { useCallback, useEffect, useState } from 'react';
import { Settings } from './Settings';
import type { AdminData, AdminLane } from './types';

const usd = (n: number) => `${n < 0 ? '-' : n > 0 ? '+' : ''}$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const tone = (n: number) => (n > 0 ? 'pos' : n < 0 ? 'neg' : undefined);
const laneName = (lane: string) => lane.replaceAll('_', ' ');

/** A lane's cumulative P&L by day as a line: the equity curve. */
function Curve({ daily }: { daily: Record<string, number> }) {
  const days = Object.keys(daily).sort();
  if (days.length < 2) return <p className="legend">not enough days for a curve</p>;
  let run = 0;
  const ys = days.map((d) => (run += daily[d]!));
  const lo = Math.min(0, ...ys), hi = Math.max(0, ...ys), span = hi - lo || 1;
  const pts = ys.map((y, i) => `${(i / (ys.length - 1)) * 100},${40 - ((y - lo) / span) * 40}`).join(' ');
  const zero = 40 - ((0 - lo) / span) * 40;
  return (
    <svg className="spark" viewBox="0 0 100 40" preserveAspectRatio="none" role="img" aria-label={`cumulative P&L ${usd(ys[ys.length - 1]!)} over ${days.length} days`}>
      <line x1="0" x2="100" y1={zero} y2={zero} />
      <polyline points={pts} />
    </svg>
  );
}

function LaneTable({ lanes }: { lanes: AdminLane[] }) {
  const totals = lanes.reduce((a, l) => ({ staked: a.staked + l.staked_usd, pnl: a.pnl + l.pnl_usd, bets: a.bets + l.settled }), { staked: 0, pnl: 0, bets: 0 });
  return (
    <div className="table-wrap">
      <table className="lanes">
        <thead>
          <tr><th>lane</th><th>mode</th><th>settled</th><th>win rate</th><th>staked</th><th>P&amp;L</th><th>return</th></tr>
        </thead>
        <tbody>
          {lanes.map((l) => (
            <tr key={`${l.lane}-${l.mode}`}>
              <td>{laneName(l.lane)} {l.retired ? <Badge tone="warn">retired</Badge> : null}</td>
              <td>{l.mode}</td>
              <td>{l.settled}</td>
              <td>{l.win_rate === null ? '-' : `${(l.win_rate * 100).toFixed(0)}%`}</td>
              <td>{usd(l.staked_usd).replace(/^\+/, '')}</td>
              <td className={tone(l.pnl_usd)}>{usd(l.pnl_usd)}</td>
              <td className={tone(l.pnl_usd)}>{l.staked_usd > 0 ? `${((l.pnl_usd / l.staked_usd) * 100).toFixed(1)}%` : '-'}</td>
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

function LaneDetail({ lane }: { lane: AdminLane }) {
  const exits = Object.entries(lane.exits).sort((a, b) => b[1].sales - a[1].sales);
  return (
    <Card>
      <Stack>
        <Cluster>
          <Text variant="title">{laneName(lane.lane)}</Text>
          <Badge tone={lane.mode === 'live' ? 'accent' : 'neutral'}>{lane.mode}</Badge>
          {lane.retired ? <Badge tone="warn">retired</Badge> : null}
          <Badge tone={lane.pnl_usd >= 0 ? 'ok' : 'danger'}>{usd(lane.pnl_usd)}</Badge>
        </Cluster>
        <Curve daily={lane.daily_pnl_usd} />
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
  const load = useCallback(() => {
    fetch('./admin.json', { cache: 'no-cache' })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`admin.json: ${r.status}`))))
      .then(setData)
      .catch((e: Error) => setError(e.message));
  }, []);
  useEffect(load, [load]);

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
                <Text variant="muted">Generated {data.generated}</Text>
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
                        <LaneTable lanes={mode === 'live' ? live : paper} />
                        <div className="detail-grid">
                          {(mode === 'live' ? live : paper).map((l) => (
                            <LaneDetail key={`${l.lane}-${l.mode}`} lane={l} />
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
