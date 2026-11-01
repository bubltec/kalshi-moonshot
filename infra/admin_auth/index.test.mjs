import assert from 'node:assert/strict';
import { generateKeyPairSync, createVerify } from 'node:crypto';
import test from 'node:test';
import { cfBase64, handler, parseCookies, safeEqual, signedCookies } from './index.mjs';

const { publicKey, privateKey } = generateKeyPairSync('rsa', { modulusLength: 2048, publicKeyEncoding: { type: 'spki', format: 'pem' }, privateKeyEncoding: { type: 'pkcs8', format: 'pem' } });
const env = { GITHUB_CLIENT_ID: 'cid', ADMIN_GITHUB_IDS: '194750, 7', SITE_ORIGIN: 'https://site.example', CF_KEY_PAIR_ID: 'KID', CLIENT_SECRET_PARAM: 'x', PRIVATE_KEY_PARAM: 'y' };
const secrets = { clientSecret: 'sec', privateKey };
const fromBase64 = (s) => Buffer.from(s.replace(/-/g, '+').replace(/_/g, '=').replace(/~/g, '/'), 'base64');

test('the cookies carry a policy for /admin/* that verifies against the public key', () => {
  const c = signedCookies({ origin: env.SITE_ORIGIN, keyPairId: 'KID', privateKey, expires: 2000000000 });
  const policy = fromBase64(c['CloudFront-Policy']).toString();
  assert.equal(JSON.parse(policy).Statement[0].Resource, 'https://site.example/admin/*');
  assert.ok(createVerify('RSA-SHA1').update(policy).verify(publicKey, fromBase64(c['CloudFront-Signature'])));
  assert.equal(c['CloudFront-Key-Pair-Id'], 'KID');
  assert.ok(!/[+=/]/.test(cfBase64('a+b/c==')));
});

test('start sends the browser to GitHub with a state cookie', async () => {
  const r = await handler({ rawPath: '/auth/github' }, {}, { env, secrets });
  assert.equal(r.statusCode, 302);
  const url = new URL(r.headers.Location);
  assert.equal(url.host, 'github.com');
  assert.equal(url.searchParams.get('redirect_uri'), 'https://site.example/auth/callback');
  const state = url.searchParams.get('state');
  assert.match(r.cookies[0], new RegExp(`oauth_state=${state}; Path=/auth; .*HttpOnly; Secure`));
});

const github = (id) => async (url) => ({ json: async () => (String(url).includes('access_token') ? { access_token: 't' } : { id }) });
const callback = (state, cookieState, id) => handler({ rawPath: '/auth/callback', queryStringParameters: { code: 'c', state }, cookies: [`oauth_state=${cookieState}`] }, {}, { env, secrets, fetch: github(id) });

test('the allowed GitHub id gets three admin cookies and is sent to /admin/', async () => {
  const r = await callback('s1', 's1', 194750);
  assert.equal(r.statusCode, 200);
  assert.ok(r.body.includes('url=https://site.example/admin/') && r.body.includes('location.replace("https://site.example/admin/")'));
  const names = r.cookies.map((c) => c.split('=')[0]);
  assert.deepEqual(names.slice(0, 3), ['CloudFront-Policy', 'CloudFront-Signature', 'CloudFront-Key-Pair-Id']);
  assert.ok(r.cookies.slice(0, 3).every((c) => c.includes('Path=/admin') && c.includes('HttpOnly') && c.includes('Secure') && c.includes('SameSite=Lax')));
});

test('another GitHub account, a wrong state, or a missing state is refused', async () => {
  assert.equal((await callback('s1', 's1', 999)).statusCode, 403);
  assert.equal((await callback('s1', 'other', 194750)).statusCode, 400);
  assert.equal((await callback('s1', undefined, 194750)).statusCode, 400);
});

test('with no client id or no allowed ids it refuses everything', async () => {
  assert.equal((await handler({ rawPath: '/auth/github' }, {}, { env: { ...env, ADMIN_GITHUB_IDS: '' } })).statusCode, 503);
  assert.equal((await handler({ rawPath: '/auth/github' }, {}, { env: { ...env, GITHUB_CLIENT_ID: '' } })).statusCode, 503);
});

test('helpers', () => {
  assert.deepEqual(parseCookies('a=1; b=2=3'), { a: '1', b: '2=3' });
  assert.ok(safeEqual('x', 'x') && !safeEqual('x', 'y') && !safeEqual('', ''));
});
