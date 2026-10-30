// End-to-end interface test: the real TypeLogic.exe server, library and Rust core with the deterministic fixture
// engine (first alternative 0.9), in headless Chromium.
// Run: cargo build --features fixture && node web/tests/ui.test.mjs
// (TYPELOGIC_EXE may point to the binary, PLAYWRIGHT_MODULE to a playwright install)
import { spawn, spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync, mkdirSync, readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import assert from 'node:assert/strict';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const here = dirname(fileURLToPath(import.meta.url));
const exe = process.env.TYPELOGIC_EXE || join(here, '../../target/debug/TypeLogic' + (process.platform === 'win32' ? '.exe' : ''));
const home = mkdtempSync(join(tmpdir(), 'typelogic-ui-'));
const data = join(home, 'data');
mkdirSync(data);
const LIMIT = 36;   // library limit of the test, so the archive dialog can be tested quickly
const server = spawn(exe, ['serve', '--fixture', '--no-browser', '--port', '0', '--data', data],
  { stdio: ['ignore', 'pipe', 'inherit'], env: { ...process.env, TYPELOGIC_HOME: home, TYPELOGIC_LIBRARY_LIMIT: String(LIMIT), TYPELOGIC_RAR: join(home, 'no-rar') } });
/** core.export_filename: eight Base32 characters of SHA-256("<id>:<format>"). */
function exportFilename(id, format) {
  const bytes = createHash('sha256').update(`${id}:${format}`, 'utf8').digest().subarray(0, 5);
  const alphabet = 'abcdefghijklmnopqrstuvwxyz234567';
  let bits = 0, value = 0, out = '';
  for (const b of bytes) { value = (value << 8) | b; bits += 8; while (bits >= 5) { out += alphabet[(value >>> (bits - 5)) & 31]; bits -= 5; } }
  return out + '.json';
}
const url = await new Promise((resolve, reject) => {
  server.stdout.once('data', chunk => resolve(String(chunk).trim()));
  server.once('exit', code => reject(new Error(`fixture server exited ${code}`)));
});

const results = [];
async function step(name, fn) {
  try { await fn(); results.push(['ok', name]); console.log('ok  ', name); }
  catch (error) { results.push(['FAIL', name]); console.log('FAIL', name, '\n    ', error.message); }
}

const browser = await chromium.launch();
const context = await browser.newContext({ acceptDownloads: true });
const page = await context.newPage();
const consoleErrors = [], external = [];
page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text()); });
page.on('pageerror', e => consoleErrors.push(String(e)));
page.on('request', r => { if (!r.url().startsWith(url)) external.push(r.url()); });

try {
  await page.goto(url + '/');
  const token = await page.getAttribute('meta[name="typelogic-token"]', 'content');
  const apiCall = (path, payload) => page.evaluate(async ([path, payload, token]) => {
    const r = await fetch(path, { method: payload ? 'POST' : 'GET', headers: { 'X-TypeLogic-Token': token, 'Content-Type': 'application/json' }, body: payload ? JSON.stringify(payload) : undefined });
    return { status: r.status, data: await r.json() };
  }, [path, payload, token]);
  await step('engine ready badge, English UI and no review controls', async () => {
    await page.waitForFunction(() => document.getElementById('engine-badge').textContent.startsWith('Local engine ready'));
    assert.equal(await page.textContent('#page-title'), 'New decision');
    assert.equal(await page.locator('.status-tile, input[name="review-mode"], #threshold, #approve-current').count(), 0);
    assert.equal(await page.textContent('#library-count'), '0');
  });
  await step('templates load case, tags, evidence and decisions', async () => {
    await page.waitForFunction(() => document.querySelectorAll('#template-select option').length > 10);
    await page.selectOption('#template-select', 'billing-duplicate-payment');
    assert.ok((await page.inputValue('#case-text')).length > 20);
    assert.equal(await page.inputValue('#case-title'), '');
    assert.deepEqual((await page.locator('#tag-chips .tag-chip').allTextContents()).map(t => t.replace('×', '')), ['billing', 'refund', 'customer']);
    assert.equal(await page.locator('.evidence-row').count(), 4);
    assert.equal(await page.locator('.question').count(), 3);
    assert.ok((await page.locator('.decision-card').first().locator('.type-tab.active').textContent()).includes('Choice'));
    assert.equal(await page.locator('.decision-card').first().locator('.option-row').count(), 4);
    assert.equal(await page.locator('.decision-card').first().locator('.opt-action, .auto-box').count(), 0);
    assert.equal(await page.locator('.decision-card').nth(1).locator('.type-tab.active').getAttribute('data-kind'), 'noul');
  });
  await step('blocks 1 to 4 have help; blocks 1 to 3 collapse and expand', async () => {
    for (const id of ['case-section', 'evidence-section', 'decisions-section']) {
      assert.equal(await page.locator(`#${id} > summary .help-icon`).count(), 1, id);
      const open = await page.evaluate(id => document.getElementById(id).open, id);
      await page.click(`#${id} > summary .collapse-chevron`);
      assert.equal(await page.evaluate(id => document.getElementById(id).open, id), !open, id);
      await page.click(`#${id} > summary .collapse-chevron`);
      assert.equal(await page.evaluate(id => document.getElementById(id).open, id), open, id);
    }
    assert.equal(await page.locator('.result-panel .block-head .help-icon').count(), 1);
    await page.click('#case-section > summary .help-icon');                 // help opens without collapsing the block
    assert.equal(await page.isHidden('#help-popup'), false);
    assert.ok((await page.textContent('#help-popup')).includes('title'));
    assert.equal(await page.evaluate(() => document.getElementById('case-section').open), true);
    await page.keyboard.press('Escape');
    assert.ok((await page.textContent('#decisions-hint')).startsWith('3 decisions'));
  });
  await step('tag field suggests while typing and accepts your own tags', async () => {
    await page.click('#tag-entry');
    await page.fill('#tag-entry', 'fra');
    await page.waitForSelector('#tag-suggestions:not(.hidden) .tag-option[data-tag="fraud"]');
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('Enter');
    await page.fill('#tag-entry', 'VIP   Customer');
    assert.ok((await page.textContent('#tag-suggestions .tag-option.own')).includes('vip customer'));
    await page.keyboard.press('Enter');
    await page.fill('#tag-entry', 'temp');
    await page.keyboard.press('Enter');
    await page.keyboard.press('Backspace');                                  // removes the last chip
    assert.deepEqual((await page.locator('#tag-chips .tag-chip').allTextContents()).map(t => t.replace('×', '')), ['billing', 'refund', 'customer', 'fraud', 'vip customer']);
    await page.locator('#tag-chips .tag-chip', { hasText: 'fraud' }).locator('.tag-remove').click();
    assert.equal(await page.locator('#tag-chips .tag-chip').count(), 4);
  });
  await step('preview uses the same request builder', async () => {
    await page.locator('summary', { hasText: 'Local integration' }).click();
    await page.click('#preview-request');
    const request = JSON.parse(await page.textContent('#request-output'));
    assert.ok(typeof request.case === 'string' && request.decisions.length === 3 && request.space === 'default' && request.template === 'billing-duplicate-payment');
    assert.ok(request.decisions.every(d => !('name' in d) && !('auto_review' in d)) && request.decisions[0].criteria['0'] && request.decisions[0].abstain_option === '3');
    assert.deepEqual(request.tags, ['billing', 'refund', 'customer', 'vip customer']);
    assert.ok(!('title' in request) && !('threshold' in request) && !('review_mode' in request));
    assert.deepEqual(request.evidence.refund_requested, { value: true, source: 'template', status: 'confirmed' });
  });
  await step('evaluate generates a title and shows tags, answers and integrity', async () => {
    await page.click('#evaluate');
    await page.waitForSelector('.result-card');
    assert.ok((await page.textContent('#results')).includes('Answer:'));
    assert.ok((await page.textContent('.integrity-status')).includes('SHA-512 and MD5 match'));
    assert.ok((await page.textContent('#latency')).includes('12.5 ms'));
    const title = await page.textContent('#results .decision-title');
    assert.ok(title.includes('Title taken from the case') && title.length > 30, title);
    assert.deepEqual(await page.locator('#results .tag-pill').allTextContents(), ['billing', 'refund', 'customer', 'vip customer']);
    assert.equal(await page.locator('#results #approve-current, #results .rule-outcome, #results .auto-review').count(), 0);
    assert.equal(await page.textContent('#library-count'), '1');
  });
  await step('help opens on click and closes on Escape', async () => {
    await page.locator('.result-card .help-icon').first().click();
    assert.equal(await page.isHidden('#help-popup'), false);
    assert.ok((await page.textContent('#help-popup')).includes('exp(logit'));
    await page.keyboard.press('Escape');
    assert.equal(await page.isHidden('#help-popup'), true);
  });
  let decisionId;
  await step('export Lite uses the eight-character Base32 filename and holds title and tags', async () => {
    decisionId = (await page.textContent('#results .record-id')).trim();
    const [download] = await Promise.all([page.waitForEvent('download'), page.click('#export-lite-current')]);
    assert.match(download.suggestedFilename(), /^[a-z2-7]{8}\.json$/);
    assert.equal(exportFilename(decisionId, 'lite'), download.suggestedFilename());
    const lite = JSON.parse((await import('node:fs')).readFileSync(await download.path(), 'utf8'));
    assert.deepEqual(Object.keys(lite), ['title', 'tags', 'questions']);
    process.env.EXPECTED_NAME = download.suggestedFilename();
  });
  await step('a typed title is kept', async () => {
    await page.fill('#case-title', '  Duplicate   charge on order 1182 ');
    await page.click('#evaluate');
    await page.waitForFunction(() => document.querySelector('#results .decision-title')?.textContent === 'Duplicate charge on order 1182');
  });
  await step('Library filters by tag and the detail saves a correction', async () => {
    await page.click('.nav[data-page="library"]');
    assert.equal(await page.textContent('#page-title'), 'Library');
    await page.waitForFunction(() => document.querySelectorAll('.library-card').length === 2);
    const chips = await page.locator('#filter-tags .filter-tag').allTextContents();
    assert.deepEqual(chips.map(c => c.replace(/\s+/g, ' ').trim()), ['All 2', 'billing 2', 'refund 2', 'customer 2', 'vip customer 2']);
    assert.ok(await page.isVisible('#filter-tags .filter-tag.own[data-filter="vip customer"]'));
    assert.ok((await page.textContent('.library-card')).includes('Duplicate charge on order 1182'));
    await page.click('.filter-tag[data-filter="vip customer"]');
    assert.equal(await page.locator('.library-card').count(), 2);
    assert.equal(await page.locator('.library-card .status-pill').count(), 0);
    await page.locator('.library-card h3').first().click();
    await page.waitForSelector('#library-detail:not(.hidden) .result-card');
    assert.ok(await page.isHidden('#library-list'));
    assert.equal(await page.locator('#library-detail .detail-approve, #library-detail .detail-deny, #library-detail .detail-clear').count(), 0);
    await page.click('#library-detail .detail-correction');
    await page.click('#library-detail .save-feedback');
    await page.waitForFunction(() => document.getElementById('toast').textContent === 'Correction saved');
    await page.click('#library-back');
    assert.ok(await page.isVisible('#library-grid'));
    await page.click('.filter-tag[data-filter="all"]');
  });
  await step('training export downloads the corrected decision as JSON Lines', async () => {
    await page.click('details.menu > summary');
    const [download] = await Promise.all([page.waitForEvent('download'), page.click('#export-training')]);
    assert.match(download.suggestedFilename(), /^typelogic-training-default\.jsonl$/);
    const rows = (await import('node:fs')).readFileSync(await download.path(), 'utf8').trim().split('\n').map(line => JSON.parse(line));
    assert.equal(rows.length, 1);
    assert.ok(Object.values(rows[0].provenance).includes('correction'), JSON.stringify(rows[0].provenance));
    assert.ok(rows[0].group_id.startsWith('state-') && rows[0].state && rows[0].targets);
  });
  await step('grid shows 30 decisions per page with page buttons', async () => {
    for (let i = 0; i < 31; i++) {
      const r = await apiCall('/api/evaluate', { case: `Case number ${i} about a late delivery to the customer.`, tags: i % 2 ? ['logistics'] : ['logistics', 'urgent'], decisions: [{ output: 'boolean', instructions: 'Late?' }] });
      assert.equal(r.status, 200, JSON.stringify(r.data));
    }
    await page.click('#refresh-history');
    await page.waitForFunction(() => document.querySelectorAll('.library-card').length === 30);
    assert.ok(await page.isVisible('#library-pager'));
    assert.equal(await page.textContent('#library-range'), 'Showing 1–30 of 33');
    assert.deepEqual(await page.locator('#library-pager .page-number').allTextContents(), ['1', '2']);
    await page.click('#library-pager .page-number[data-page="2"]');
    await page.waitForFunction(() => document.querySelectorAll('.library-card').length === 3);
    assert.equal(await page.textContent('#library-range'), 'Showing 31–33 of 33');
    assert.ok(await page.isDisabled('#library-pager .page-step[aria-label="Next page"]'));
    await page.click('#library-pager .page-step[aria-label="Previous page"]');
    await page.waitForFunction(() => document.querySelectorAll('.library-card').length === 30);
    await page.click('.filter-tag[data-filter="urgent"]');
    await page.waitForFunction(() => document.querySelectorAll('.library-card').length === 16);
    assert.ok(await page.isHidden('#library-pager'));
    await page.click('.filter-tag[data-filter="all"]');
  });
  await step('lowest confidence first orders the grid', async () => {
    await page.selectOption('#library-sort', 'confidence');
    assert.match(await page.textContent('.library-card .card-foot'), /Lowest confidence \d+%/);
    await page.selectOption('#library-sort', 'oldest');
    assert.ok((await page.textContent('.library-card h3')).length > 10);
    await page.selectOption('#library-sort', 'newest');
  });
  await step('bulk delete through the selection bar', async () => {
    assert.ok(await page.isHidden('#selection-bar'));
    await page.locator('.library-card .select-request').nth(0).check();
    await page.locator('.library-card .select-request').nth(1).check();
    assert.ok(await page.isVisible('#selection-bar'));
    assert.equal(await page.textContent('#selection-count'), '2 selected');
    assert.equal(await page.locator('#selection-bar button').count(), 2);       // Delete and Cancel only
    await page.click('#bulk-delete');
    await page.waitForFunction(() => document.getElementById('library-range').textContent === 'Showing 1–30 of 31');
    await page.waitForFunction(() => document.getElementById('library-count').textContent === '31');
  });
  await step('a full library asks before archiving into the data folder', async () => {
    for (let i = 31; i < LIMIT; i++) assert.equal((await apiCall('/api/evaluate', { case: 'Filler case to reach the library limit.', decisions: [{ output: 'boolean', instructions: 'Filler?' }] })).status, 200);
    const full = await apiCall('/api/evaluate', { case: 'One too many.', decisions: [{ output: 'boolean', instructions: 'More?' }] });
    assert.equal(full.status, 409); assert.equal(full.data.code, 'library_full');
    await page.click('.nav[data-page="decide"]');
    await page.waitForFunction(n => document.getElementById('library-room').textContent.startsWith(`${n} of ${n}`), LIMIT);
    await page.click('#evaluate');
    await page.waitForSelector('#archive-dialog[open]');
    const text = await page.textContent('#archive-text');
    assert.ok(text.includes(`${LIMIT} decisions`) && /\d{2}-\d{2}-\d{4}\.zip/.test(text), text);
    await page.click('#archive-cancel');
    await page.waitForFunction(() => document.getElementById('toast').textContent.startsWith('Not evaluated'));
    assert.equal(await page.textContent('#library-count'), String(LIMIT));
    await page.click('#evaluate');
    await page.waitForSelector('#archive-dialog[open]');
    await page.click('#archive-confirm');
    await page.waitForFunction(() => document.querySelector('#results .decision-title'));
    await page.waitForFunction(() => document.getElementById('library-count').textContent === '1');
    const files = (await import('node:fs')).readdirSync(join(data, 'archives'));
    assert.equal(files.length, 1); assert.match(files[0], /^\d{2}-\d{2}-\d{4}\.zip$/);
  });
  await step('evidence extraction suggests values that must be kept', async () => {
    await page.selectOption('#template-select', 'ticket-routing');
    await page.uncheck('#template-load-values');
    await page.selectOption('#template-select', 'billing-duplicate-payment');
    await page.selectOption('#template-select', 'ticket-routing');
    assert.equal(await page.inputValue('#case-text'), '');
    await page.fill('#case-text', 'My payout has failed three days in a row and support chat keeps timing out. I need this fixed today.');
    await page.click('#extract-evidence');
    await page.waitForFunction(() => document.querySelectorAll('.ev-status.unverified').length === 3);
    assert.ok((await page.textContent('#extract-status')).includes('fields suggested'));
    await page.click('#preview-request');
    const request = JSON.parse(await page.textContent('#request-output'));
    assert.deepEqual(request.evidence.payout_failed_days, { value: 3, source: 'extracted', status: 'unverified', check: 'in_text' });
    await page.click('#keep-all-evidence');
    assert.equal(await page.locator('.ev-status.unverified').count(), 0);
    await page.check('#template-load-values');
  });
  await step('decision editor builds Yes / no, Choice and Score in the Jev style', async () => {
    await page.selectOption('#template-select', '');
    assert.equal(await page.locator('#tag-chips .tag-chip').count(), 0);
    const tabs = await page.locator('.decision-card').first().locator('.type-tab').allTextContents();
    assert.deepEqual(tabs.map(t => t.trim()), ['Yes / no', 'Choice', 'Score']);
    assert.equal(await page.locator('.decision-card').first().locator('.decision-head .help-icon').count(), 1);
    assert.equal(await page.locator('.crit-true, .crit-false, .auto-threshold, .score-level').count(), 0);
    assert.equal(await page.evaluate(() => document.getElementById('evidence-section').open), false);
    await page.fill('#case-text', 'Task: delete rows <img src=x onerror="window.pwned=1">');
    await page.fill('#case-title', '<b>Rows</b>');
    const yesNo = page.locator('.decision-card').nth(0);
    await yesNo.locator('.instructions').fill('Is this action safe?');
    await page.click('#add-question');
    const choice = page.locator('.decision-card').nth(1);
    await choice.locator('.type-tab[data-kind="choice"]').click();
    await choice.locator('.instructions').fill('Which team?');
    await choice.locator('.opt-desc').nth(0).fill('Payments, payouts, refunds');
    await choice.locator('.opt-desc').nth(1).fill('<b>Not enough information</b>');
    assert.deepEqual(await choice.locator('.opt-index').allTextContents(), ['0', '1']);
    await choice.locator('.abstain').selectOption('1');
    await page.click('#add-question');
    const score = page.locator('.decision-card').nth(2);
    await score.locator('.type-tab[data-kind="score"]').click();
    await score.locator('.instructions').fill('How ready is this lead to buy?');
    for (const [i, text] of ['Browsing', 'Evaluating', 'Ready to buy'].entries()) await score.locator('.level-text').nth(i).fill(text);
    await score.locator('.add-level').click();
    await score.locator('.level-text').nth(3).fill('Urgent');
    assert.deepEqual(await score.locator('.level-index').allTextContents(), ['0', '1', '2', '3']);
    await page.click('#preview-request');
    const request = JSON.parse(await page.textContent('#request-output'));
    assert.equal(request.case, 'Task: delete rows <img src=x onerror="window.pwned=1">');
    assert.deepEqual(request.decisions.map(d => d.output), ['boolean', 'choice', 'score']);
    assert.deepEqual(request.decisions[0], { output: 'boolean', instructions: 'Is this action safe?' });
    assert.deepEqual(request.decisions[1], { output: 'choice', instructions: 'Which team?', criteria: { 0: 'Payments, payouts, refunds', 1: '<b>Not enough information</b>' }, abstain_option: '1' });
    assert.deepEqual(request.decisions[2], { output: 'score', instructions: 'How ready is this lead to buy?', criteria: ['Browsing', 'Evaluating', 'Ready to buy', 'Urgent'] });
    await page.click('#evaluate');
    try { await page.waitForFunction(() => document.querySelectorAll('#results .result-card').length === 3 && document.querySelector('#results .result-name')?.textContent.startsWith('1. Is this action safe?'), null, { timeout: 8000 }); } catch (e) { throw Error('results: ' + (await page.textContent('#results')).slice(0, 300)); }
    const firstName = await page.textContent('#results .result-card .result-name');
    assert.ok(firstName.startsWith('1. Is this action safe?'), firstName);
    assert.equal(await page.textContent('#results .decision-title'), '<b>Rows</b>');
    assert.ok((await page.textContent('#results')).includes('Confidence'));
    assert.equal(await page.evaluate(() => window.pwned), undefined);
  });
  await step('a problem inside a collapsed block opens it', async () => {
    await page.locator('.decision-card').nth(0).locator('.instructions').fill('');
    await page.click('#decisions-section > summary .collapse-chevron');
    assert.equal(await page.evaluate(() => document.getElementById('decisions-section').open), false);
    await page.click('#evaluate');
    await page.waitForFunction(() => document.getElementById('decisions-section').open && document.querySelector('#results .error-box')?.textContent.includes('Decision 1'));
    await page.locator('.decision-card').nth(0).locator('.instructions').fill('Is this action safe?');
  });
  await step('engine page shows the models, the routing and the Rust core', async () => {
    await page.click('.nav[data-page="settings"]');
    const text = await page.textContent('#engine-profile');
    assert.ok(text.includes('llama.cpp') && text.includes('Plumb') && /Routing\s*Up to 16 options: Plumb · more: specialist/.test(text), text);
    assert.ok(/Deterministic core\s*Rust/.test(text), text);
    await page.waitForFunction(() => !document.getElementById('needle-settings').textContent.includes('Checking'));
    assert.ok(/not installed|no Needle entry/.test(await page.textContent('#needle-settings')));
  });
  await step('engine mode selector saves Automatic or CPU only', async () => {
    await page.waitForFunction(() => document.getElementById('mode-current').textContent.includes('Current mode'));
    assert.ok(await page.isChecked('input[name="engine-mode"][value="auto"]'));
    assert.ok((await page.textContent('#mode-plan')).includes('fixture'));
    await page.check('input[name="engine-mode"][value="cpu"]');
    await page.click('#apply-mode');
    await page.waitForFunction(() => document.getElementById('engine-badge').textContent.endsWith('CPU only'));
    await page.waitForFunction(() => document.getElementById('mode-current').textContent.includes('CPU only'));
    await page.check('input[name="engine-mode"][value="auto"]');
    await page.click('#apply-mode');
    await page.waitForFunction(() => document.getElementById('engine-badge').textContent.endsWith('Automatic'));
  });
  await step('Combine is saved without reloading the models', async () => {
    assert.ok(await page.isChecked('input[name="engine-combine"][value="route"]'));
    await page.check('input[name="engine-combine"][value="ensemble"]');
    await page.click('#apply-mode');
    await page.waitForFunction(() => document.getElementById('mode-status').textContent.includes('The models stay loaded'));
    const info = await page.evaluate(() => fetch('/api/modes', { headers: { 'X-TypeLogic-Token': document.querySelector('meta[name="typelogic-token"]').content } }).then(r => r.json()));
    assert.equal(info.settings.combine, 'ensemble');
    assert.equal(info.engine_status, 'ready');
    await page.check('input[name="engine-combine"][value="route"]');
    await page.click('#apply-mode');
    await page.waitForFunction(() => document.getElementById('mode-status').textContent.includes('routed'));
  });
  await step('Evaluate runs JevBench and saves the result', async () => {
    const before = Number(await page.textContent('#library-count'));
    await page.click('.nav[data-page="evaluate"]');
    try { await page.waitForFunction(() => document.getElementById('eval-saved').textContent.includes('No saved evaluation'), null, { timeout: 8000 }); }
    catch (e) { throw Error(`saved ${await page.textContent('#eval-saved')}, toast ${await page.textContent('#toast')}, hidden ${await page.isHidden('#page-evaluate')}`); }
    assert.ok(await page.evaluate(() => document.querySelector('#eval-engine option[value="clef"]').disabled));
    await page.fill('#eval-limit', '30');
    await page.click('#eval-start');
    try { await page.waitForFunction(() => document.getElementById('eval-status').textContent.startsWith('Finished: 30 of 30'), null, { timeout: 30000 }); }
    catch (e) { throw Error(`status ${await page.textContent('#eval-status')}, toast ${await page.textContent('#toast')}`); }
    const index = await page.textContent('#decision-index');
    assert.ok(/^\d+\.\d$/.test(index), index);
    const text = await page.textContent('#eval-result');
    assert.ok(text.includes('By question type') && text.includes('Yes polarity') && text.includes('First option'), text.slice(0, 300));
    await page.waitForFunction(() => document.querySelectorAll('#eval-saved tbody tr').length === 1);
    if (process.env.SHOT_DIR) await page.screenshot({ path: process.env.SHOT_DIR + '/evaluate.png', fullPage: true });
    const download = page.waitForEvent('download');
    await page.click('#eval-saved button[data-format="jsonl"]');
    const rows = readFileSync(await (await download).path(), 'utf8').trim().split('\n').map(l => JSON.parse(l));
    assert.equal(rows.length, 30);
    assert.ok('correct' in rows[0] && 'probabilities' in rows[0] && rows[0].gold);
    assert.equal(Number(await page.textContent('#library-count')), before);
  });
  await step('Evaluate reads a JSONL file and reports its problems', async () => {
    await page.check('#eval-source-file');
    await page.setInputFiles('#eval-file', { name: 'bad.jsonl', mimeType: 'application/json', buffer: Buffer.from('{"id": "x", "state": "s", "question": {"type": "noul", "instructions": "Is it?"}, "expected": "maybe"}\n') });
    await page.click('#eval-start');
    await page.waitForFunction(() => document.getElementById('toast').textContent.includes('line 1'));
    const good = ['{"id": "a", "state": "The parcel arrived broken.", "question": {"type": "noul", "instructions": "Is the parcel damaged?"}, "expected": "yes"}',
      '{"id": "b", "case": "Refund my second charge.", "questions": {"team": {"type": "choice", "instructions": "Which team?", "criteria": ["billing", "shipping"]}}, "gold": {"team": "billing"}}'].join('\n');
    await page.setInputFiles('#eval-file', { name: 'mine.jsonl', mimeType: 'application/json', buffer: Buffer.from(good) });
    await page.waitForFunction(() => document.getElementById('eval-file-info').textContent.includes('2 rows'));
    await page.fill('#eval-limit', '');
    await page.click('#eval-start');
    await page.waitForFunction(() => document.getElementById('eval-status').textContent.startsWith('Finished: 2 of 2'));
    // the fixture answers the first option: yes is the second key of Yes / no, billing the first Choice option
    assert.ok((await page.textContent('#eval-result')).includes('1 of 2'));
    await page.waitForFunction(() => document.querySelectorAll('#eval-saved tbody tr').length === 2);
    await page.check('#eval-source-jevbench');
  });
  await step('delete removes every decision from the Library', async () => {
    await page.click('.nav[data-page="library"]');
    await page.waitForFunction(() => document.querySelectorAll('.library-card').length > 0 && document.querySelectorAll('.library-card').length === Number(document.getElementById('library-count').textContent));
    for (let left = await page.locator('.library-card').count(); left > 0; left--) {
      await page.locator('.library-card h3').first().click();
      await page.click('#library-detail .detail-delete');
      try { await page.waitForFunction(n => document.querySelectorAll('.library-card').length === n && !document.getElementById('library-list').classList.contains('hidden'), left - 1, { timeout: 8000 }); }
      catch (e) { throw Error(`left ${left}: cards ${await page.locator('.library-card').count()}, list hidden ${await page.isHidden('#library-list')}, toast ${await page.textContent('#toast')}`); }
    }
    await page.waitForFunction(() => document.getElementById('library-count').textContent === '0' && document.querySelector('.library-empty'));
  });
  await step('dark theme and mobile layout without horizontal overflow', async () => {
    await page.selectOption('#theme-select', 'dark');
    assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), 'dark');
    await page.setViewportSize({ width: 375, height: 800 });
    await page.click('.nav[data-page="decide"]');
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1));
    await page.click('.nav[data-page="library"]');
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1));
  });
  await step('no console errors and no external requests', async () => {
    assert.deepEqual(consoleErrors.filter(e => !e.includes('409') && !e.includes('422')), []);  // 409: library full, 422: the bad JSONL file
    assert.deepEqual(external, []);
  });
} finally {
  await browser.close();
  server.kill();
  rmSync(home, { recursive: true, force: true });
}
const failed = results.filter(r => r[0] === 'FAIL').length;
console.log(`${results.length - failed}/${results.length} UI checks passed` + (process.env.EXPECTED_NAME ? ` (export ${process.env.EXPECTED_NAME})` : ''));
process.exit(failed ? 1 : 0);
