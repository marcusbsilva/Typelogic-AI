// The HTTP client against the real server (store, integrity, exports) with a deterministic fake engine.
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { TypeLogicClient, ApiError } from '../dist/index.js';

const fixture = fileURLToPath(new URL('../../../web/tests/fixture_server.py', import.meta.url));

test('client covers evaluate, titles, tags, feedback, delete and exports', async () => {
  const data = mkdtempSync(join(tmpdir(), 'typelogic-sdk-'));
  const server = spawn(process.env.TYPELOGIC_PYTHON || 'python3', ['-I', fixture, '--data', data], { stdio: ['ignore', 'pipe', 'inherit'] });
  try {
    const url = await new Promise((resolve, reject) => { server.stdout.once('data', c => resolve(String(c).trim())); server.once('exit', reject); });
    const client = await new TypeLogicClient({ baseUrl: url }).connect();
    assert.equal((await client.status()).engine.status, 'ready');
    const result = await client.evaluate({ title: 'Refund', tags: ['Billing', 'vip'], state: 'Charged twice', questions: { route: { type: 'choice', instructions: 'Team?', criteria: { A: 'Billing', B: 'Support' } } } });
    assert.equal(result.answers.route.choice, 'A');
    assert.equal(result.integrity.verified, true);
    assert.equal(result.external_action_executed, false);
    assert.equal(result.title, 'Refund');
    assert.deepEqual(result.tags, ['billing', 'vip']);
    assert.ok(!('decision_status' in result) && !('auto_review' in result));
    const lite = await client.decision(result.decision_id, 'lite');
    assert.deepEqual(lite, { title: 'Refund', tags: ['billing', 'vip'], questions: [{ question: 'Team?', answer: 'A' }] });
    assert.deepEqual(await client.feedback(result.decision_id, 'route', { A: 0, B: 1 }), { saved: true, retrained: false });
    const second = await client.evaluate({ case: 'Charged twice for one order.', decisions: [{ output: 'boolean', instructions: 'Is it a billing problem?' }] });
    assert.ok(second.answers.decision_1 && second.title && second.title_source !== 'user');
    assert.equal((await client.decisions('default', 'VIP')).length, 1);
    assert.equal((await client.decisions()).length, 2);
    assert.ok((await client.tags()).suggested.includes('billing'));
    const info = await client.archiveInfo();
    assert.equal(info.limit, 200);
    assert.equal(info.full, false);
    assert.equal((await client.delete(second.decision_id)).deleted, true);
    await assert.rejects(client.evaluate({ state: ' ', questions: {} }), e => e instanceof ApiError && e.status === 422);
    assert.equal((await client.deleteMany([result.decision_id, 'missing'])).deleted, 1);
    assert.equal((await client.decisions()).length, 0);
  } finally {
    server.kill();
    rmSync(data, { recursive: true, force: true });
  }
});

test('client refuses non-loopback hosts', () => {
  assert.throws(() => new TypeLogicClient({ baseUrl: 'http://example.com' }), /loopback/);
});
