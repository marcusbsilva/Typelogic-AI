// Evaluate page: the public JevBench decisions (embedded) or the user's JSONL, run through the loaded models.
// The server runs the evaluation in the background; this page polls /api/eval and shows the Decision Index, the
// metrics per tier, type and engine, the bias probes and the saved evaluations.
import { $, $button, $input, $select, api, downloadText, esc, reportError, toast } from './dom';

interface Group {
  questions: number; valid: number; correct: number; accuracy: number | null; accuracy_on_valid: number | null;
  chance: number | null; skill: number | null; brier: number | null; nll: number | null; ece_10: number | null;
  ordinal_mae: number | null; latency_p50_s: number | null; latency_p95_s: number | null;
}
interface IndexPart { tier: string; weight: number; questions: number; accuracy: number; chance: number; skill: number }
interface Summary {
  decision_index: { value: number | null; speed: number | null; tiers: IndexPart[]; formula: string; speed_formula: string; note: string };
  overall: Group; by_tier: Record<string, Group>; by_type: Record<string, Group>; by_engine: Record<string, Group>;
  selective: Record<string, { accepted: number; coverage: number | null; accuracy: number | null }>;
  probes: {
    order: { checked: number; top_choice_changed: number; flip_rate: number | null; mean_max_shift: number | null; max_shift: number | null };
    position: { questions: number; predicted_first: number | null; gold_first: number | null; predicted_last: number | null; gold_last: number | null; first_excess: number | null; last_excess: number | null };
    polarity: { questions: number; predicted_yes: number | null; gold_yes: number | null; yes_excess: number | null };
    leniency: { questions: number; mean_expected_level: number | null; mean_gold_level: number | null; excess: number | null };
    note: string;
  };
}
interface Job {
  id?: string; status: string; created?: string; done?: number; total?: number; elapsed_s?: number; error?: string | null;
  saved?: string | null; config?: Record<string, unknown>; summary?: Summary | null;
}
interface Saved { name: string; created: string; status: string; config: Record<string, unknown>; decision_index: number | null; accuracy: number | null; brier: number | null; questions: number | null }

const TYPE_LABELS: Record<string, string> = { noul: 'Yes / no', choice: 'Choice', score: 'Score' };
const ENGINE_LABELS: Record<string, string> = { generalist: 'Plumb', specialist: 'Specialist', clef: 'Clef-Flash', ensemble: 'Combined', failed: 'Failed', unknown: 'Unknown' };
const pct = (v: number | null | undefined, digits = 1) => v === null || v === undefined ? '–' : `${(100 * v).toFixed(digits)}%`;
const fix = (v: number | null | undefined, digits = 3) => v === null || v === undefined ? '–' : v.toFixed(digits);
const signed = (v: number | null | undefined) => v === null || v === undefined ? '–' : `${v >= 0 ? '+' : '−'}${Math.abs(100 * v).toFixed(1)} pt`;
let poll: number | undefined;
let watching = false;
let fileText: string | null = null;

function groupTable(title: string, groups: Record<string, Group>, labels: Record<string, string> = {}): string {
  const rows = Object.entries(groups).map(([name, g]) => `<tr><th scope="row">${esc(labels[name] || name)}</th><td>${g.questions}</td><td>${pct(g.accuracy)}</td><td>${pct(g.chance)}</td><td>${fix(g.skill)}</td><td>${fix(g.brier)}</td><td>${fix(g.ece_10)}</td><td>${fix(g.nll)}</td><td>${g.valid < g.questions ? `${g.questions - g.valid}` : '0'}</td></tr>`).join('');
  return `<h4>${esc(title)}</h4><div class="evidence-scroll"><table class="evidence-table eval-table"><thead><tr><th></th><th>Questions</th><th>Accuracy</th><th>Chance</th><th>Skill</th><th>Brier</th><th>ECE</th><th>NLL</th><th>Failed</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function probesHtml(p: Summary['probes']): string {
  const o = p.order, pos = p.position, pol = p.polarity, len = p.leniency;
  const line = (name: string, value: string, detail: string) => `<tr><th scope="row">${esc(name)}</th><td>${value}</td><td>${esc(detail)}</td></tr>`;
  return `<h4>Bias probes</h4><div class="evidence-scroll"><table class="evidence-table eval-table"><thead><tr><th>Probe</th><th>Result</th><th>What it measures</th></tr></thead><tbody>
    ${o.checked ? line('Option order', `${pct(o.flip_rate)} flipped`, `${o.top_choice_changed} of ${o.checked} Choice answers changed their top choice when the options were reordered; mean largest shift ${fix(o.mean_max_shift)}, max ${fix(o.max_shift)}`) : line('Option order', '–', 'Not run (enable the order probe)')}
    ${line('First option', signed(pos.first_excess), `chosen ${pct(pos.predicted_first)} of ${pos.questions} Choice questions with 3+ options; gold first ${pct(pos.gold_first)}`)}
    ${line('Last option', signed(pos.last_excess), `chosen ${pct(pos.predicted_last)}; gold last ${pct(pos.gold_last)}`)}
    ${line('Yes polarity', signed(pol.yes_excess), `Yes answered on ${pct(pol.predicted_yes)} of ${pol.questions} Yes / no questions; gold Yes ${pct(pol.gold_yes)}`)}
    ${line('Score leniency', len.excess === null ? '–' : `${len.excess >= 0 ? '+' : '−'}${Math.abs(len.excess).toFixed(2)} levels`, `mean expected level ${fix(len.mean_expected_level, 2)} vs gold ${fix(len.mean_gold_level, 2)} on ${len.questions} Score questions`)}
  </tbody></table></div><p class="micro">${esc(p.note)}. Positive values mean the models pick that option more often than the gold answers do.</p>`;
}

function renderSummary(job: Job): void {
  const box = $('eval-result');
  const s = job.summary;
  if (!s) { box.innerHTML = '<div class="empty">No results yet.<p>Start an evaluation to see the Decision Index, calibration and bias probes.</p></div>'; return; }
  const d = s.decision_index, o = s.overall;
  const tiers = d.tiers.map(t => `<tr><th scope="row">${esc(t.tier)}</th><td>${t.weight}</td><td>${t.questions}</td><td>${pct(t.accuracy)}</td><td>${pct(t.chance)}</td><td>${fix(t.skill)}</td></tr>`).join('');
  const sel = Object.entries(s.selective).map(([t, v]) => `<tr><th scope="row">≥ ${esc(t)}</th><td>${pct(v.coverage)}</td><td>${pct(v.accuracy)}</td><td>${v.accepted}</td></tr>`).join('');
  const partial = job.status === 'running' ? `<p class="notice">Partial results: ${job.done} of ${job.total} items answered.</p>` : job.status === 'stopped' ? `<p class="notice">Stopped after ${job.done} of ${job.total} items; the numbers cover only those.</p>` : '';
  box.classList.remove('empty');
  box.innerHTML = `${partial}<div class="metrics eval-metrics">
      <div class="metric"><strong id="decision-index">${d.value === null ? '–' : d.value.toFixed(1)}</strong><span>Decision Index (0–100)</span></div>
      <div class="metric"><strong>${pct(o.accuracy)}</strong><span>Accuracy · ${o.correct} of ${o.questions}${o.valid < o.questions ? ` · ${o.questions - o.valid} failed` : ''}</span></div>
      <div class="metric"><strong>${fix(o.brier)}</strong><span>Brier · ECE ${fix(o.ece_10)}</span></div>
      <div class="metric"><strong>${d.speed === null ? '–' : d.speed.toFixed(0)}</strong><span>Speed · p50 ${fix(o.latency_p50_s, 2)} s, p95 ${fix(o.latency_p95_s, 2)} s</span></div>
    </div>
    <details class="eval-formula"><summary>How the Decision Index is computed</summary><p>${esc(d.formula)}.</p><p>Speed: ${esc(d.speed_formula)}.</p><p>${esc(d.note)}.</p>
      <div class="evidence-scroll"><table class="evidence-table eval-table"><thead><tr><th>Tier</th><th>Weight</th><th>Questions</th><th>Accuracy</th><th>Chance</th><th>Skill (≥ 0)</th></tr></thead><tbody>${tiers}</tbody></table></div></details>
    ${groupTable('By tier', s.by_tier)}${groupTable('By question type', s.by_type, TYPE_LABELS)}${groupTable('By engine', s.by_engine, ENGINE_LABELS)}
    ${probesHtml(s.probes)}
    <h4>Selective accuracy</h4><div class="evidence-scroll"><table class="evidence-table eval-table"><thead><tr><th>Top probability</th><th>Coverage</th><th>Accuracy</th><th>Answers</th></tr></thead><tbody>${sel}</tbody></table></div>
    <p class="micro">Accuracy counts failed answers as wrong. Skill = (accuracy − chance) / (1 − chance), chance = mean of 1 / number of options. Brier is multi-class; ECE uses ten top-probability bins.${o.ordinal_mae !== null ? ` Ordinal MAE (Score): ${fix(o.ordinal_mae, 2)} levels.` : ''}</p>`;
}

function renderJob(job: Job): void {
  const running = job.status === 'running';
  $button('eval-start').disabled = running;
  $button('eval-stop').classList.toggle('hidden', !running);
  const progress = $('eval-progress');
  progress.classList.toggle('hidden', job.status === 'idle');
  const share = job.total ? (job.done || 0) / job.total : 0;
  ($('eval-bar').firstElementChild as HTMLElement).style.width = `${(100 * share).toFixed(1)}%`;
  const states: Record<string, string> = { running: 'Running', done: 'Finished', stopped: 'Stopped', idle: '' };
  $('eval-status').textContent = job.status === 'idle' ? '' : `${states[job.status] || job.status}: ${job.done} of ${job.total} items · ${Math.round(job.elapsed_s || 0)} s${job.saved ? ` · saved as data/evaluations/${job.saved}` : ''}${job.error ? ` · ${job.error}` : ''}`;
  renderSummary(job);
}

async function refreshJob(): Promise<Job> {
  const job = await api<Job>('/api/eval');
  renderJob(job);
  if (job.status === 'running') {
    clearTimeout(poll);
    poll = window.setTimeout(() => { refreshJob().catch(reportError); }, 1500);
  } else if (watching) {
    watching = false;
    loadSaved().catch(reportError);
  }
  return job;
}

async function loadSaved(): Promise<void> {
  const { evaluations } = await api<{ evaluations: Saved[] }>('/api/evaluations');
  const box = $('eval-saved');
  if (!evaluations.length) { box.innerHTML = '<p class="micro">No saved evaluation yet. Each finished run is saved in data/evaluations.</p>'; return; }
  const label = (c: Record<string, unknown>) => `${c.source === 'jevbench' ? 'JevBench' : 'File'} · ${ENGINE_LABELS[String(c.engine)] || 'Automatic'}${c.combine === 'ensemble' ? ' · combined' : ''}`;
  box.innerHTML = `<div class="evidence-scroll"><table class="evidence-table eval-table"><thead><tr><th>Date (UTC)</th><th>Run</th><th>Questions</th><th>Index</th><th>Accuracy</th><th>Brier</th><th></th></tr></thead><tbody>${evaluations.map(e => `<tr><td>${esc(e.created.slice(5, 16).replace('T', ' '))}</td><td>${esc(label(e.config || {}))}${e.status === 'stopped' ? ' (stopped)' : ''}</td><td>${e.questions ?? '–'}</td><td>${e.decision_index === null ? '–' : e.decision_index.toFixed(1)}</td><td>${pct(e.accuracy)}</td><td>${fix(e.brier)}</td><td><button type="button" class="text-button" data-eval="${esc(e.name)}" data-format="json">JSON</button> <button type="button" class="text-button" data-eval="${esc(e.name)}" data-format="jsonl">JSONL</button></td></tr>`).join('')}</tbody></table></div>`;
  box.querySelectorAll<HTMLButtonElement>('button[data-eval]').forEach(b => b.addEventListener('click', () => { downloadSaved(b.dataset.eval || '', b.dataset.format || 'json').catch(reportError); }));
}

async function downloadSaved(name: string, format: string): Promise<void> {
  const token = (document.querySelector('meta[name="typelogic-token"]') as HTMLMetaElement).content;
  const response = await fetch(`/api/evaluation?name=${encodeURIComponent(name)}&format=${format}`, { headers: { 'X-TypeLogic-Token': token } });
  if (!response.ok) throw Error(`Download failed (${response.status})`);
  downloadText(format === 'jsonl' ? name.replace(/\.json$/, '.jsonl') : name, await response.text(), format === 'jsonl' ? 'application/x-ndjson' : 'application/json');
}

function sourceChanged(): void {
  const file = $input('eval-source-file').checked;
  $('eval-tiers').classList.toggle('hidden', file);
  $('eval-file-line').classList.toggle('hidden', !file);
}

async function start(): Promise<void> {
  const file = $input('eval-source-file').checked;
  const tiers = Array.from(document.querySelectorAll<HTMLInputElement>('input[name="eval-tier"]:checked')).map(i => i.value);
  if (!file && !tiers.length) { toast('Choose at least one JevBench tier.'); return; }
  if (file && !fileText) { toast('Choose a JSONL file first.'); return; }
  const limit = $input('eval-limit').value.trim();
  const engine = $select('eval-engine').value;
  const body: Record<string, unknown> = {
    source: file ? 'file' : 'jevbench', engine, combine: engine === 'auto' ? $select('eval-combine').value : 'route',
    probes: $input('eval-probes').checked,
  };
  if (file) body.jsonl = fileText; else body.tiers = tiers;
  if (limit) {
    const n = Number(limit);
    if (!Number.isInteger(n) || n < 1) { toast('The limit is a whole number of items.'); return; }
    body.limit = n;
  }
  await api('/api/eval/start', body);
  watching = true;
  toast('Evaluation started. You can keep using TypeLogic; answers are slower meanwhile.');
  await refreshJob();
}

export async function loadEvaluate(): Promise<void> {
  const status = await api<{ engine: { engines?: string[] } }>('/api/status');
  const loaded = status.engine.engines || [];
  for (const option of Array.from($select('eval-engine').options)) {
    option.disabled = option.value !== 'auto' && !loaded.includes(option.value);
  }
  if ($select('eval-engine').selectedOptions[0]?.disabled) $select('eval-engine').value = 'auto';
  await Promise.all([refreshJob(), loadSaved()]);
}

export function installEvaluate(): void {
  $input('eval-source-jevbench').addEventListener('change', sourceChanged);
  $input('eval-source-file').addEventListener('change', sourceChanged);
  $select('eval-engine').addEventListener('change', () => { $select('eval-combine').disabled = $select('eval-engine').value !== 'auto'; });
  $input('eval-file').addEventListener('change', async () => {
    const f = $input('eval-file').files?.[0];
    fileText = f ? await f.text() : null;
    $('eval-file-info').textContent = f ? `${f.name}: ${fileText?.split('\n').filter(l => l.trim()).length} rows` : '';
  });
  $('eval-start').addEventListener('click', () => { start().catch(reportError); });
  $('eval-stop').addEventListener('click', () => { api('/api/eval/stop', {}).then(() => refreshJob()).catch(reportError); });
  sourceChanged();
}
