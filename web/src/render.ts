import type { Answer, ChoiceCriteria, DecisionResponse, EngineHealth, Integrity, QuestionSpec } from './types';
import { $, api, esc, exportDecision, reportError, toast } from './dom';
import { helpIcon, helpTexts } from './help';
import { refreshStatus } from './status';
import { CHECK_LABELS } from './needle';

// Result, identity and engine rendering. Formulas shown are the ones the engine actually applies.

let current: DecisionResponse | null = null;

/** ID of the decision shown on the New decision page, if any. */
export const currentDecisionId = (): string | undefined => current?.decision_id;

const reasons: Record<string, string> = {
  below_threshold: 'Below threshold', abstention_not_declared: 'Abstention not declared', explicit_abstention: 'The model selected abstention',
  option_order_sensitive: 'Sensitive to option order', order_check_disabled: 'Order check disabled',
  specialist_order_robustness_not_verified: 'Order not audited in the specialist',
  model_extracted_fields: 'Model-extracted fields (unverified)', unconfirmed_evidence: 'Extracted evidence not confirmed', missing_required_evidence: 'Required evidence missing',
};

/** Answers in the declared order: stored records keep JSON keys sorted, so the request's decision order is used. */
export function orderedAnswers(answers: Record<string, Answer>, request?: Record<string, unknown>): [string, Answer][] {
  const order = ((request?.case_request as { decision_order?: string[] } | undefined)?.decision_order) || [];
  const rank = (id: string) => { const i = order.indexOf(id); const n = /^(?:question|decision)_(\d+)$/.exec(id); return i >= 0 ? i : n ? Number(n[1]) : 1e6; };
  return Object.entries(answers).sort((a, b) => rank(a[0]) - rank(b[0]));
}

export function questionTitle(id: string, a: Answer, questions: Record<string, QuestionSpec> | undefined, index: number): string {
  const number = /^(?:question|decision)_(\d+)$/.exec(id)?.[1] || index + 1;
  return `${number}. ${a.instructions || questions?.[id]?.instructions || id}`;
}

function choiceCriteriaOf(id: string, a: Answer, questions?: Record<string, QuestionSpec>): ChoiceCriteria {
  const own = a.criteria && typeof a.criteria === 'object' && !Array.isArray(a.criteria) ? a.criteria : undefined;
  const fallback = questions?.[id]?.criteria;
  return own || (fallback && !Array.isArray(fallback) ? fallback : undefined) || {};
}

export function resultHTML(id: string, a: Answer, questions: Record<string, QuestionSpec> = {}, index = 0): string {
  const entries = Object.entries(a.probabilities);
  const best = Math.max(...entries.map(([, v]) => v));
  const winner = (entries.find(([, p]) => p === best) as [string, number])[0];
  const binaryLabel = winner === 'true' ? 'Yes' : 'No';
  const value = a.type === 'choice' ? a.choice : a.type === 'noul' ? binaryLabel : (a.legend?.[winner] || winner);
  // human_review_required is always present and below_threshold refers to the engine's candidate threshold: both are
  // left out; the other flags (abstention, order sensitivity, unconfirmed evidence, ...) are shown as notes.
  const warnings = a.review_reasons.filter(r => r !== 'human_review_required' && r !== 'below_threshold');
  const choiceCriteria = choiceCriteriaOf(id, a, questions);
  const optionLabel = (key: string): string => a.type === 'choice'
    ? (choiceCriteria[key] ? `${key} — ${choiceCriteria[key]}` : key)
    : (a.legend?.[key] || ({ false: 'No', true: 'Yes' } as Record<string, string>)[key] || key);
  const chanceLabel = a.type === 'noul' ? binaryLabel : a.type === 'choice' ? optionLabel(a.choice as string) : (a.legend?.[winner] || winner);
  const chanceHelp = `Confidence is the model's probability for the most likely alternative (as in Jev), not a measured accuracy rate.\nThe decision head transforms logits into weights: pᵢ = exp(logitᵢ) / Σ exp(logitⱼ), with temperature applied by the engine. The contract normalizes the distribution.\nPresented distribution: ${entries.map(([k, p]) => `${k}: ${p.toFixed(6)}`).join('; ')}.\nConfidence = 100 × ${best.toFixed(6)} = ${(best * 100).toFixed(1)}%, rounded to one decimal. Largest weight: ${chanceLabel}. This does not disclose internal reasoning or verify the facts.`;
  const n = entries.length;
  const concentrationHelp = `Concentration measures how far the largest probability is above an even distribution, not the probability that the answer is correct.\n1. Alternatives: n = ${n}; uniform = 1/n = ${(1 / n).toFixed(6)}.\n2. Largest probability = ${best.toFixed(6)}.\n3. C = (max(p) − 1/n) / (1 − 1/n).\n4. C = (${best.toFixed(6)} − ${(1 / n).toFixed(6)}) / ${(1 - 1 / n).toFixed(6)} = ${a.concentration.toFixed(6)}; 100 × C = ${(a.concentration * 100).toFixed(1)}%.\n0% is uniform; 100% puts all weight on one alternative. Accuracy validation requires independent labeled cases.`;
  const score = a.score ?? 0;
  const scoreHelp = a.type === 'score' ? `Ordinal score: levels use indices starting at zero. Expected index = Σ index × probability.\n${entries.map(([k, p]) => `${k} × ${p.toFixed(6)}`).join(' + ')} = ${score.toFixed(6)}.\nThe highlighted answer is the most likely level; the expected index can lie between levels. Indices are a convention, not a physical measurement.` : '';
  const bars = entries.map(([key, p]) => `<div class="probability ${p === best ? 'top' : ''}"><span>${esc(optionLabel(key))}</span><div class="bar"><span style="width:${Math.min(100, p * 100)}%"></span></div><span>${(p * 100).toFixed(1)}%</span></div>`).join('');
  // Numbered options (0, 1, 2, ...): show the description as the answer and the number underneath.
  const numbered = a.type === 'choice' && value !== undefined && /^\d+$/.test(value) && !!choiceCriteria[value];
  const description = numbered ? `<p class="answer-description">Option ${esc(value)}</p>`
    : a.type === 'choice' && value !== undefined && choiceCriteria[value] ? `<p class="answer-description">${esc(choiceCriteria[value])}</p>` : '';
  const engineLabel = a.engine === 'generalist' ? 'Plumb-4B (generalist, direct option-letter readout)' : a.engine === 'specialist' ? 'Specialist (TypeLogic Qwen3.5-2B, trained decision head)' : a.engine === 'clef' ? 'Clef-Flash (9B decision model)' : a.engine === 'ensemble' ? 'Ensemble (two models, weighted product of probabilities)' : '';
  const engine = engineLabel ? `<p>Engine: ${esc(engineLabel)}</p>` : '';
  const order = a.order_diagnostic?.checked ? `<p>Order: ${a.order_diagnostic.unstable ? 'sensitivity detected' : 'no relevant change in two permutations'} ${helpIcon(helpTexts.order, 'Explain order check')}</p>` : '';
  return `<article class="result-card"><h4 class="result-name">${esc(questionTitle(id, a, questions, index))}</h4><div class="result-value"><span class="answer-label">Answer:</span> ${esc(numbered ? choiceCriteria[value as string] : value)}</div>${description}${a.abstained ? '<span class="status-tag">Not enough information</span>' : ''}${bars}<div class="result-details"><p>Confidence: <strong>${(best * 100).toFixed(1)}% = ${esc(chanceLabel)}</strong> ${helpIcon(chanceHelp, 'Explain confidence')}</p>${a.type === 'score' ? `<p>Calculated score: ${score.toFixed(2)} / ${n - 1} ${helpIcon(scoreHelp, 'Explain score')}</p>` : ''}<p>Concentration: ${(a.concentration * 100).toFixed(1)}% · no accuracy guarantee ${helpIcon(concentrationHelp, 'Explain concentration')}</p>${engine}${order}${warnings.length ? `<p class="flags">${warnings.map(r => esc(reasons[r] || r)).join(' · ')}</p>` : ''}</div></article>`;
}

/** Tags as chips (escaped). */
export function tagChips(tags: string[] | undefined, cls = 'tag-list'): string {
  return tags?.length ? `<ul class="${cls}">${tags.map(t => `<li class="tag-pill">${esc(t)}</li>`).join('')}</ul>` : '';
}

export function identityHTML(record: { integrity?: Integrity; created?: string; id?: string; decision_id?: string; title?: string; tags?: string[]; title_source?: string }): string {
  const integrity = record.integrity || {}, created = record.created, date = new Date(created as string);
  const formatted = Number.isNaN(date.getTime()) ? 'Unknown date' : date.toLocaleString('en-GB', { dateStyle: 'long', timeStyle: 'medium' });
  const migrated = integrity.baseline_origin === 'migrated_existing' ? '<p class="micro">Baseline established from an existing record during upgrade; its original content was not previously checksummed.</p>' : '';
  const generated = record.title_source && record.title_source !== 'user'
    ? `<span class="title-source">${record.title_source.startsWith('model:') ? `Title written by ${esc(record.title_source.slice(6))}` : 'Title taken from the case'}</span>` : '';
  const heading = record.title ? `<h3 class="decision-title">${esc(record.title)}${generated}</h3>${tagChips(record.tags)}` : '';
  return `<section class="decision-identity">${heading}<p class="decision-date">Created: <time datetime="${esc(created || '')}">${esc(formatted)}</time></p><p><strong>Decision ID:</strong> <code class="record-id">${esc(record.id || record.decision_id)}</code></p><p class="integrity-status ${integrity.verified ? 'verified' : 'failed'}">Integrity: ${integrity.verified ? 'SHA-512 and MD5 match the stored baseline' : 'verification failed'} ${helpIcon(helpTexts.integrity, 'Explain record integrity')}</p><dl class="checksums"><dt>SHA-512</dt><dd>${esc(integrity.sha512 || 'Unavailable')}</dd><dt>MD5</dt><dd>${esc(integrity.md5 || 'Unavailable')}</dd></dl>${migrated}</section>`;
}

export function exportGuide(history = false): string {
  return `<div class="action-guide"><p><strong>Delete:</strong> removes the decision with its corrections and audit entries.</p>${history ? '<p><strong>Record correction:</strong> saves the observed alternative without retraining.</p>' : ''}<p><strong>Export Full JSON:</strong> downloads the stored decision with title, tags, case, evidence, technical answers, probabilities, unique ID and checked digests. <strong>Export Lite JSON:</strong> downloads only the title, tags, question text and typed answers; no case, probabilities, hashes or audit data. Single-decision downloads use stable eight-character filenames based on their ID and format.</p></div>`;
}

export function renderResult(data: DecisionResponse): void {
  current = data;
  $('machine-output').textContent = JSON.stringify(data, null, 2);
  $('latency').textContent = typeof data.latency_ms === 'number' ? `${data.latency_ms.toLocaleString('en-US')} ms` : '';
  const results = $('results');
  results.classList.remove('empty');
  const fields = data.extracted_fields?.fields?.length ? `<details><summary>Fields extracted by Needle 3 (unverified)</summary>${data.extracted_fields.fields.map(f => `<p class="result-details"><code>${esc(f.name)}</code>: <strong>${f.value === null ? '-' : esc(typeof f.value === 'string' ? f.value : JSON.stringify(f.value))}</strong> · ${esc(CHECK_LABELS[f.check] || f.check)}</p>`).join('')}</details>` : '';
  results.innerHTML = identityHTML(data) + orderedAnswers(data.answers, data.request as Record<string, unknown> | undefined).map(([id, a], i) => resultHTML(id, a, {}, i)).join('') + fields
    + '<div class="actions"><button id="dismiss-current" class="button danger ghost">Delete</button><button id="export-current" class="button secondary">Export Full JSON</button><button id="export-lite-current" class="button secondary">Export Lite JSON</button></div>'
    + exportGuide();
  $('dismiss-current').addEventListener('click', () => deleteCurrent());
  $('export-current').addEventListener('click', () => exportDecision(data.decision_id, 'full'));
  $('export-lite-current').addEventListener('click', () => exportDecision(data.decision_id, 'lite'));
}

export function clearCurrentResults(): void {
  current = null;
  $('machine-output').textContent = 'No completed evaluation.';
  $('results').classList.add('empty');
  $('results').textContent = 'Decision deleted from the database.';
  $('latency').textContent = '';
}

async function deleteCurrent(): Promise<void> {
  try {
    if (!current) return;
    await api('/api/delete', { id: current.decision_id });
    clearCurrentResults();
    toast('Decision and associated records deleted');
    await refreshStatus();
  } catch (e) { reportError(e); }
}

const ROLE_LABELS: Record<string, string> = { generalist: 'Plumb (up to 16 options, titles, evidence)', specialist: 'Specialist (catalogs)', clef: 'Clef-Flash (catalogs)' };

export function renderEngineProfile(engine: EngineHealth): void {
  const core = engine.core;
  const models = engine.models || [];
  const catalog = models.some(m => m.role === 'clef') ? 'Clef-Flash' : models.some(m => m.role === 'specialist') ? 'specialist' : '';
  const limit = engine.routing?.max_generalist_options ?? 16;
  const facts: [string, string][] = [
    ['Runtime', engine.runtime || 'llama.cpp'],
    ...models.map(m => [ROLE_LABELS[m.role] || m.role, m.file] as [string, string]),
    ['Execution', engine.device === 'CPU' || !engine.device ? 'Processor (CPU)' : `GPU ${engine.device}`],
    ['Why these models', engine.plan || 'Not loaded yet'],
    ['Routing', models.some(m => m.role === 'generalist') ? `Up to ${limit} options: Plumb${catalog ? ` · more: ${catalog}` : ''}` : catalog ? `Every question: ${catalog}` : 'No model loaded'],
    ['Output contract', 'Choice · Yes/no · Score'],
    ['Deterministic core', core ? (core.backend === 'rust' ? `Rust ${core.core_version || ''}`.trim() : 'Python reference') : 'Rust'],
    ['Calibration', 'Specialist and Plumb: local validation, not a general guarantee · Clef-Flash: not validated for your domain'],
    ['Network', 'Local only · no external API'],
  ];
  $('engine-profile').innerHTML = facts.map(([label, value]) => `<div class="engine-fact"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join('');
}
