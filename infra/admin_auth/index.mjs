// The admin login: GitHub OAuth, then CloudFront signed cookies. CloudFront checks the cookies at the edge for /admin/*, so nothing else here needs to guard the page.
// Routes (a Lambda function URL behind the site's CloudFront at /auth/*):
//   /auth/github    start: a random state in a cookie, then to GitHub
//   /auth/callback  GitHub returns here; the state must match, the GitHub user's numeric id must be in ADMIN_GITHUB_IDS, then three signed cookies for /admin/*
//   /auth/logout    expire the cookies
// Only the numeric GitHub id counts (a login name can be released and reused). No dependencies beyond the AWS SDK that the Node runtime includes.
import { createHash, createHmac, createSign, randomBytes, timingSafeEqual } from 'node:crypto';

const SESSION_SECONDS = 8 * 3600;

export const cfBase64 = (buf) => Buffer.from(buf).toString('base64').replace(/\+/g, '-').replace(/=/g, '_').replace(/\//g, '~');

export function parseCookies(header) {
  const out = {};
  for (const part of (header || '').split(';')) {
    const i = part.indexOf('=');
    if (i > 0) out[part.slice(0, i).trim()] = part.slice(i + 1).trim();
  }
  return out;
}

export function safeEqual(a, b) {
  const x = Buffer.from(String(a ?? '')), y = Buffer.from(String(b ?? ''));
  return x.length === y.length && x.length > 0 && timingSafeEqual(x, y);
}

/** The three CloudFront cookies for /admin/*, a custom policy valid until `expires` (epoch seconds), signed RSA-SHA1 as CloudFront requires. */
export function signedCookies({ origin, keyPairId, privateKey, expires }) {
  const policy = JSON.stringify({ Statement: [{ Resource: `${origin}/admin/*`, Condition: { DateLessThan: { 'AWS:EpochTime': expires } } }] });
  const signature = createSign('RSA-SHA1').update(policy).sign(privateKey);
  return { 'CloudFront-Policy': cfBase64(policy), 'CloudFront-Signature': cfBase64(signature), 'CloudFront-Key-Pair-Id': keyPairId };
}

// Lax, not Strict: the redirect from GitHub back to /admin/ is a cross-site chain, and a Strict cookie is not sent on it (a blank page).
/** Proof of a recent GitHub sign-in, for the settings API: "id.timestamp.hmac". The key comes from the CloudFront private key, which the API Lambda can also read, so there is no second secret to keep. */
export const freshKey = (privateKey) => createHash('sha256').update(`fresh:${privateKey}`).digest();
export const freshToken = (id, privateKey, nowSeconds) => {
  const body = `${id}.${nowSeconds}`;
  return `${body}.${createHmac('sha256', freshKey(privateKey)).update(body).digest('hex')}`;
};

const cookie = (name, value, path, maxAge, sameSite = 'Lax') => `${name}=${value}; Path=${path}; Max-Age=${maxAge}; HttpOnly; Secure; SameSite=${sameSite}`;
const redirect = (location, cookies = []) => ({ statusCode: 302, headers: { Location: location, 'Cache-Control': 'no-store' }, cookies, body: '' });
const page = (status, text) => ({ statusCode: status, headers: { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' }, body: text });

let secrets;
async function loadSecrets(env) {
  if (secrets) return secrets;
  const { SSMClient, GetParameterCommand } = await import('@aws-sdk/client-ssm');
  const ssm = new SSMClient({});
  const get = async (Name) => (await ssm.send(new GetParameterCommand({ Name, WithDecryption: true }))).Parameter.Value;
  secrets = { clientSecret: await get(env.CLIENT_SECRET_PARAM), privateKey: await get(env.PRIVATE_KEY_PARAM) };
  return secrets;
}

export async function handler(event, _context, deps = {}) {
  const env = deps.env ?? process.env;
  const fetchFn = deps.fetch ?? fetch;
  const origin = env.SITE_ORIGIN;
  const allowed = (env.ADMIN_GITHUB_IDS || '').split(',').map((s) => s.trim()).filter(Boolean);
  if (!env.GITHUB_CLIENT_ID || allowed.length === 0) return page(503, 'admin login is not configured');
  const path = event.rawPath || '/';
  const query = event.queryStringParameters || {};
  const cookies = parseCookies((event.cookies || []).join('; '));

  if (path === '/auth/github') {
    const state = randomBytes(16).toString('hex');
    const url = new URL('https://github.com/login/oauth/authorize');
    url.search = new URLSearchParams({ client_id: env.GITHUB_CLIENT_ID, redirect_uri: `${origin}/auth/callback`, scope: 'read:user', state }).toString();
    return redirect(url.toString(), [cookie('oauth_state', state, '/auth', 600)]);
  }

  if (path === '/auth/callback') {
    if (!query.code || !safeEqual(query.state, cookies.oauth_state)) return page(400, 'login expired or invalid; start again at /auth/github');
    const { clientSecret, privateKey } = deps.secrets ?? (await loadSecrets(env));
    const tokenRes = await fetchFn('https://github.com/login/oauth/access_token', {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_id: env.GITHUB_CLIENT_ID, client_secret: clientSecret, code: query.code, redirect_uri: `${origin}/auth/callback` }),
    });
    const token = (await tokenRes.json()).access_token;
    if (!token) return page(401, 'GitHub did not accept the login');
    const userRes = await fetchFn('https://api.github.com/user', { headers: { Authorization: `Bearer ${token}`, 'User-Agent': 'kalshi-moonshot-admin', Accept: 'application/vnd.github+json' } });
    const user = await userRes.json();
    if (!user || !allowed.includes(String(user.id))) return page(403, 'this GitHub account is not allowed');
    const expires = Math.floor(Date.now() / 1000) + SESSION_SECONDS;
    const signed = signedCookies({ origin, keyPairId: env.CF_KEY_PAIR_ID, privateKey, expires });
    return redirect(`${origin}/admin/`, [
      ...Object.entries(signed).map(([k, v]) => cookie(k, v, '/admin', SESSION_SECONDS)),
      cookie('admin_fresh', freshToken(user.id, privateKey, Math.floor(Date.now() / 1000)), '/admin/api', SESSION_SECONDS),
      cookie('oauth_state', '', '/auth', 0),
    ]);
  }

  if (path === '/auth/logout') {
    return redirect(`${origin}/`, ['CloudFront-Policy', 'CloudFront-Signature', 'CloudFront-Key-Pair-Id'].map((k) => cookie(k, '', '/admin', 0)).concat(cookie('admin_fresh', '', '/admin/api', 0)));
  }
  return page(404, 'not found');
}
