import { Badge, Button, Card, Cluster, Dialog, Input, Notice, Stack, Switch, Text } from '@bubltec/uiux';
import { useEffect, useState } from 'react';
import type { AdminData, Tunable, Value } from './types';

const API = './api';
type Drafts = Record<string, Record<string, Value>>;
type Session = { fresh: boolean; secondsLeft: number } | null;

const laneName = (lane: string) => lane.replaceAll('_', ' ');
const show = (v: Value | null | undefined, t: Tunable) => (v === null || v === undefined ? '-' : typeof v === 'boolean' ? (v ? 'on' : 'off') : `${t.unit === '$' ? '$' : ''}${v}${t.unit === 's' ? ' s' : t.unit === 'x' ? 'x' : t.unit === '%' ? '%' : ''}`);

/** A change that puts more money at risk, by the field's own direction. A bool field's "up" means turning it on. */
function addsRisk(t: Tunable, from: Value, to: Value) {
  if (t.risk === 'neutral') return false;
  const up = typeof to === 'boolean' ? to && !from : (to as number) > (from as number);
  return t.risk === 'up' ? up : !up;
}

async function call(path: string, init?: RequestInit) {
  const res = await fetch(`${API}/${path}`, { credentials: 'same-origin', ...init, headers: { 'x-admin-request': '1', 'content-type': 'application/json' } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(body.error ?? `HTTP ${res.status}`), { status: res.status, needsFreshLogin: Boolean(body.needsFreshLogin) });
  return body as Record<string, unknown>;
}

export function Settings({ data, reload }: { data: AdminData; reload: () => void }) {
  const settings = data.settings;
  const [drafts, setDrafts] = useState<Drafts>({});
  const [review, setReview] = useState<string | null>(null);
  const [session, setSession] = useState<Session>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [sent, setSent] = useState<number | null>(null);

  const loadSession = () => call('session').then((r) => setSession(r as unknown as Session)).catch(() => setSession({ fresh: false, secondsLeft: 0 }));
  useEffect(() => { void loadSession(); }, []);
  // After a change is sent, look for the bot's answer (it pulls every minute, then republishes admin.json).
  useEffect(() => {
    if (sent === null || data.control?.status?.revision === sent) return;
    const t = setInterval(reload, 10_000);
    return () => clearInterval(t);
  }, [sent, data.control?.status?.revision, reload]);

  if (!settings) return <Notice tone="info">The bot has not published its settings yet. They appear after its next admin.json (up to 30 minutes after a restart).</Notice>;
  const lanes = data.lanes.filter((l) => l.mode === 'live' && !l.retired && settings.lanes[l.lane]);
  const status = data.control?.status;
  const revision = data.control?.document?.revision ?? 0;

  const setField = (lane: string, field: string, value: Value | undefined) =>
    setDrafts((d) => {
      const next = { ...d, [lane]: { ...(d[lane] ?? {}) } };
      if (value === undefined || value === settings.lanes[lane]![field]) delete next[lane]![field];
      else next[lane]![field] = value;
      if (Object.keys(next[lane]!).length === 0) delete next[lane];
      return next;
    });

  const changes = (lane: string) => Object.entries(drafts[lane] ?? {}).map(([field, to]) => ({ field, to, from: settings.lanes[lane]![field]!, t: settings.schema[field]! }));

  const send = async (lane: string) => {
    setBusy(true);
    setProblem(null);
    try {
      const r = await call('overrides', { method: 'POST', body: JSON.stringify({ lane, base_revision: revision, changes: drafts[lane] }) });
      setSent(Number(r.revision));
      setDrafts((d) => { const { [lane]: _gone, ...rest } = d; return rest; });
      setReview(null);
      reload();
    } catch (e) {
      const err = e as Error & { status?: number; needsFreshLogin?: boolean };
      setProblem(err.message);
      if (err.needsFreshLogin) void loadSession();
    } finally {
      setBusy(false);
    }
  };

  return (
    <Stack>
      <Notice tone="warn">These change the live bot. Each change is checked against hard limits, applied within about a minute, and recorded below. Lanes cannot be added or made live here.</Notice>
      {sent !== null && status?.revision === sent ? (
        status.state === 'applied' ? <Notice tone="info">Change {sent} is applied.</Notice> : <Notice tone="error">Change {sent} was refused by the bot: {status.errors.join('; ')}</Notice>
      ) : sent !== null ? <Notice tone="info">Change {sent} sent. Waiting for the bot to pull it (up to a minute)...</Notice> : null}
      {lanes.map((l) => {
        const cs = changes(l.lane);
        return (
          <Card key={l.lane}>
            <Stack>
              <Cluster>
                <Text variant="title">{laneName(l.lane)}</Text>
                {cs.length ? <Badge tone="accent">{cs.length} unsaved</Badge> : null}
              </Cluster>
              <div className="table-wrap">
                <table className="lanes settings">
                  <thead><tr><th>setting</th><th>now</th><th>change to</th><th>allowed</th></tr></thead>
                  <tbody>
                    {Object.entries(settings.schema).map(([field, t]) => {
                      const now = settings.lanes[l.lane]![field]!;
                      const draft = drafts[l.lane]?.[field];
                      const changed = draft !== undefined;
                      return (
                        <tr key={field} className={changed ? 'changed' : undefined}>
                          <td>{t.label}</td>
                          <td>{show(now, t)}</td>
                          <td>
                            {t.kind === 'bool' ? (
                              <Switch label={field} checked={typeof (draft ?? now) === 'boolean' ? (draft ?? now) as boolean : false} onCheckedChange={(v) => setField(l.lane, field, v)} />
                            ) : (
                              <Input
                                type="number" inputMode="decimal" step={t.kind === 'int' ? 1 : 'any'} min={t.low} max={t.high} aria-label={`${laneName(l.lane)} ${t.label}`}
                                value={changed ? String(draft) : ''} placeholder={String(now)}
                                onChange={(e) => setField(l.lane, field, e.target.value === '' ? undefined : Number(e.target.value))}
                              />
                            )}
                          </td>
                          <td className="legend">{t.kind === 'bool' ? 'on / off' : `${t.low} to ${t.high}`}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <Cluster>
                <Button disabled={cs.length === 0} onClick={() => { setProblem(null); setReview(l.lane); void loadSession(); }}>Review {cs.length || ''} change{cs.length === 1 ? '' : 's'}</Button>
                <Button variant="ghost" disabled={cs.length === 0} onClick={() => setDrafts((d) => { const { [l.lane]: _gone, ...rest } = d; return rest; })}>Discard</Button>
              </Cluster>
              <Dialog open={review === l.lane} onOpenChange={(o) => !o && setReview(null)} title={`Change ${laneName(l.lane)}`} description="Check each line. This goes to the live bot.">
                <Stack>
                  {cs.map(({ field, from, to, t }) => (
                    <Cluster key={field}>
                      <Text>{t.label}: <strong>{show(from, t)}</strong> to <strong>{show(to, t)}</strong></Text>
                      {addsRisk(t, from, to) ? <Badge tone="warn">adds risk</Badge> : null}
                    </Cluster>
                  ))}
                  {session && !session.fresh ? <Notice tone="warn">Your sign-in is more than 15 minutes old. <a href="/auth/github">Sign in again</a>, then reopen this page.</Notice> : null}
                  {problem ? <Notice tone="error">{problem}</Notice> : null}
                  <Cluster>
                    <Button disabled={busy || !session?.fresh} onClick={() => void send(l.lane)}>{busy ? 'Sending...' : 'Confirm and send'}</Button>
                    <Button variant="ghost" onClick={() => setReview(null)}>Cancel</Button>
                  </Cluster>
                </Stack>
              </Dialog>
            </Stack>
          </Card>
        );
      })}
      <History data={data} />
    </Stack>
  );
}

function History({ data }: { data: AdminData }) {
  const history = [...(data.control?.document?.history ?? [])].reverse().slice(0, 20);
  const schema = data.settings?.schema ?? {};
  return (
    <Card>
      <Stack>
        <Text variant="title">Recent changes</Text>
        {history.length === 0 ? <p className="legend">none yet</p> : (
          <div className="table-wrap">
            <table className="lanes">
              <thead><tr><th>#</th><th>when</th><th>lane</th><th>change</th></tr></thead>
              <tbody>
                {history.map((h) => (
                  <tr key={h.revision}>
                    <td>{h.revision}</td><td>{h.at.replace('T', ' ').replace(/\..*|Z$/, '')}</td><td>{laneName(h.lane)}</td>
                    <td>{Object.entries(h.changes).map(([f, c]) => `${schema[f]?.label ?? f}: ${String(c.from)} to ${String(c.to)}`).join('; ')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Stack>
    </Card>
  );
}
