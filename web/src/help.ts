import { $, esc } from './dom';

// Circled help: hover, focus, click-to-pin and Escape, without layout shift (AGENTS.md interface rules).

export const helpTexts: Record<string, string> = {
  json: 'Select JSON scenario for a valid object or array, such as {"order":42,"paid":true}. Leave it unchecked for ordinary text. Object keys are sorted before evaluation. JSON organizes facts; it does not compute values or guarantee accuracy.',
  case: 'The case is what happened: the original message, e-mail, tool call or scenario, as one text. The title is optional: when you leave it empty, a short title is written after the evaluation (by the local Ollama model when it is already running, otherwise from the first sentence of the case). Tags group decisions in the Library: pick suggested tags or type your own, up to 8. Click the header to collapse or expand this block.',
  decisions: 'Each decision is one question about the case: Yes / no, Choice or Score. Decisions are answered independently, in order, and the answers keep the question text. Add up to 32. Save as template keeps the case layout, evidence fields, tags and decisions for later. Click the header to collapse or expand this block.',
  results: 'The answers of the last evaluation: for each decision the most likely answer, the probability of every alternative, confidence (the top probability, not a measured accuracy) and concentration. The decision is saved in the Library with its title, tags, a unique ID and SHA-512 / MD5 digests. Export Full or Lite JSON here, or delete the decision.',
  types: 'Click Yes / no, Choice or Score to choose the output type of this decision. Yes / no returns the probability that the question is true. Choice picks one of your numbered options (0, 1, 2, ...) and returns a probability for each. Score rates the case on your numbered levels, from lowest to highest, and returns the expected level. You can switch the type at any time; the question text is kept.',
  order: 'The primary engine repeats each Choice question with reversed options and one rotation, remaps probabilities to the original IDs and compares them. A winner change or any probability shift greater than 0.15 is flagged. This costs additional time; two stable permutations do not prove robustness. The fixed-profile specialist does not perform this test.',
  evidence: 'Optional. Evidence holds the facts that matter as typed fields: true/false, numbers, integers, dates, text and enums. Template values are marked template; values you type are confirmed; values from Extract evidence are suggestions marked extracted until you keep (✓) or correct them. Extraction uses the local LLM in Ollama (qwen3.5:4b by default) or, for short texts, Needle. The engine reads the case and the evidence together. On the held-out tests the evidence did not change model accuracy: its value is an auditable record of the facts. Unconfirmed or missing required evidence is flagged on every answer.',
  needle: 'Optional. Needle 3, a 121M-parameter model by Cactus Compute, runs on this computer and fills the fields you declare from the scenario text. Each value is then checked in code: written in the scenario, not found, or inferred (yes/no fields). The model can omit or misread values, so they are unverified; when you add them to the decision they enter the scenario as model-extracted data and every answer is flagged. No network: only the native engine is used.',
  integrity: 'SHA-512 and MD5 are recomputed from the immutable record: ID, workspace, title, creation time, request and response. JSON uses sorted keys, compact separators, UTF-8 and finite numbers. Digests are compared with the stored baseline. MD5 is a compatibility checksum with known collision weaknesses. These checks are not signatures or proof of factual correctness; someone able to change both record and baseline can bypass them.',
};

let helpAnchor: HTMLElement | null = null;
let helpPinned = false;
let helpCloseTimer: number | undefined;

function closeHelp(): void {
  clearTimeout(helpCloseTimer);
  if (helpAnchor) {
    helpAnchor.setAttribute('aria-expanded', 'false');
    helpAnchor.removeAttribute('aria-describedby');
  }
  helpAnchor = null;
  helpPinned = false;
  $('help-popup').hidden = true;
}

function positionHelp(): void {
  if (!helpAnchor) return;
  const popup = $('help-popup'), r = helpAnchor.getBoundingClientRect();
  popup.style.left = `${Math.max(8, Math.min(r.left, innerWidth - popup.offsetWidth - 8))}px`;
  popup.style.top = `${Math.max(8, r.bottom + 8 + popup.offsetHeight < innerHeight ? r.bottom + 8 : r.top - popup.offsetHeight - 8)}px`;
}

function openHelp(button: HTMLElement, pin = false): void {
  clearTimeout(helpCloseTimer);
  if (helpAnchor !== button) closeHelp();
  helpAnchor = button;
  helpPinned = helpPinned || pin;
  const popup = $('help-popup');
  popup.textContent = button.dataset.helpText || helpTexts[button.dataset.help || ''] || '';
  popup.hidden = false;
  button.setAttribute('aria-expanded', 'true');
  button.setAttribute('aria-describedby', 'help-popup');
  positionHelp();
}

function laterCloseHelp(): void {
  if (!helpPinned) helpCloseTimer = window.setTimeout(closeHelp, 150);
}

const helpTarget = (event: Event): HTMLElement | null => (event.target instanceof Element ? event.target.closest<HTMLElement>('.help-icon') : null);

export function installHelp(): void {
  document.addEventListener('mouseover', e => { const button = helpTarget(e); if (button && (!helpPinned || button === helpAnchor)) openHelp(button); });
  document.addEventListener('mouseout', e => { if (helpTarget(e)) laterCloseHelp(); });
  document.addEventListener('focusin', e => { if (e.target instanceof HTMLElement && e.target.matches('.help-icon')) openHelp(e.target); });
  document.addEventListener('focusout', e => { if (e.target instanceof HTMLElement && e.target.matches('.help-icon')) laterCloseHelp(); });
  document.addEventListener('click', e => {
    const button = helpTarget(e);
    if (button) {
      e.preventDefault();
      e.stopPropagation();
      if (helpAnchor === button && helpPinned) closeHelp(); else openHelp(button, true);
    } else if (!(e.target instanceof Element && e.target.closest('#help-popup'))) closeHelp();
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeHelp(); });
  $('help-popup').addEventListener('mouseenter', () => clearTimeout(helpCloseTimer));
  $('help-popup').addEventListener('mouseleave', laterCloseHelp);
  window.addEventListener('resize', positionHelp);
  window.addEventListener('scroll', positionHelp, true);
}

export function helpGlyph(): string {
  return '<svg aria-hidden="true" viewBox="0 0 20 20"><circle cx="10" cy="10" r="8.5" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M10 5.5v5.7" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><circle cx="10" cy="14.4" r="1" fill="currentColor"/></svg>';
}

export function helpIcon(text: string, label = 'Explain this indicator'): string {
  return `<button type="button" class="help-icon" aria-label="${esc(label)}" aria-expanded="false" data-help-text="${esc(text)}">${helpGlyph()}</button>`;
}
