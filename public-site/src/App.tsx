import { AppFooter, AppFrame, AppHeader, AppMain, AppSection, Badge, Card, Cluster, ErrorBox, Loading, Notice, Stack, Text } from '@bubltec/uiux';
import { useEffect, useState } from 'react';
import type { PublicData, PublicLane } from './types';

/** "commodity_bounce" -> "commodity bounce". */
const laneName = (lane: string) => lane.replaceAll('_', ' ');

/** "KXBTC15M" -> "BTC 15m": the kind of market. */
function seriesName(series: string) {
  const m = /^KX([A-Z]+?)(\d+M|D)?$/.exec(series);
  return m ? `${m[1]}${m[2] ? ' ' + m[2].toLowerCase() : ''}` : series;
}

function Split({ lane }: { lane: PublicLane }) {
  const total = lane.held_won + lane.cashed_out + lane.held_lost;
  if (total === 0) return null;
  const pct = (n: number) => `${(n / total) * 100}%`;
  return (
    <Stack>
      <div className="split-bar" role="img" aria-label={`${lane.held_won} held and won, ${lane.cashed_out} cashed out, ${lane.held_lost} held and lost`}>
        <span className="split-won" style={{ width: pct(lane.held_won) }} />
        <span className="split-cash" style={{ width: pct(lane.cashed_out) }} />
        <span className="split-lost" style={{ width: pct(lane.held_lost) }} />
      </div>
      <p className="legend">held and won · cashed out · held and lost</p>
    </Stack>
  );
}

/** Win rate so far, by day, drawn only from the day the lane has enough settled bets for a rate to mean anything. */
function RateCurve({ lane, minSettled }: { lane: PublicLane; minSettled: number }) {
  let n = 0, w = 0;
  const pts: number[] = [];
  for (const [, settled, won] of lane.daily) {
    n += settled; w += won;
    if (n >= minSettled) pts.push(w / n);
  }
  if (pts.length < 2) return <p className="legend">win rate curve appears after {minSettled} settled bets and a second day</p>;
  const line = pts.map((v, i) => `${(i / (pts.length - 1)) * 100},${40 - v * 40}`).join(' ');
  return (
    <Stack>
      <svg className="spark" viewBox="0 0 100 40" preserveAspectRatio="none" role="img" aria-label={`win rate over time, now ${(pts[pts.length - 1]! * 100).toFixed(0)}%`}>
        <line x1="0" x2="100" y1="20" y2="20" />
        <polyline points={line} />
      </svg>
      <p className="legend">win rate so far, by day (the middle line is 50%)</p>
    </Stack>
  );
}

/** Settled bets per day, the won share in green and the rest in red. */
function DailyBars({ lane }: { lane: PublicLane }) {
  const days = lane.daily.slice(-30);
  if (days.length === 0) return null;
  const top = Math.max(...days.map((d) => d[1]));
  const w = 100 / days.length;
  return (
    <Stack>
      <svg className="bars" viewBox="0 0 100 40" preserveAspectRatio="none" role="img" aria-label={`settled bets per day over ${days.length} days`}>
        {days.map(([day, settled, won], i) => {
          const h = (settled / top) * 38, hw = (won / top) * 38;
          return (
            <g key={day}>
              <rect className="bar-lost" x={i * w + w * 0.1} width={w * 0.8} y={40 - h} height={h - hw} />
              <rect className="bar-won" x={i * w + w * 0.1} width={w * 0.8} y={40 - hw} height={hw} />
            </g>
          );
        })}
      </svg>
      <p className="legend">settled bets per day · won in green, lost in red</p>
    </Stack>
  );
}

function SummaryTable({ lanes }: { lanes: PublicLane[] }) {
  return (
    <div className="table-wrap">
      <table className="lanes">
        <thead>
          <tr><th>lane</th><th>bets</th><th>won</th><th>lost</th><th>cashed out</th><th>pending</th><th>win rate</th></tr>
        </thead>
        <tbody>
          {lanes.map((l) => (
            <tr key={`${l.lane}-${l.mode}`}>
              <td>{laneName(l.lane)}</td><td>{l.bets}</td><td>{l.held_won}</td><td>{l.held_lost}</td><td>{l.cashed_out}</td><td>{l.pending}</td>
              <td>{l.win_rate === null ? '-' : `${(l.win_rate * 100).toFixed(0)}%`}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LaneCard({ lane, minSettled }: { lane: PublicLane; minSettled: number }) {
  const markets = Object.entries(lane.markets).sort((a, b) => b[1] - a[1]);
  return (
    <Card>
      <Stack>
        <Cluster>
          <Text variant="title">{laneName(lane.lane)}</Text>
          <Badge tone={lane.mode === 'live' ? 'accent' : 'neutral'}>{lane.mode}</Badge>
        </Cluster>
        {lane.win_rate === null ? (
          <Badge tone="warn">collecting data ({lane.settled}/{minSettled} settled)</Badge>
        ) : (
          <Badge tone="accent">win rate {(lane.win_rate * 100).toFixed(0)}%</Badge>
        )}
        <dl className="lane-stats">
          <div><dt>bets</dt><dd>{lane.bets}</dd></div>
          <div><dt>won</dt><dd>{lane.held_won}</dd></div>
          <div><dt>lost</dt><dd>{lane.held_lost}</dd></div>
          <div><dt>cashed out</dt><dd>{lane.cashed_out}</dd></div>
          <div><dt>pending</dt><dd>{lane.pending}</dd></div>
        </dl>
        <Split lane={lane} />
        <DailyBars lane={lane} />
        <RateCurve lane={lane} minSettled={minSettled} />
        <Cluster>
          {markets.map(([series, n]) => (
            <Badge key={series}>{seriesName(series)} · {n}</Badge>
          ))}
        </Cluster>
      </Stack>
    </Card>
  );
}

export function App() {
  const [data, setData] = useState<PublicData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch('./public.json', { cache: 'no-cache' })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`public.json: ${r.status}`))))
      .then((d: PublicData) => setData({ ...d, lanes: d.lanes.filter((l) => l.mode === 'live').map((l) => ({ ...l, daily: l.daily ?? [] })) }))
      .catch((e: Error) => setError(e.message));
  }, []);

  return (
    <AppFrame>
      <AppHeader brand={<Text variant="display">Moonshot</Text>} />
      <AppMain>
        <AppSection>
          <Stack>
            <Notice>
              Live lanes only. Counts only, no dollar amounts. Results run a day behind. A win rate appears once a lane has {data?.min_settled_for_rate ?? 30} settled bets.
            </Notice>
            {error ? <ErrorBox title="Could not load the results">{error}</ErrorBox> : null}
            {!data && !error ? <Loading /> : null}
            {data ? (
              <>
                <Text variant="muted">Through {data.as_of}</Text>
                <SummaryTable lanes={data.lanes} />
                <div className="lane-grid">
                  {data.lanes.map((lane) => (
                    <LaneCard key={`${lane.lane}-${lane.mode}`} lane={lane} minSettled={data.min_settled_for_rate} />
                  ))}
                </div>
              </>
            ) : null}
          </Stack>
        </AppSection>
      </AppMain>
      <AppFooter>Kalshi Moonshot</AppFooter>
    </AppFrame>
  );
}
