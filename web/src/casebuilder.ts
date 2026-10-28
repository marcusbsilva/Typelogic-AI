import type { CaseRequest, CaseTemplate, EvidenceEntry, EvidenceExtraction, EvidenceSpec, EvidenceType, EvidenceValue, TemplateRow } from './types';
import { $, $area, $button, $input, $select, api, errorMessage, esc, reportError, space, toast } from './dom';
import { addQuestion, getDecisions, loadDecisions } from './questions';
import { getTags, installTags, setTags } from './tags';

// New decision page: Case (template, optional title, tags and one case text), Evidence (typed facts with provenance), Decisions.
// buildCaseRequest() is the single request builder used by preview, copy and Evaluate.

const TYPES: EvidenceType[] = ['boolean', 'number', 'integer', 'date', 'text', 'enum'];
const NAME = /^[a-z][a-z0-9_]{0,63}$/;
export const CHECK_LABELS: Record<string, string> = {
  in_text: 'written in the case', not_in_text: 'not found in the case', inferred: 'inferred', not_stated: 'not stated',
};
let templates: TemplateRow[] = [];
let currentTemplate: string | undefined;

interface EvidenceRow extends HTMLTableRowElement { entry?: Omit<EvidenceEntry, 'value'> & { check?: string }; }

// ---------------------------------------------------------------- evidence table

function valueControl(type: EvidenceType, options: string[], value: EvidenceValue): string {
  if (type === 'boolean') return `<select class="ev-value" aria-label="Value"><option value="">—</option><option value="true"${value === true ? ' selected' : ''}>true</option><option value="false"${value === false ? ' selected' : ''}>false</option></select>`;
  if (type === 'enum') return `<select class="ev-value" aria-label="Value"><option value="">—</option>${options.map(o => `<option${o === value ? ' selected' : ''}>${esc(o)}</option>`).join('')}</select>`;
  const kind = type === 'date' ? 'date' : type === 'text' ? 'text' : 'number';
  const step = type === 'integer' ? ' step="1"' : type === 'number' ? ' step="any"' : '';
  return `<input class="ev-value" aria-label="Value" type="${kind}"${step} value="${value === null || value === undefined ? '' : esc(value)}">`;
}

function badge(row: EvidenceRow): string {
  const e = row.entry;
  if (!e || e.status === 'confirmed') return `<span class="ev-status confirmed">${e?.source === 'template' ? 'template' : 'confirmed'}</span>`;
  const check = e.check ? ` · ${esc(CHECK_LABELS[e.check] || e.check)}` : '';
  return `<span class="ev-status unverified check-${esc(e.check || '')}">extracted${check}</span><button type="button" class="text-button ev-keep" title="Keep this value">✓ Keep</button>`;
}

function renderValueCell(row: EvidenceRow, value: EvidenceValue): void {
  const type = (row.querySelector('.ev-type') as HTMLSelectElement).value as EvidenceType;
  const options = (row.querySelector('.ev-options') as HTMLInputElement).value.split(',').map(v => v.trim()).filter(Boolean);
  (row.querySelector('.ev-value-cell') as HTMLElement).innerHTML = valueControl(type, options, value);
  (row.querySelector('.ev-options') as HTMLElement).classList.toggle('hidden', type !== 'enum');
  row.querySelector('.ev-value')?.addEventListener('change', () => confirmRow(row, 'user'));
}

function renderStatus(row: EvidenceRow): void {
  const cell = row.querySelector('.ev-status-cell') as HTMLElement;
  cell.innerHTML = badge(row);
  cell.querySelector('.ev-keep')?.addEventListener('click', () => confirmRow(row));
}

function confirmRow(row: EvidenceRow, source?: EvidenceEntry['source']): void {
  row.entry = { source: source || row.entry?.source || 'user', status: 'confirmed' };
  renderStatus(row);
}

export function addEvidence(spec: Partial<EvidenceSpec> = {}, value: EvidenceValue = null, entry?: EvidenceRow['entry']): EvidenceRow {
  const row = document.createElement('tr') as EvidenceRow;
  row.className = 'evidence-row';
  row.entry = entry || { source: 'user', status: 'confirmed' };
  const type = spec.type || 'text';
  row.innerHTML = `<td><input class="ev-name" aria-label="Field name" maxlength="64" spellcheck="false" placeholder="field_name" value="${esc(spec.name || '')}" title="${esc(spec.description || '')}">`
    + `<small class="ev-description">${esc(spec.description || '')}${spec.unit ? ` (${esc(spec.unit)})` : ''}</small></td>`
    + `<td><select class="ev-type" aria-label="Type">${TYPES.map(t => `<option${t === type ? ' selected' : ''}>${t}</option>`).join('')}</select>`
    + `<input class="ev-options${type === 'enum' ? '' : ' hidden'}" aria-label="Options" placeholder="low, medium, high" value="${esc((spec.options || []).join(', '))}"></td>`
    + `<td class="ev-value-cell"></td><td class="ev-status-cell"></td><td><button type="button" class="remove ev-remove" aria-label="Remove field">×</button></td>`;
  row.dataset.description = spec.description || '';
  if (spec.unit) row.dataset.unit = spec.unit;
  if (spec.required) row.dataset.required = '1';
  $('evidence-body').append(row);
  renderValueCell(row, value);
  renderStatus(row);
  (row.querySelector('.ev-type') as HTMLSelectElement).addEventListener('change', () => renderValueCell(row, null));
  (row.querySelector('.ev-options') as HTMLInputElement).addEventListener('change', () => renderValueCell(row, readValue(row)));
  (row.querySelector('.ev-remove') as HTMLElement).addEventListener('click', () => row.remove());
  return row;
}

function readValue(row: EvidenceRow): EvidenceValue {
  const type = (row.querySelector('.ev-type') as HTMLSelectElement).value as EvidenceType;
  const raw = (row.querySelector('.ev-value') as HTMLInputElement | HTMLSelectElement).value.trim();
  if (raw === '') return null;
  if (type === 'boolean') return raw === 'true';
  if (type === 'number' || type === 'integer') {
    const n = Number(raw);
    if (!Number.isFinite(n) || (type === 'integer' && !Number.isInteger(n))) throw Error(`Evidence ${(row.querySelector('.ev-name') as HTMLInputElement).value}: expected ${type}`);
    return n;
  }
  return raw;
}

function evidenceRows(): EvidenceRow[] { return [...document.querySelectorAll<EvidenceRow>('.evidence-row')]; }

export function getEvidence(): { schema: EvidenceSpec[]; evidence: Record<string, EvidenceEntry> } {
  const schema: EvidenceSpec[] = [], evidence: Record<string, EvidenceEntry> = {};
  for (const row of evidenceRows()) {
    const name = (row.querySelector('.ev-name') as HTMLInputElement).value.trim();
    if (!name && readValue(row) === null) continue;      // an empty new row is ignored
    if (!NAME.test(name)) throw Error(`Evidence name "${name}": lowercase letters, digits and _, starting with a letter`);
    if (schema.some(s => s.name === name)) throw Error(`Duplicate evidence field: ${name}`);
    const type = (row.querySelector('.ev-type') as HTMLSelectElement).value as EvidenceType;
    const spec: EvidenceSpec = { name, type };
    if (row.dataset.description) spec.description = row.dataset.description;
    if (row.dataset.unit) spec.unit = row.dataset.unit;
    if (row.dataset.required) spec.required = true;
    if (type === 'enum') {
      spec.options = (row.querySelector('.ev-options') as HTMLInputElement).value.split(',').map(v => v.trim()).filter(Boolean);
      if (spec.options.length < 2) throw Error(`Evidence ${name}: an enum needs at least two options`);
    }
    schema.push(spec);
    const value = readValue(row);
    if (value !== null) evidence[name] = { value, source: row.entry?.source || 'user', status: row.entry?.status || 'confirmed', ...(row.entry?.check ? { check: row.entry.check } : {}) };
  }
  return { schema, evidence };
}

// ---------------------------------------------------------------- templates

export function selectedTemplate(): TemplateRow | undefined {
  return templates.find(t => t.id === $select('template-select').value);
}

export function applyTemplate(t: CaseTemplate, withValues = true): void {
  currentTemplate = t.id;
  $area('case-text').value = withValues ? t.case || '' : '';
  $input('case-title').value = '';
  setTags(t.tags || []);
  $('evidence-body').replaceChildren();
  for (const spec of t.evidence_schema || []) {
    const value = withValues ? t.evidence?.[spec.name] ?? null : null;
    addEvidence(spec, value, { source: value === null ? 'user' : 'template', status: 'confirmed' });
  }
  loadDecisions(t.decisions);
  ($('evidence-section') as HTMLDetailsElement).open = (t.evidence_schema || []).length > 0;
  $input('permutation').checked = t.permutation_check !== false;
  $('extract-status').textContent = '';
}

export function blankCase(): void {
  currentTemplate = undefined;
  $area('case-text').value = '';
  $input('case-title').value = '';
  setTags([]);
  $('evidence-body').replaceChildren();
  ($('evidence-section') as HTMLDetailsElement).open = false;
  $('questions').replaceChildren();
  addQuestion();
}

export async function loadTemplates(selectId?: string): Promise<void> {
  const data = await api<{ templates: TemplateRow[]; errors: { path: string; error: string }[] }>('/api/templates');
  templates = data.templates;
  const group = (origin: string, label: string) => {
    const rows = templates.filter(t => t.origin === origin);
    return rows.length ? `<optgroup label="${label}">${rows.map(t => `<option value="${esc(t.id)}">${esc(t.title)}</option>`).join('')}</optgroup>` : '';
  };
  $select('template-select').innerHTML = '<option value="">Blank case</option>' + group('builtin', 'Built-in') + group('project', 'Project templates') + group('local', 'My templates');
  if (selectId) $select('template-select').value = selectId;
  if (data.errors.length) toast(`${data.errors.length} template file(s) could not be read: ${data.errors[0].path}`);
}

async function saveTemplate(): Promise<void> {
  const id = $input('template-id').value.trim();
  const title = $input('template-title').value.trim() || id;
  const withValues = $input('template-values').checked;
  const { schema, evidence } = getEvidence();
  const template: CaseTemplate = {
    id, title, description: $input('template-description').value.trim(), decisions: getDecisions(),
    evidence_schema: schema, permutation_check: $input('permutation').checked,
    ...(getTags().length ? { tags: getTags() } : {}),
    ...(withValues && $area('case-text').value.trim() ? { case: $area('case-text').value } : {}),
  };
  if (withValues) { template.example = true; template.evidence = Object.fromEntries(Object.entries(evidence).map(([k, v]) => [k, v.value])); }
  const overwrite = $input('template-overwrite').checked;
  const result = await api<{ id: string; saved: string[] }>('/api/templates', { template, overwrite });
  await loadTemplates(result.id);
  currentTemplate = result.id;
  $('template-save-status').textContent = `Saved to ${result.saved.join(' and ')}`;
  toast('Template saved');
}

// ---------------------------------------------------------------- extraction

async function extractEvidence(): Promise<void> {
  const button = $button('extract-evidence');
  const text = $area('case-text').value.trim();
  try {
    if (!text) throw Error('Describe the case first');
    const { schema } = getEvidence();
    if (!schema.length) throw Error('Declare the evidence fields first (a template defines them)');
    button.disabled = true;
    $('extract-status').innerHTML = '<span class="spinner"></span>Reading the case on this computer…';
    const result = await api<EvidenceExtraction>('/api/extract-evidence', { text, schema, backend: $select('extract-backend').value });
    let filled = 0;
    for (const row of evidenceRows()) {
      const name = (row.querySelector('.ev-name') as HTMLInputElement).value.trim();
      const found = result.fields.find(f => f.name === name);
      if (!found) continue;
      if (found.value === null) continue;           // keep what the user already typed
      renderValueCell(row, found.value);
      row.entry = { source: 'extracted', status: 'unverified', check: found.check };
      renderStatus(row);
      filled++;
    }
    $('extract-status').textContent = `${filled} of ${result.fields.length} fields suggested by ${result.model || result.backend} in ${result.seconds.toFixed(1)} s. Check each value and keep it (✓) or correct it.`;
  } catch (e) {
    $('extract-status').innerHTML = `<span class="error-text">${esc(errorMessage(e))}</span>`;
  } finally {
    button.disabled = false;
  }
}

function keepAll(): void {
  for (const row of evidenceRows()) if (row.entry?.status === 'unverified') confirmRow(row);
}

// ---------------------------------------------------------------- request

/** The one request builder used by preview, copy and Evaluate (AGENTS.md). */
export function buildCaseRequest(): CaseRequest {
  // A problem in a collapsed block opens that block, so the message points at something visible.
  const inBlock = <T>(block: string, read: () => T): T => {
    try { return read(); } catch (e) { ($(block) as HTMLDetailsElement).open = true; throw e; }
  };
  const text = inBlock('case-section', () => { const t = $area('case-text').value; if (!t.trim()) throw Error('Describe the case'); return t; });
  const { schema, evidence } = inBlock('evidence-section', getEvidence);
  const decisions = inBlock('decisions-section', getDecisions);
  const title = $input('case-title').value.trim().split(/\s+/).join(' ');
  const tags = getTags();
  return {
    ...(currentTemplate ? { template: currentTemplate } : {}),
    space: space(), ...(title ? { title } : {}), ...(tags.length ? { tags } : {}), case: text,
    evidence_schema: schema, evidence, decisions, permutation_check: $input('permutation').checked,
  };
}

export function installCaseBuilder(): void {
  $select('template-select').addEventListener('change', () => {
    const row = selectedTemplate();
    if (row) {
      applyTemplate(row.template, $input('template-load-values').checked);
      $('template-description-line').textContent = row.description || '';
    } else { blankCase(); $('template-description-line').textContent = 'Start from an empty case.'; }
  });
  installTags().catch(reportError);
  $('add-evidence').addEventListener('click', () => addEvidence());
  $('evidence-section').addEventListener('toggle', () => {
    if (($('evidence-section') as HTMLDetailsElement).open && !document.querySelector('.evidence-row')) addEvidence();
  });
  $('extract-evidence').addEventListener('click', () => { extractEvidence(); });
  $('keep-all-evidence').addEventListener('click', keepAll);
  $('save-template').addEventListener('click', () => saveTemplate().catch(e => { $('template-save-status').textContent = errorMessage(e); reportError(e); }));
  loadTemplates().then(() => { if (!document.querySelector('.decision-card')) blankCase(); }).catch(reportError);
}
