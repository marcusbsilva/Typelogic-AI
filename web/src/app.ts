import type { ArchiveInfo, ArchiveResult, DecisionResponse } from './types';
import { $, $button, $select, api, download, errorMessage, esc, reportError, toast } from './dom';
import { helpGlyph, installHelp } from './help';
import { addQuestion, loadQuestions } from './questions';
import { buildCaseRequest, installCaseBuilder } from './casebuilder';
import { renderResult } from './render';
import { exportTraining, installLibrary, refreshHistory, showLibraryList } from './history';
import { engineProfile, isEngineReady, refreshStatus, setEvaluating } from './status';
import { installModes, loadModes } from './modes';
import { loadNeedle } from './needle';
import { installEvaluate, loadEvaluate } from './evaluate';

// Navigation, engine status polling and wiring. Entry point of the compiled app.js.


const PAGE_TITLES: Record<string, string> = {
  decide: 'New decision', library: 'Library', evaluate: 'Evaluate', settings: 'Engine & privacy', about: 'About TypeLogic AI',
};

function showPage(page: string): void {
  window.scrollTo({ top: 0, behavior: 'instant' });
  document.querySelectorAll<HTMLElement>('.page').forEach(n => n.classList.toggle('hidden', n.id !== `page-${page}`));
  document.querySelectorAll<HTMLElement>('.nav').forEach(n => n.classList.toggle('active', n.dataset.page === page));
  $('page-title').textContent = PAGE_TITLES[page];
  if (page === 'library') { showLibraryList(); refreshHistory().catch(reportError); }
  if (page === 'settings') { loadModes().catch(reportError); loadNeedle(); }
  if (page === 'evaluate') loadEvaluate().catch(reportError);
  refreshStatus().catch(reportError);
}

async function loadSettings(): Promise<void> {
  const settings = await api<{ extract_backend: string }>('/api/settings');
  $select('extract-backend').value = settings.extract_backend;
}

// ---------------------------------------------------------------- library limit

/** Ask before archiving: resolves true when the user chose "Archive and continue". */
function confirmArchive(info: ArchiveInfo): Promise<boolean> {
  const dialog = $('archive-dialog') as HTMLDialogElement;
  $('archive-text').textContent = `The library holds ${info.count} decisions; the maximum is ${info.limit}. To continue, all decision files and data will be compressed into one ${info.format === 'rar' ? '.rar' : '.zip'} file, ${info.name}, saved in the data folder of the TypeLogic AI project (${info.folder}), and removed from the library.`;
  $('archive-note').textContent = info.note ? `${info.note}. Install WinRAR to get .rar files.` : 'The archive is written and tested before anything is removed.';
  return new Promise(resolve => {
    const done = (value: boolean) => { dialog.close(); resolve(value); };
    $('archive-confirm').onclick = () => done(true);
    $('archive-cancel').onclick = () => done(false);
    dialog.oncancel = e => { e.preventDefault(); done(false); };
    dialog.showModal();
  });
}

/** Archive the library when it is full (after the user agrees). False when the user cancelled or archiving failed. */
async function ensureRoom(): Promise<boolean> {
  const info = await api<ArchiveInfo>('/api/archive');
  if (!info.full) return true;
  if (!(await confirmArchive(info))) { toast('Not evaluated: the library is full. Archive it to continue, or delete decisions in the Library.'); return false; }
  $('results').classList.remove('empty');
  $('results').innerHTML = `<div class="loading"><span class="spinner"></span>Compressing ${info.count} decisions into ${esc(info.name)}…</div>`;
  const result = await api<ArchiveResult>('/api/archive', { confirm: true });
  toast(`${result.decisions} decisions archived to ${result.path}. The library starts empty.`);
  await refreshStatus();
  return true;
}

function installDecide(): void {
  $('add-question').addEventListener('click', () => addQuestion());
  installCaseBuilder();
  $select('extract-backend').addEventListener('change', () => api('/api/settings', { extract_backend: $select('extract-backend').value }).catch(reportError));
  loadSettings().catch(reportError);
  $('preview-request').addEventListener('click', () => {
    try { $('request-output').textContent = JSON.stringify(buildCaseRequest(), null, 2); } catch (e) { reportError(e); }
  });
  $('copy-request').addEventListener('click', async () => {
    try {
      const text = JSON.stringify(buildCaseRequest(), null, 2);
      $('request-output').textContent = text;
      if (!navigator.clipboard?.writeText) throw Error('Clipboard unavailable. Select and copy the request preview.');
      await navigator.clipboard.writeText(text);
      toast('Request JSON copied; no evaluation or record was created.');
    } catch (e) { reportError(e); }
  });
  $('evaluate').addEventListener('click', async () => {
    try {
      const payload = buildCaseRequest();
      setEvaluating(true);
      $button('evaluate').disabled = true;
      if (!(await ensureRoom())) return;
      $('results').classList.remove('empty');
      $('results').innerHTML = '<div class="loading"><span class="spinner"></span>Evaluating on your computer…<p class="micro">The first load takes longer. Order checks run additional passes. Without a title, a short one is written at the end.</p></div>';
      renderResult(await api<DecisionResponse>('/api/evaluate', payload));
      await refreshStatus();
    } catch (e) {
      $('results').innerHTML = `<div class="error-box">${esc(errorMessage(e))}</div>`;
    } finally {
      setEvaluating(false);
      $button('evaluate').disabled = !isEngineReady();
    }
  });
}

function installSettings(): void {
  installModes();
  installEvaluate();
  $('retry-engine').addEventListener('click', async () => {
    try { await api('/api/load', {}); await refreshStatus(); } catch (e) { reportError(e); }
  });
  $('reload-status').addEventListener('click', () => refreshStatus().catch(reportError));
  $('apply-profile').addEventListener('click', () => {
    const profile = engineProfile();
    if (!profile) return;
    loadQuestions(profile);
    showPage('decide');
    toast('Profile questions applied');
  });
  $('refresh-history').addEventListener('click', () => refreshHistory().catch(reportError));
  const closeMenu = () => document.querySelectorAll<HTMLDetailsElement>('details.menu').forEach(m => { m.open = false; });
  $('export-training').addEventListener('click', () => { closeMenu(); exportTraining().catch(reportError); });
  $('export-all').addEventListener('click', async () => {
    closeMenu();
    try { download('typelogic-export.json', await api('/api/export')); } catch (e) { reportError(e); }
  });
  document.addEventListener('click', e => { if (!(e.target instanceof Element && e.target.closest('details.menu'))) closeMenu(); });
}

function installTheme(): void {
  const picker = $select('theme-select');
  picker.value = window.TypeLogicTheme.get();
  picker.addEventListener('change', () => window.TypeLogicTheme.set(picker.value));
  window.addEventListener('typelogic-theme-change', () => { picker.value = window.TypeLogicTheme.get(); });
}

installHelp();
installTheme();
document.querySelectorAll<HTMLElement>('.nav').forEach(n => n.addEventListener('click', () => showPage(n.dataset.page || 'decide')));
$('space').addEventListener('change', () => { refreshHistory().catch(reportError); });
installDecide();
installLibrary();
installSettings();
refreshStatus().catch(reportError);
document.querySelectorAll<HTMLElement>('.help-icon[data-help]').forEach(button => { button.innerHTML = helpGlyph(); });
