import assert from 'node:assert/strict';
import { generateKeyPairSync } from 'node:crypto';
import test from 'node:test';
import { freshToken, signedCookies } from '../admin_auth/index.mjs';
import { checkChange, cloudfrontCookiesOk, freshId, handler, parseCookies } from './index.mjs';

const { publicKey, privateKey } = generateKeyPairSync('rsa', { modulusLength: 2048, publicKeyEncoding: { type: 'spki', format: 'pem' }, privateKeyEncoding: { type: 'pkcs8', format: 'pem' } });
const other = generateKeyPairSync('rsa', { modulusLength: 2048, publicKeyEncoding: { type: 'spki', format: 'pem' }, privateKeyEncoding: { type: 'pkcs8', format: 'pem' } });
const NOW = 1_800_000_000;
const env = { ADMIN_GITHUB_IDS: '194750', CF_PUBLIC_KEY: publicKey, CF_KEY_PAIR_ID: 'KID', SITE_ORIGIN: 'https://site.example', BUCKET: 'b', PRIVATE_KEY_PARAM: 'p' };

const schema = { min_edge: { kind: 'float', low: 0, high: 0.5 }, max_open: { kind: 'int', low: 0, high: 20 }, enabled: { kind: 'bool' } };
const adminDoc = { lanes: [{ lane: 'commodity', mode: 'live', retired: false }, { lane: 'old', mode: 'live', retired: true }, { lane: 'paperish', mode: 'paper', retired: false }], settings: { schema, lanes: { commodity: { min_edge: 0.1, max_open: 2, enabled: true }, old: { min_edge: 0.1 }, paperish: { min_edge: 0.1 } } } };

class FakeS3 {
  constructor(objects) { this.objects = { 'admin/admin.json': adminDoc, ...objects }; this.puts = []; }
  async get(key) { return key in this.objects ? structuredClone(this.objects[key]) : null; }
  async put(key, body) { this.puts.push({ key, body }); this.objects[key] = structuredClone(body); }
}

const cookiesFor = ({ at = NOW, signWith = privateKey, expires = NOW + 3600, id = '194750', fresh = true } = {}) => {
  const c = signedCookies({ origin: env.SITE_ORIGIN, keyPairId: 'KID', privateKey: signWith, expires });
  const list = Object.entries(c).map(([k, v]) => `${k}=${v}`);
  if (fresh) list.push(`admin_fresh=${freshToken(id, privateKey, at)}`);
  return list;
};
const post = (body, { cookies = cookiesFor(), headers = { origin: env.SITE_ORIGIN, 'x-admin-request': '1' } } = {}) =>
  ({ requestContext: { http: { method: 'POST' } }, rawPath: '/admin/api/overrides', headers, cookies, body: JSON.stringify(body) });
const run = (event, s3 = new FakeS3({})) => handler(event, {}, { env, privateKey, store: s3, now: NOW }).then((r) => ({ ...r, json: JSON.parse(r.body), s3 }));
const change = { lane: 'commodity', changes: { min_edge: 0.2 }, base_revision: 0 };

test('a good change is written to the control document, with history, and audited', async () => {
  const s3 = new FakeS3({});
  const r = await run(post(change), s3);
  assert.equal(r.statusCode, 200);
  assert.equal(r.json.revision, 1);
  const doc = s3.objects['control/overrides.json'];
  assert.deepEqual(doc.lanes, { commodity: { min_edge: 0.2 } });
  assert.deepEqual(doc.history[0].changes, { min_edge: { from: 0.1, to: 0.2 } });
  assert.equal(doc.history[0].by, '194750');
  assert.deepEqual(s3.objects['control/audit/000001.json'].changes, { min_edge: { from: 0.1, to: 0.2 } });
});

test('a second change builds on the first and needs the current revision', async () => {
  const s3 = new FakeS3({});
  await run(post(change), s3);
  assert.equal((await run(post({ ...change, changes: { max_open: 3 }, base_revision: 0 }), s3)).statusCode, 409);
  const r = await run(post({ ...change, changes: { max_open: 3 }, base_revision: 1 }), s3);
  assert.equal(r.statusCode, 200);
  assert.deepEqual(s3.objects['control/overrides.json'].lanes.commodity, { min_edge: 0.2, max_open: 3 });
  assert.equal(s3.objects['control/overrides.json'].history.length, 2);
});

test('values outside the published bounds, unknown fields and non-live lanes are refused', async () => {
  for (const bad of [{ min_edge: 0.9 }, { min_edge: '0.1' }, { max_open: 1.5 }, { enabled: 'yes' }, { estimator: 'llm' }, { min_edge: null }]) {
    assert.equal((await run(post({ ...change, changes: bad }))).statusCode, 400, JSON.stringify(bad));
  }
  for (const lane of ['old', 'paperish', 'nope', 7]) assert.equal((await run(post({ ...change, lane }))).statusCode, 400, String(lane));
  assert.equal((await run(post({ lane: 'commodity', changes: {}, base_revision: 0 }))).statusCode, 400);
});

test('nothing is written when the request is refused, and a no-op change writes nothing', async () => {
  const s3 = new FakeS3({});
  await run(post({ ...change, changes: { min_edge: 0.9 } }), s3);
  await run(post({ ...change, changes: { min_edge: 0.1 } }), s3);
  assert.deepEqual(s3.puts, []);
});

test('without valid CloudFront cookies, a fresh sign-in, or this site as origin, nothing happens', async () => {
  const s3 = new FakeS3({});
  assert.equal((await run(post(change, { cookies: [] }), s3)).statusCode, 401);
  assert.equal((await run(post(change, { cookies: cookiesFor({ signWith: other.privateKey }) }), s3)).statusCode, 401);
  assert.equal((await run(post(change, { cookies: cookiesFor({ expires: NOW - 1 }) }), s3)).statusCode, 401);
  assert.equal((await run(post(change, { cookies: cookiesFor({ fresh: false }) }), s3)).statusCode, 401);
  assert.equal((await run(post(change, { cookies: cookiesFor({ at: NOW - 16 * 60 }) }), s3)).statusCode, 401);
  assert.equal((await run(post(change, { cookies: cookiesFor({ id: '999' }) }), s3)).statusCode, 401);
  assert.equal((await run(post(change, { headers: { origin: 'https://evil.example', 'x-admin-request': '1' } }), s3)).statusCode, 403);
  assert.equal((await run(post(change, { headers: { origin: env.SITE_ORIGIN } }), s3)).statusCode, 403);
  assert.deepEqual(s3.puts, []);
});

test('the session endpoint says whether the sign-in is fresh enough', async () => {
  const get = (cookies) => ({ requestContext: { http: { method: 'GET' } }, rawPath: '/admin/api/session', headers: {}, cookies });
  assert.deepEqual((await run(get(cookiesFor({ at: NOW - 60 })))).json, { fresh: true, secondsLeft: 840 });
  assert.deepEqual((await run(get(cookiesFor({ at: NOW - 3600 })))).json, { fresh: false, secondsLeft: 0 });
});

test('helpers', () => {
  assert.deepEqual(parseCookies(['a=1; b=2=3']), { a: '1', b: '2=3' });
  assert.equal(checkChange('min_edge', 0.2, schema), null);
  assert.ok(checkChange('min_edge', 0.7, schema) && checkChange('x', 1, schema) && checkChange('max_open', 2.5, schema));
  assert.equal(freshId(freshToken('1', privateKey, NOW), { privateKey, now: NOW, allowed: ['1'] }), '1');
  assert.equal(freshId(freshToken('1', privateKey, NOW).replace(/.$/, '0'), { privateKey, now: NOW, allowed: ['1'] }), null);
  assert.ok(!cloudfrontCookiesOk({}, { publicKey, keyPairId: 'KID', now: NOW }));
});
