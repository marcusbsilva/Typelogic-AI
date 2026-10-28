import type { Answer, StoredDecision, TrainingExport } from './types';
import { $, $input, $select, api, downloadText, esc, exportDecision, reportError, space, toast } from './dom';
import { clearCurrentResults, currentDecisionId, exportGuide, identityHTML, orderedAnswers, questionTitle, resultHTML, tagChips } from './render';
import { refreshStatus } from './status';
import { setUsedTags, suggestedTags } from './tags';

// Library: every stored decision of the workspace in a grid of up to PAGE_SIZE cards per page, filtered by tag. The
// tag filter lists the tags the decisions of the workspace carry (suggested tags first, then your own), so it grows as
// decisions are tagged. Clicking a card opens its detail (answers, integrity, export, correction, delete); "Back to
// Library" returns to the grid. Several cards can be selected and deleted together.

export const PAGE_SIZE = 30;
const UNTAGGED = '\u0000untagged';
let rows: StoredDecision[] = [];
let filter = 'all';
let page = 1;
let openId: string | null = null;
let showAllTags = false;
const selected = new Set<string>();
const TAG_PREVIEW = 14;

const tagsOf = (r: StoredDecision): string[] => (Array.isArray(r.request.tags) ? r.request.tags : []);

export function setFilter(next: string): void {
  if (next !== filter) { selected.clear(); page = 1; }
  filter = next;
}

/** Show the grid (closing any open detail). */
export function showLibraryList(): void {
  openId = null;
  $('library-detail').classList.add('hidden');
  $('library-list').classList.remove('hidden');
}

let generation = 0;

export async function refreshHistory(): Promise<void> {
  const mine = ++generation;
  const fresh = (await api<{ decisions: StoredDecision[] }>(`/api/decisions?space=${encodeURIComponent(space())}`)).decisions;
  if (mine !== generation) return;          // a newer refresh has started; drop this stale answer
  rows = fresh;
  for (const id of [...selected]) if (!rows.some(r => r.id === id)) selected.delete(id);
  setUsedTags(usedTags());
  if (filter !== 'all' && !tagCounts().has(filter)) { filter = 'all'; page = 1; }
  renderFilters();
  renderGrid();
  if (openId) {
    const r = rows.find(row => row.id === openId);
    if (r) renderDetail(r); else showLibraryList();
  }
}

// ---------------------------------------------------------------- tags

function tagCounts(): Map<string, number> {
  const counts = new Map<string, number>();
  for (const r of rows) {
    const tags = tagsOf(r);
    if (!tags.length) counts.set(UNTAGGED, (counts.get(UNTAGGED) || 0) + 1);
    for (const t of tags) counts.set(t, (counts.get(t) || 0) + 1);
  }
  return counts;
}

/** Used tags: the suggested ones in their order, then your own by frequency and name. */
function usedTags(): string[] {
  const counts = tagCounts();
  counts.delete(UNTAGGED);
  const preset = suggestedTags().filter(t => counts.has(t));
  const own = [...counts.keys()].filter(t => !suggestedTags().includes(t)).sort((a, b) => (counts.get(b)! - counts.get(a)!) || a.localeCompare(b));
  return [...preset, ...own];
}

function renderFilters(): void {
  const counts = tagCounts();
  const tags = usedTags();
  const visible = showAllTags || tags.length <= TAG_PREVIEW ? tags : tags.slice(0, TAG_PREVIEW);
  if (filter !== 'all' && filter !== UNTAGGED && !visible.includes(filter)) visible.push(filter);
  const chip = (value: string, label: string, n: number, extra = '') => `<button type="button" class="filter-tag${extra}${filter === value ? ' active' : ''}" role="tab" aria-selected="${filter === value}" data-filter="${esc(value)}">${label} <b>${n}</b></button>`;
  const custom = (t: string) => suggestedTags().includes(t) ? '' : ' own';
  $('filter-tags').innerHTML = chip('all', 'All', rows.length)
    + visible.map(t => chip(t, esc(t), counts.get(t) || 0, custom(t))).join('')
    + (counts.has(UNTAGGED) && tags.length ? chip(UNTAGGED, 'Untagged', counts.get(UNTAGGED) || 0, ' untagged') : '')
    + (tags.length > TAG_PREVIEW ? `<button type="button" class="text-button more-tags" aria-expanded="${showAllTags}">${showAllTags ? 'Fewer tags' : `+${tags.length - TAG_PREVIEW} more`}</button>` : '');
  $('filter-tags').querySelectorAll<HTMLElement>('.filter-tag').forEach(b => b.addEventListener('click', () => {
    setFilter(b.dataset.filter || 'all');
    renderFilters();
    renderGrid();
  }));
  $('filter-tags').querySelector('.more-tags')?.addEventListener('click', () => { showAllTags = !showAllTags; renderFilters(); });
}

// ---------------------------------------------------------------- grid

/** Lowest top probability among a decision's questions (the model's own estimate, not a measured accuracy). */
export function weakestAnswer(r: StoredDecision): { probability: number; title: string } | null {
  let best: { probability: number; title: string } | null = null;
  Object.entries(r.response.answers || {}).forEach(([id, a], i) => {
    const top = Math.max(...Object.values(a.probabilities));
    if (!best || top < best.probability) best = { probability: top, title: questionTitle(id, a, r.request.questions, i) };
  });
  return best;
}

function filtered(): StoredDecision[] {
  const list = filter === 'all' ? rows : filter === UNTAGGED ? rows.filter(r => !tagsOf(r).length) : rows.filter(r => tagsOf(r).includes(filter));
  const order = $select('library-sort').value;
  if (order === 'oldest') return [...list].reverse();
  if (order === 'confidence') {
    return [...list].sort((a, b) => ((weakestAnswer(a)?.probability ?? 2) - (weakestAnswer(b)?.probability ?? 2)) || a.created.localeCompare(b.created));
  }
  return list;
}

export async function exportTraining(): Promise<void> {
  const result = await api<TrainingExport>(`/api/training-export?space=${encodeURIComponent(space())}`);
  if (!result.records.length) { toast('No corrected decisions to export yet. Open a decision and record a correction first.'); return; }
  downloadText(`typelogic-training-${space()}.jsonl`, result.records.map(r => JSON.stringify(r)).join('\n') + '\n');
  toast(`${result.records.length} decisions exported for training (${result.corrections} corrections). Nothing was retrained.`);
}

function answerSummary(id: string, a: Answer): string {
  const top = Object.entries(a.probabilities).sort((x, y) => y[1] - x[1])[0];
  const value = a.type === 'noul' ? (top[0] === 'true' ? 'Yes' : 'No') : a.type === 'score' ? `${(a.score ?? 0).toFixed(2)}` : a.choice || top[0];
  const criteria = a.criteria && typeof a.criteria === 'object' && !Array.isArray(a.criteria) ? a.criteria : {};
  const text = a.type === 'choice' && criteria[value] ? (/^\d+$/.test(value) ? criteria[value] : `${value} · ${criteria[value]}`)
    : a.type === 'score' ? `${value} · ${a.legend?.[top[0]] || top[0]}` : value;
  return `<li><span class="mini-q">${esc(a.instructions || id)}</span><span class="mini-a">${esc(text)} <em>${(top[1] * 100).toFixed(0)}%</em></span></li>`;
}

function cardHTML(r: StoredDecision): string {
  const date = new Date(r.created);
  const when = Number.isNaN(date.getTime()) ? '' : date.toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' });
  const answers = orderedAnswers(r.response.answers || {}, r.request);
  const shown = answers.slice(0, 3).map(([id, a]) => answerSummary(id, a)).join('');
  const more = answers.length > 3 ? `<li class="mini-more">+${answers.length - 3} more</li>` : '';
  const weakest = weakestAnswer(r);
  return `<article class="library-card${selected.has(r.id) ? ' selected' : ''}" data-id="${esc(r.id)}" tabindex="0" role="button" aria-label="Open ${esc(r.title)}">`
    + `<div class="card-top"><label class="card-select" title="Select"><input type="checkbox" class="select-request" aria-label="Select decision"${selected.has(r.id) ? ' checked' : ''}></label>`
    + `<time>${esc(when)}</time></div>`
    + `<h3>${esc(r.title)}</h3>${tagChips(tagsOf(r), 'tag-list card-tags')}<ul class="mini-answers">${shown}${more}</ul>`
    + `<div class="card-foot">${weakest ? `Lowest confidence <strong>${(weakest.probability * 100).toFixed(0)}%</strong>` : ''}<span>${r.integrity?.verified === false ? '⚠ integrity check failed' : ''}</span></div></article>`;
}

function pageButtons(pages: number): string {
  // 1 … 4 5 [6] 7 8 … 12
  const want = new Set([1, pages, page - 2, page - 1, page, page + 1, page + 2].filter(n => n >= 1 && n <= pages));
  const numbers = [...want].sort((a, b) => a - b);
  let out = `<button type="button" class="page-step" data-page="${page - 1}"${page === 1 ? ' disabled' : ''} aria-label="Previous page">‹ Previous</button>`;
  numbers.forEach((n, i) => {
    if (i && n - numbers[i - 1] > 1) out += '<span class="page-gap">…</span>';
    out += `<button type="button" class="page-number${n === page ? ' active' : ''}" data-page="${n}"${n === page ? ' aria-current="page"' : ''} aria-label="Page ${n}">${n}</button>`;
  });
  return out + `<button type="button" class="page-step" data-page="${page + 1}"${page === pages ? ' disabled' : ''} aria-label="Next page">Next ›</button>`;
}

export function renderGrid(): void {
  const list = filtered();
  const pages = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
  page = Math.min(Math.max(1, page), pages);
  const start = (page - 1) * PAGE_SIZE;
  const shown = list.slice(start, start + PAGE_SIZE);
  const label = filter === 'all' ? '' : filter === UNTAGGED ? 'untagged ' : `“${filter}” `;
  $('library-grid').innerHTML = shown.length ? shown.map(cardHTML).join('')
    : `<div class="library-empty"><strong>No ${esc(label)}decisions in this workspace.</strong><span>Evaluate a decision on the New decision page; it appears here.</span></div>`;
  $('library-range').textContent = list.length ? `Showing ${start + 1}–${start + shown.length} of ${list.length}` : '';
  const pager = $('library-pager');
  pager.classList.toggle('hidden', pages <= 1);
  pager.innerHTML = pages > 1 ? pageButtons(pages) : '';
  pager.querySelectorAll<HTMLButtonElement>('button[data-page]').forEach(b => b.addEventListener('click', () => {
    page = Number(b.dataset.page);
    renderGrid();
    $('library-list').scrollIntoView({ block: 'start' });
  }));
  document.querySelectorAll<HTMLElement>('.library-card').forEach(card => {
    const id = card.dataset.id as string;
    const box = card.querySelector('.select-request') as HTMLInputElement;
    box.addEventListener('click', e => e.stopPropagation());
    (card.querySelector('.card-select') as HTMLElement).addEventListener('click', e => e.stopPropagation());
    box.addEventListener('change', () => {
      if (box.checked) selected.add(id); else selected.delete(id);
      card.classList.toggle('selected', box.checked);
      updateSelection();
    });
    const open = () => { const r = rows.find(row => row.id === id); if (r) openDetail(r); };
    card.addEventListener('click', open);
    card.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  });
  updateSelection();
}

// ---------------------------------------------------------------- selection and bulk delete

function updateSelection(): void {
  const count = selected.size;
  $('selection-bar').classList.toggle('hidden', count === 0);
  $('selection-count').textContent = `${count} selected`;
  const boxes = document.querySelectorAll<HTMLInputElement>('.select-request');
  $input('select-all').checked = boxes.length > 0 && [...boxes].every(b => b.checked);
}

async function deleteSelected(): Promise<void> {
  try {
    const ids = [...selected];
    if (!ids.length) return;
    const result = await api<{ deleted: number; results: { id: string; error?: string }[] }>('/api/delete', { ids });
    const failed = result.results.filter(r => r.error);
    toast(`${result.deleted} decision${result.deleted === 1 ? '' : 's'} deleted` + (failed.length ? ` · ${failed.length} failed: ${failed[0].error}` : ''));
    selected.clear();
    const current = currentDecisionId();
    if (current && ids.includes(current)) clearCurrentResults();
    await refreshHistory();
    await refreshStatus();
  } catch (e) { reportError(e); }
}

// ---------------------------------------------------------------- detail

function optionText(a: Answer, key: string): string {
  const criteria = a.criteria && typeof a.criteria === 'object' && !Array.isArray(a.criteria) ? a.criteria : undefined;
  return a.type === 'choice' && criteria?.[key] ? `${key} — ${criteria[key]}` : (a.legend?.[key] || key);
}

function bindCorrection(view: HTMLElement, r: StoredDecision): void {
  const form = view.querySelector('.feedback-form') as HTMLElement;
  form.classList.toggle('hidden');
  if (form.innerHTML) return;
  form.innerHTML = `<label>Question<select class="feedback-question">${Object.entries(r.response.answers).map(([id, a], i) => `<option value="${esc(id)}">${esc(questionTitle(id, a, r.request.questions, i))}</option>`).join('')}</select></label><label>Observed alternative<select class="feedback-option"></select></label><button class="button secondary save-feedback">Save correction</button><p class="micro">The label is stored; the model is not retrained automatically.</p>`;
  const question = form.querySelector('.feedback-question') as HTMLSelectElement;
  const option = form.querySelector('.feedback-option') as HTMLSelectElement;
  const update = () => {
    const a = r.response.answers[question.value];
    option.innerHTML = Object.keys(a.probabilities).map(key => `<option value="${esc(key)}">${esc(optionText(a, key))}</option>`).join('');
  };
  question.addEventListener('change', update);
  update();
  (form.querySelector('.save-feedback') as HTMLElement).addEventListener('click', async () => {
    try {
      const question_id = question.value, chosen = option.value;
      const target = Object.fromEntries(Object.keys(r.response.answers[question_id].probabilities).map(key => [key, key === chosen ? 1 : 0]));
      await api('/api/feedback', { id: r.id, question_id, target });
      toast('Correction saved');
      await refreshStatus();
    } catch (e) { reportError(e); }
  });
}

function renderDetail(r: StoredDecision): void {
  const body = $('library-detail-body');
  body.innerHTML = `<article class="panel detail-card"><div class="actions detail-actions"><button class="button danger ghost detail-delete" title="Delete this decision and its associated records">Delete</button><span class="actions-gap"></span><button class="button secondary detail-correction">Record correction</button><button class="button secondary detail-export">Export Full JSON</button><button class="button secondary detail-export-lite">Export Lite JSON</button></div>`
    + `<div class="feedback-form hidden"></div>${identityHTML({ ...r, tags: tagsOf(r), title_source: r.response.title_source })}`
    + orderedAnswers(r.response.answers, r.request).map(([id, a], i) => resultHTML(id, a, r.request.questions, i)).join('')
    + `${exportGuide(true)}</article>`;
  (body.querySelector('.detail-delete') as HTMLElement).addEventListener('click', async () => {
    try {
      await api('/api/delete', { id: r.id });
      if (currentDecisionId() === r.id) clearCurrentResults();
      toast('Decision and associated records deleted');
      showLibraryList();
      await refreshHistory();
      await refreshStatus();
    } catch (e) { reportError(e); }
  });
  (body.querySelector('.detail-export') as HTMLElement).addEventListener('click', () => exportDecision(r.id, 'full'));
  (body.querySelector('.detail-export-lite') as HTMLElement).addEventListener('click', () => exportDecision(r.id, 'lite'));
  (body.querySelector('.detail-correction') as HTMLElement).addEventListener('click', () => bindCorrection(body, r));
}

function openDetail(r: StoredDecision): void {
  openId = r.id;
  renderDetail(r);
  $('library-list').classList.add('hidden');
  $('library-detail').classList.remove('hidden');
  window.scrollTo({ top: 0, behavior: 'instant' });
}

export function installLibrary(): void {
  $select('library-sort').addEventListener('change', () => { page = 1; renderGrid(); });
  $('library-back').addEventListener('click', () => { showLibraryList(); renderGrid(); });
  $('bulk-delete').addEventListener('click', () => { deleteSelected(); });
  $('clear-selection').addEventListener('click', () => {
    selected.clear();
    document.querySelectorAll<HTMLInputElement>('.select-request').forEach(b => { b.checked = false; });
    document.querySelectorAll('.library-card.selected').forEach(c => c.classList.remove('selected'));
    updateSelection();
  });
  $input('select-all').addEventListener('change', () => {
    const on = $input('select-all').checked;
    document.querySelectorAll<HTMLInputElement>('.select-request').forEach(box => {
      box.checked = on;
      const card = box.closest('.library-card') as HTMLElement;
      card.classList.toggle('selected', on);
      if (on) selected.add(card.dataset.id as string); else selected.delete(card.dataset.id as string);
    });
    updateSelection();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && openId && !document.querySelector('#help-popup:not([hidden])') && !(document.querySelector('#archive-dialog') as HTMLDialogElement | null)?.open) { showLibraryList(); renderGrid(); }
  });
}
