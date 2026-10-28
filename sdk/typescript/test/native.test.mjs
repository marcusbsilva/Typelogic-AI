// Golden vectors computed by the Python reference must come back byte-identical through the N-API addon.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { core, CoreError } from '../dist/index.js';

const golden = JSON.parse(readFileSync(new URL('../../testdata/golden.json', import.meta.url), 'utf8')).cases;

test('addon loads and reports its version', () => {
  assert.match(core.coreVersion(), /^\d+\.\d+\.\d+$/);
});

test('every golden case matches the Python reference byte for byte', () => {
  const addon = core.loadAddon();
  for (const c of golden) {
    assert.equal(addon.call(c.method, c.params_json), c.response, `${c.method} ${JSON.stringify(c.params).slice(0, 120)}`);
  }
  assert.ok(golden.length >= 30);
});

test('typed wrappers and CoreError', () => {
  const v = core.validate({ state: 'Refund?', questions: { r: { type: 'choice', instructions: 'Route?', criteria: ['A', 'B'] } } });
  assert.equal(v.state, 'Refund?');
  assert.deepEqual(v.questions[0].keys, ['A', 'B']);
  const a = core.answers(v.questions, [[0.2, 0.8]], v.threshold);
  assert.equal(a.r.choice, 'B');
  assert.equal(a.r.eligible_for_automation, false);
  assert.match(core.exportFilename('abc', 'lite'), /^[a-z2-7]{8}\.json$/);
  assert.throws(() => core.validate({ state: ' ' }), e => e instanceof CoreError && e.type === 'ValueError' && e.message === 'Scenario: 1–120,000 characters');
  assert.throws(() => core.loadAddon().call(1, 2), /expects two strings/);
  assert.equal(core.hash('sha256', 'abc'), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
  const routed = core.route({ task: 'decision', language: 'en', latency: 'medium' }, [{ name: 'm', tasks: ['decision'], languages: ['en'], latency: 'low' }]);
  assert.equal(routed.model, 'm');
});
