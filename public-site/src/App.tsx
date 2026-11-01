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
      .then(setData)
      .catch((e: Error) => setError(e.message));
  }, []);

  return (
    <AppFrame>
      <AppHeader brand={<Text variant="display">Moonshot</Text>} />
      <AppMain>
        <AppSection>
          <Stack>
            <Notice>
              Counts only, no dollar amounts. Results run a day behind. A win rate appears once a lane has {data?.min_settled_for_rate ?? 30} settled bets. A paper lane trades on paper, not real money.
            </Notice>
            {error ? <ErrorBox title="Could not load the results">{error}</ErrorBox> : null}
            {!data && !error ? <Loading /> : null}
            {data ? (
              <>
                <Text variant="muted">Through {data.as_of}</Text>
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
