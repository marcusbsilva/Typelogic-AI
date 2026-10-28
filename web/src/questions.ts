import type { ChoiceCriteria, DecisionSpec, QuestionSpec, QuestionType } from './types';
import { $, esc, toast } from './dom';
import { helpIcon, helpTexts } from './help';

// Decision editor in the Jev style: no decision name, only the question. Each decision is Yes / no, Choice or Score:
//   Yes / no  the question only (the case and the evidence carry the facts)
//   Choice    numbered options (0, 1, 2, ...), each with when to choose it
//   Score     numbered levels from lowest to highest

type Kind = 'noul' | 'choice' | 'score';
const KINDS: [Kind, string, string][] = [
  ['noul', 'Yes / no', 'A Yes / no question returns the probability that a condition holds.'],
  ['choice', 'Choice', 'A Choice question picks one option from a set you define and returns a probability for each.'],
  ['score', 'Score', 'A Score question rates the case against ordered levels and returns a score, a distribution and confidence.'],
];
const OUTPUT: Record<Kind, DecisionSpec['output']> = { noul: 'boolean', choice: 'choice', score: 'score' };
const KIND: Record<string, Kind> = { boolean: 'noul', noul: 'noul', choice: 'choice', score: 'score' };
const PLACEHOLDER: Record<Kind, string> = {
  noul: 'Is this action safe to run without a human approving it first?',
  choice: 'Which team should handle this message?',
  score: 'How ready is this lead to buy?',
};

interface DecisionCard extends HTMLDivElement { kind: Kind; name?: string; }

// ---------------------------------------------------------------- rows

function addOption(card: DecisionCard, description = ''): void {
  const list = card.querySelector('.options-list') as HTMLElement;
  if (list.children.length >= 255) { toast('Limit: 255 options'); return; }
  const row = document.createElement('div');
  row.className = 'option-row';
  row.innerHTML = `<span class="level-index opt-index"></span><input class="opt-desc" maxlength="3000" placeholder="Choose when…" value="${esc(description)}" aria-label="Choose when">`
    + `<button type="button" class="remove opt-remove" aria-label="Remove option">×</button>`;
  (row.querySelector('.opt-remove') as HTMLElement).addEventListener('click', () => { row.remove(); refreshOptions(card); });
  (row.querySelector('.opt-desc') as HTMLInputElement).addEventListener('input', () => refreshOptions(card));
  list.append(row);
  refreshOptions(card);
}

/** Options are numbered 0, 1, 2, ... like Score levels; the number is the value the answer returns. */
function refreshOptions(card: DecisionCard, selected?: string): void {
  card.querySelectorAll<HTMLElement>('.opt-index').forEach((n, i) => { n.textContent = String(i); });
  const select = card.querySelector('.abstain') as HTMLSelectElement;
  const current = selected ?? select.value;
  const rows = [...card.querySelectorAll<HTMLInputElement>('.opt-desc')].map((input, i) => [String(i), input.value.trim()]);
  select.innerHTML = '<option value="">None</option>' + rows.map(([k, d]) => `<option value="${k}"${k === current ? ' selected' : ''}>${k}${d ? ` · ${esc(d.slice(0, 40))}` : ''}</option>`).join('');
}

function addLevel(card: DecisionCard, text = ''): void {
  const list = card.querySelector('.levels-list') as HTMLElement;
  if (list.children.length >= 32) { toast('Limit: 32 levels'); return; }
  const row = document.createElement('div');
  row.className = 'level-row';
  row.innerHTML = `<span class="level-index"></span><input class="level-text" maxlength="3000" placeholder="Describe this level" value="${esc(text)}" aria-label="Level"><button type="button" class="remove level-remove" aria-label="Remove level">×</button>`;
  (row.querySelector('.level-remove') as HTMLElement).addEventListener('click', () => { row.remove(); refreshLevels(card); });
  (row.querySelector('.level-text') as HTMLInputElement).addEventListener('input', () => refreshLevels(card));
  list.append(row);
  refreshLevels(card);
}

function levelTexts(card: DecisionCard): string[] {
  return [...card.querySelectorAll<HTMLInputElement>('.level-text')].map(i => i.value.trim());
}

function refreshLevels(card: DecisionCard): void {
  card.querySelectorAll<HTMLElement>('.levels-list .level-index').forEach((n, i) => { n.textContent = String(i); });
}

function setKind(card: DecisionCard, kind: Kind): void {
  card.kind = kind;
  card.querySelectorAll<HTMLButtonElement>('.type-tab').forEach(b => {
    const on = b.dataset.kind === kind;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', String(on));
  });
  (card.querySelector('.type-hint') as HTMLElement).textContent = KINDS.find(k => k[0] === kind)![2];
  (card.querySelector('.instructions') as HTMLTextAreaElement).placeholder = PLACEHOLDER[kind];
  card.querySelectorAll<HTMLElement>('.for-noul, .for-choice, .for-score').forEach(n => n.classList.toggle('hidden', !n.classList.contains(`for-${kind}`)));
  if (kind === 'choice' && !card.querySelector('.option-row')) { addOption(card); addOption(card); }
  if (kind === 'score' && !card.querySelector('.level-row')) { addLevel(card); addLevel(card); addLevel(card); }
}

// ---------------------------------------------------------------- card

export function addQuestion(type: QuestionType = 'noul', d?: DecisionSpec): HTMLDivElement | undefined {
  if (document.querySelectorAll('.question').length >= 32) { toast('Limit: 32 decisions'); return undefined; }
  const card = document.createElement('div') as DecisionCard;
  card.className = 'question decision-card';
  if (d?.name) card.name = d.name;
  card.innerHTML = `<div class="decision-head"><span class="decision-number"></span><div class="type-tabs" role="tablist" aria-label="Output type">`
    + KINDS.map(([k, label]) => `<button type="button" class="type-tab" role="tab" data-kind="${k}">${label}</button>`).join('')
    + `</div>${helpIcon(helpTexts.types, 'Explain the output types')}<button type="button" class="remove decision-remove" aria-label="Remove decision">×</button></div>`
    + `<p class="type-hint micro"></p>`
    + `<label class="field-caption">Question<textarea class="instructions" rows="2" maxlength="6000">${esc(d?.instructions || '')}</textarea></label>`
    // Choice
    + `<div class="for-choice"><span class="field-caption">Options</span><div class="options-list"></div>`
    + `<div class="row-actions"><button type="button" class="text-button add-option">+ Add option</button>`
    + `<label class="abstain-line">"Not enough information" option <select class="abstain" aria-label="Abstention option"><option value="">None</option></select></label></div></div>`
    // Score
    + `<div class="for-score"><span class="field-caption">Levels, lowest to highest</span><div class="levels-list"></div>`
    + `<div class="row-actions"><button type="button" class="text-button add-level">+ Add level</button></div></div>`;
  card.querySelectorAll<HTMLButtonElement>('.type-tab').forEach(b => b.addEventListener('click', () => setKind(card, b.dataset.kind as Kind)));
  (card.querySelector('.decision-remove') as HTMLElement).addEventListener('click', () => { card.remove(); renumber(); });
  (card.querySelector('.add-option') as HTMLElement).addEventListener('click', () => addOption(card));
  (card.querySelector('.add-level') as HTMLElement).addEventListener('click', () => addLevel(card));
  $('questions').append(card);
  const kind = KIND[d?.output || type] || 'noul';
  if (kind === 'choice' && d) {
    const options = (d.alternatives || (d.criteria && !Array.isArray(d.criteria) ? d.criteria : Object.fromEntries(((d.criteria as string[]) || []).map(k => [k, ''])))) as ChoiceCriteria;
    const keys = Object.keys(options);
    keys.forEach(key => {
      const text = options[key] || '';
      // Earlier templates used named options (billing, A, X): keep the name in the description when it adds meaning.
      const named = !/^\d+$/.test(key) && key.length > 1 && !text.toLowerCase().includes(key.toLowerCase());
      addOption(card, text ? (named ? `${key[0].toUpperCase()}${key.slice(1)} — ${text}` : text) : key);
    });
    const abstain = d.abstain_option ? keys.indexOf(d.abstain_option) : -1;
    refreshOptions(card, abstain >= 0 ? String(abstain) : '');
  }
  if (kind === 'score' && d) {
    const raw = d.levels || d.criteria || [];
    const levels = Array.isArray(raw) ? raw : Object.keys(raw).sort((a, b) => Number(a) - Number(b)).map(k => (raw as Record<string, string>)[k]);
    levels.forEach(text => addLevel(card, String(text)));
  }
  setKind(card, kind);
  renumber();
  return card;
}

function renumber(): void {
  const numbers = document.querySelectorAll<HTMLElement>('.decision-card .decision-number');
  numbers.forEach((n, i) => { n.textContent = String(i + 1); });
  const hint = document.getElementById('decisions-hint');
  if (hint) hint.textContent = `${numbers.length} decision${numbers.length === 1 ? '' : 's'} · Yes / no, Choice or Score`;
}

/** Fill the editor from v2 decisions (templates). */
export function loadDecisions(decisions: DecisionSpec[]): void {
  $('questions').replaceChildren();
  for (const d of decisions) addQuestion(KIND[d.output] as QuestionType, d);
  if (!decisions.length) addQuestion();
}

/** Fill the editor from v1 questions (the specialist profile). */
export function loadQuestions(questions: Record<string, QuestionSpec>): void {
  loadDecisions(Object.values(questions).map(q => ({
    output: OUTPUT[q.type as Kind], instructions: q.instructions, criteria: q.criteria, abstain_option: q.abstain_option,
  })));
}

// ---------------------------------------------------------------- read back

/** v2 decisions from the editor; throws a readable error for the first problem. */
export function getDecisions(): DecisionSpec[] {
  const out: DecisionSpec[] = [];
  document.querySelectorAll<DecisionCard>('.decision-card').forEach((card, i) => {
    const label = `Decision ${i + 1}`;
    const instructions = (card.querySelector('.instructions') as HTMLTextAreaElement).value.trim();
    if (!instructions) throw Error(`${label}: write the question`);
    const kind = card.kind;
    const d: DecisionSpec = { output: OUTPUT[kind], instructions };
    if (card.name) d.name = card.name;
    if (kind === 'choice') {
      const criteria: ChoiceCriteria = Object.create(null);
      card.querySelectorAll<HTMLElement>('.option-row').forEach((row, n) => {
        const key = String(n);
        const description = (row.querySelector('.opt-desc') as HTMLInputElement).value.trim();
        if (!description) throw Error(`${label}: describe option ${n} or remove it`);
        criteria[key] = description;
      });
      if (Object.keys(criteria).length < 2) throw Error(`${label}: add at least two options`);
      d.criteria = criteria;
      const abstain = (card.querySelector('.abstain') as HTMLSelectElement).value;
      if (abstain && Object.hasOwn(criteria, abstain)) d.abstain_option = abstain;
    } else if (kind === 'score') {
      const levels = levelTexts(card).filter(Boolean);
      if (levels.length < 2) throw Error(`${label}: add at least two levels`);
      if (new Set(levels).size !== levels.length) throw Error(`${label}: levels must be different`);
      d.criteria = levels;
    }
    out.push(d);
  });
  if (!out.length) throw Error('Add a decision');
  return out;
}
