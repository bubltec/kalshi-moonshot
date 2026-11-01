// The admin page's write API: change a live lane's tunable settings. A Lambda function URL behind the site's CloudFront at /admin/api/*.
//   GET  /admin/api/session    { fresh, secondsLeft }: whether a sign-in recent enough to change settings is on this browser
//   POST /admin/api/overrides  { lane, changes: { field: value }, base_revision }: write the change; the bot checks it again and applies it
// The function URL is public, so this Lambda trusts nothing from the edge. It checks the CloudFront signed cookie itself (the same signature CloudFront checks), that the sign-in is
// under FRESH_SECONDS old, that the request came from this site's page, and the change against the bounds the bot published in admin.json. The bot's checks (overrides.py) are the final ones.
// Every accepted change is written twice: into control/overrides.json (with a short history) and, never overwritten, as control/audit/<revision>.json.
import { createHash, createHmac, createVerify, timingSafeEqual } from 'node:crypto';

const FRESH_SECONDS = 15 * 60;
const KEEP_HISTORY = 200;
const CONTROL_KEY = 'control/overrides.json';
const ADMIN_KEY = 'admin/admin.json';

const json = (status, body) => ({ statusCode: status, headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }, body: JSON.stringify(body) });
const fromCfBase64 = (s) => Buffer.from(String(s).replace(/-/g, '+').replace(/_/g, '=').replace(/~/g, '/'), 'base64');

export function parseCookies(list) {
  const out = {};
  for (const part of (list || []).join('; ').split(';')) {
    const i = part.indexOf('=');
    if (i > 0) out[part.slice(0, i).trim()] = part.slice(i + 1).trim();
  }
  return out;
}

const safeEqual = (a, b) => {
  const x = Buffer.from(String(a ?? '')), y = Buffer.from(String(b ?? ''));
  return x.length === y.length && x.length > 0 && timingSafeEqual(x, y);
};

/** True when the CloudFront cookies are signed by our key, name this key pair, cover /admin/*, and have not expired. */
export function cloudfrontCookiesOk(cookies, { publicKey, keyPairId, now }) {
  const { 'CloudFront-Policy': policy, 'CloudFront-Signature': signature, 'CloudFront-Key-Pair-Id': kid } = cookies;
  if (!policy || !signature || kid !== keyPairId) return false;
  const text = fromCfBase64(policy).toString();
  try {
    if (!createVerify('RSA-SHA1').update(text).verify(publicKey, fromCfBase64(signature))) return false;
    const statement = JSON.parse(text).Statement?.[0];
    return Boolean(statement) && String(statement.Resource).endsWith('/admin/*') && statement.Condition?.DateLessThan?.['AWS:EpochTime'] > now;
  } catch {
    return false;
  }
}

/** The GitHub id in a fresh sign-in token ("id.timestamp.hmac", made by the login Lambda), or null when it is forged, malformed or older than FRESH_SECONDS. */
export function freshId(token, { privateKey, now, allowed }) {
  const [id, ts, mac] = String(token ?? '').split('.');
  if (!id || !ts || !mac) return null;
  const key = createHash('sha256').update(`fresh:${privateKey}`).digest();
  const expected = createHmac('sha256', key).update(`${id}.${ts}`).digest('hex');
  if (!safeEqual(mac, expected) || !allowed.includes(id)) return null;
  return now - Number(ts) <= FRESH_SECONDS && Number(ts) <= now + 60 ? id : null;
}

/** Why a change is refused, from the bounds the bot published in admin.json, or null. */
export function checkChange(field, value, schema) {
  const t = schema?.[field];
  if (!t) return `${field} is not a field that can be changed`;
  if (t.kind === 'bool') return typeof value === 'boolean' ? null : `${field} must be true or false`;
  if (typeof value !== 'number' || !Number.isFinite(value)) return `${field} must be a number`;
  if (t.kind === 'int' && !Number.isInteger(value)) return `${field} must be a whole number`;
  return value >= t.low && value <= t.high ? null : `${field} must be between ${t.low} and ${t.high}`;
}

/** The bucket, as get(key) -> parsed JSON or null and put(key, object). The SDK is in the Lambda runtime; tests pass a fake. */
async function bucketStore(bucket) {
  const { S3Client, GetObjectCommand, PutObjectCommand } = await import('@aws-sdk/client-s3');
  const s3 = new S3Client({});
  return {
    async get(Key) {
      try {
        return JSON.parse(await (await s3.send(new GetObjectCommand({ Bucket: bucket, Key }))).Body.transformToString());
      } catch (e) {
        if (e?.name === 'NoSuchKey' || e?.$metadata?.httpStatusCode === 404) return null;
        throw e;
      }
    },
    put: (Key, body) => s3.send(new PutObjectCommand({ Bucket: bucket, Key, Body: JSON.stringify(body), ContentType: 'application/json', CacheControl: 'private, no-store' })),
  };
}

let secrets;
async function loadPrivateKey(env) {
  if (secrets) return secrets;
  const { SSMClient, GetParameterCommand } = await import('@aws-sdk/client-ssm');
  secrets = (await new SSMClient({}).send(new GetParameterCommand({ Name: env.PRIVATE_KEY_PARAM, WithDecryption: true }))).Parameter.Value;
  return secrets;
}

export async function handler(event, _context, deps = {}) {
  const env = deps.env ?? process.env;
  const now = deps.now ?? Math.floor(Date.now() / 1000);
  const allowed = (env.ADMIN_GITHUB_IDS || '').split(',').map((x) => x.trim()).filter(Boolean);
  if (allowed.length === 0 || !env.CF_PUBLIC_KEY) return json(503, { error: 'not configured' });
  const method = event.requestContext?.http?.method ?? event.httpMethod;
  const path = event.rawPath || '/';
  const headers = Object.fromEntries(Object.entries(event.headers || {}).map(([k, v]) => [k.toLowerCase(), v]));
  const cookies = parseCookies(event.cookies);

  if (!cloudfrontCookiesOk(cookies, { publicKey: env.CF_PUBLIC_KEY, keyPairId: env.CF_KEY_PAIR_ID, now })) return json(401, { error: 'sign in at /auth/github' });
  const privateKey = deps.privateKey ?? (await loadPrivateKey(env));
  const who = freshId(cookies.admin_fresh, { privateKey, now, allowed });

  if (method === 'GET' && path === '/admin/api/session') {
    const [, ts] = String(cookies.admin_fresh ?? '').split('.');
    return json(200, { fresh: who !== null, secondsLeft: who ? Math.max(0, FRESH_SECONDS - (now - Number(ts))) : 0 });
  }

  if (method === 'POST' && path === '/admin/api/overrides') {
    // a form on another site cannot set these two, and the cookies are SameSite=Lax besides
    if (headers.origin !== env.SITE_ORIGIN || headers['x-admin-request'] !== '1') return json(403, { error: 'wrong origin' });
    if (!who) return json(401, { error: 'sign in again to change settings', needsFreshLogin: true });
    let req;
    try {
      req = JSON.parse(event.isBase64Encoded ? Buffer.from(event.body, 'base64').toString() : event.body);
    } catch {
      return json(400, { error: 'body is not JSON' });
    }
    const { lane, changes, base_revision: base } = req ?? {};
    if (typeof lane !== 'string' || !changes || typeof changes !== 'object' || Array.isArray(changes) || Object.keys(changes).length === 0 || Object.keys(changes).length > 30) return json(400, { error: 'lane and changes are required' });

    const store = deps.store ?? (await bucketStore(env.BUCKET));
    const admin = await store.get(ADMIN_KEY);
    const current = await store.get(CONTROL_KEY);
    const liveLanes = new Set((admin?.lanes ?? []).filter((l) => l.mode === 'live' && !l.retired).map((l) => l.lane));
    if (!liveLanes.has(lane) || !admin?.settings?.lanes?.[lane]) return json(400, { error: `${lane} is not a live lane` });
    const problems = Object.entries(changes).map(([f, v]) => checkChange(f, v, admin.settings.schema)).filter(Boolean);
    if (problems.length) return json(400, { error: problems.join('; ') });
    const revision = current?.revision ?? 0;
    if (base !== revision) return json(409, { error: 'settings changed since this page loaded; reload', revision });

    const before = { ...admin.settings.lanes[lane], ...(current?.lanes?.[lane] ?? {}) };
    const diff = Object.fromEntries(Object.entries(changes).filter(([f, v]) => before[f] !== v).map(([f, v]) => [f, { from: before[f] ?? null, to: v }]));
    if (Object.keys(diff).length === 0) return json(200, { revision, unchanged: true });
    const entry = { revision: revision + 1, at: new Date(now * 1000).toISOString(), by: who, lane, changes: diff };
    const next = {
      revision: revision + 1,
      updated: entry.at,
      by: who,
      lanes: { ...(current?.lanes ?? {}), [lane]: { ...(current?.lanes?.[lane] ?? {}), ...Object.fromEntries(Object.entries(diff).map(([f, d]) => [f, d.to])) } },
      history: [...(current?.history ?? []), entry].slice(-KEEP_HISTORY),
    };
    await store.put(`control/audit/${String(entry.revision).padStart(6, '0')}.json`, entry);
    await store.put(CONTROL_KEY, next);
    return json(200, { revision: next.revision });
  }

  return json(404, { error: 'not found' });
}
