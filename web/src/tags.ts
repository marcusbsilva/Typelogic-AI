import type { TagInfo } from './types';
import { $, $input, api, esc, toast } from './dom';

// Tag field of the case: chips plus a text entry that suggests tags while typing (the suggested tags of the app and
// the tags already used in the Library). Enter, Tab or a comma adds the highlighted suggestion or the typed text, so
// any tag of your own can be added; Backspace on an empty entry removes the last chip.

let suggested: string[] = [];
let used: string[] = [];
let chosen: string[] = [];
let highlight = -1;
let maxTags = 8;
let maxChars = 32;

/** Same normalization as casefmt.validate_tags: spaces collapsed, lowercase. */
export const normalizeTag = (value: string): string => value.split(/\s+/).filter(Boolean).join(' ').toLowerCase();

export const suggestedTags = (): string[] => suggested;
export const getTags = (): string[] => [...chosen];

/** Tags already used by decisions of the Library (offered after the suggested ones). */
export function setUsedTags(tags: string[]): void { used = tags; }

export function setTags(tags: string[] = []): void {
  chosen = [];
  for (const t of tags) { const tag = normalizeTag(t); if (tag && !chosen.includes(tag)) chosen.push(tag); }
  renderChips();
}

function renderChips(): void {
  $('tag-chips').innerHTML = chosen.map((t, i) => `<span class="tag-chip">${esc(t)}<button type="button" class="tag-remove" data-index="${i}" aria-label="Remove tag ${esc(t)}">×</button></span>`).join('');
  $('tag-chips').querySelectorAll<HTMLButtonElement>('.tag-remove').forEach(b => b.addEventListener('click', () => {
    chosen.splice(Number(b.dataset.index), 1);
    renderChips();
    $input('tag-entry').focus();
  }));
  $input('tag-entry').placeholder = chosen.length >= maxTags ? 'Tag limit reached' : chosen.length ? 'Add another tag…' : 'Type a tag…';
}

function matches(text: string): string[] {
  const q = normalizeTag(text);
  const pool = [...new Set([...suggested, ...used])].filter(t => !chosen.includes(t));
  if (!q) return pool.slice(0, 12);
  const starts = pool.filter(t => t.startsWith(q)), contains = pool.filter(t => !t.startsWith(q) && t.includes(q));
  return [...starts, ...contains].slice(0, 12);
}

function closeList(): void {
  highlight = -1;
  $('tag-suggestions').classList.add('hidden');
  $input('tag-entry').setAttribute('aria-expanded', 'false');
  $input('tag-entry').removeAttribute('aria-activedescendant');
}

function renderList(): void {
  const entry = $input('tag-entry'), list = $('tag-suggestions');
  const typed = normalizeTag(entry.value);
  const items = matches(entry.value);
  const own = typed && !items.includes(typed) && !chosen.includes(typed);
  if (!items.length && !own) { closeList(); return; }
  if (highlight >= items.length + (own ? 1 : 0)) highlight = -1;
  list.innerHTML = items.map((t, i) => `<li id="tag-option-${i}" role="option" class="tag-option${i === highlight ? ' active' : ''}" data-tag="${esc(t)}" aria-selected="${i === highlight}">${esc(t)}${used.includes(t) && !suggested.includes(t) ? '<small>used</small>' : ''}</li>`).join('')
    + (own ? `<li id="tag-option-${items.length}" role="option" class="tag-option own${highlight === items.length ? ' active' : ''}" data-tag="${esc(typed)}" aria-selected="${highlight === items.length}">Add “${esc(typed)}”<small>your tag</small></li>` : '');
  list.classList.remove('hidden');
  entry.setAttribute('aria-expanded', 'true');
  if (highlight >= 0) entry.setAttribute('aria-activedescendant', `tag-option-${highlight}`); else entry.removeAttribute('aria-activedescendant');
  list.querySelectorAll<HTMLElement>('.tag-option').forEach(li => li.addEventListener('mousedown', e => {
    e.preventDefault();                     // keep the focus in the entry
    addTag(li.dataset.tag || '');
  }));
}

export function addTag(value: string): boolean {
  const tag = normalizeTag(value);
  const entry = $input('tag-entry');
  if (!tag) return false;
  if (chosen.includes(tag)) { entry.value = ''; renderList(); return false; }
  if (chosen.length >= maxTags) { toast(`Up to ${maxTags} tags`); return false; }
  if (tag.length > maxChars || /[<>"\\]/.test(tag)) { toast(`A tag has 1-${maxChars} characters, without < > " or \\`); return false; }
  chosen.push(tag);
  entry.value = '';
  highlight = -1;
  renderChips();
  renderList();
  return true;
}

export async function installTags(): Promise<void> {
  const entry = $input('tag-entry');
  entry.addEventListener('focus', renderList);
  entry.addEventListener('input', () => {
    if (entry.value.includes(',')) { entry.value.split(',').slice(0, -1).forEach(addTag); entry.value = entry.value.split(',').pop() || ''; }
    highlight = -1;
    renderList();
  });
  entry.addEventListener('blur', () => window.setTimeout(closeList, 100));
  entry.addEventListener('keydown', e => {
    const options = [...$('tag-suggestions').querySelectorAll<HTMLElement>('.tag-option')];
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!options.length) { renderList(); return; }
      highlight = e.key === 'ArrowDown' ? (highlight + 1) % options.length : (highlight <= 0 ? options.length - 1 : highlight - 1);
      renderList();
    } else if (e.key === 'Enter' || (e.key === 'Tab' && entry.value.trim())) {
      const pick = highlight >= 0 ? options[highlight]?.dataset.tag : entry.value;
      if (pick && pick.trim()) { e.preventDefault(); addTag(pick); }
      else if (e.key === 'Enter') e.preventDefault();
    } else if (e.key === 'Backspace' && !entry.value && chosen.length) {
      chosen.pop();
      renderChips();
      renderList();
    } else if (e.key === 'Escape') {
      if (!$('tag-suggestions').classList.contains('hidden')) { e.stopPropagation(); closeList(); }
    }
  });
  $('tag-input').addEventListener('click', e => { if (e.target === $('tag-input') || e.target === $('tag-chips')) entry.focus(); });
  renderChips();
  try {
    const info = await api<TagInfo>('/api/tags');
    suggested = info.suggested;
    maxTags = info.max_tags;
    maxChars = info.max_chars;
  } catch { suggested = []; }
}
