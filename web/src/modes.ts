// Engine mode selector (Engine & privacy): CPU, GPU or Hybrid, saved by the server in <data>/settings.json.
import type { ModeInfo } from './types';
import { $, $button, $select, api, esc, reportError, toast } from './dom';
import { refreshStatus } from './status';

const MODES = ['cpu', 'gpu', 'hybrid'] as const;
const LABELS: Record<string, string> = { cpu: 'CPU', gpu: 'GPU', hybrid: 'Hybrid', custom: 'Custom' };
let current: ModeInfo | null = null;

export const modeLabel = (mode: string | undefined) => (mode && LABELS[mode]) || '';

const chosen = (): string => (document.querySelector<HTMLInputElement>('input[name="engine-mode"]:checked')?.value) || 'cpu';

function syncHybridOptions(): void {
  const hybrid = chosen() === 'hybrid';
  $('hybrid-options').classList.toggle('hidden', !hybrid);
  const gpuOption = $select('hybrid-device').querySelector<HTMLOptionElement>('option[value="gpu"]');
  if (gpuOption) gpuOption.disabled = !current?.availability.hybrid.gpu_device;
}

export async function loadModes(): Promise<void> {
  current = await api<ModeInfo>('/api/modes');
  const info = current;
  $('mode-current').textContent = `Current mode: ${modeLabel(info.mode) || info.mode}${info.engine_status ? ' · engine ' + info.engine_status.replaceAll('_', ' ') : ''}`;
  for (const mode of MODES) {
    const option = info.availability[mode];
    const input = document.querySelector<HTMLInputElement>(`input[name="engine-mode"][value="${mode}"]`);
    if (!input) continue;
    input.checked = info.mode === mode;
    input.disabled = !option.available && info.mode !== mode;
    input.closest('label')?.classList.toggle('unavailable', !option.available);
    $(`mode-detail-${mode}`).textContent = option.detail || '';
  }
  $select('hybrid-device').value = info.settings.hybrid_device;
  $select('hybrid-combine').value = info.settings.hybrid_combine;
  ($('ollama-model') as HTMLInputElement).value = info.settings.ollama_model;
  $('ollama-models').innerHTML = (info.availability.hybrid.models || []).map(m => `<option value="${esc(m)}"></option>`).join('');
  syncHybridOptions();
}

async function waitForRestart(): Promise<void> {
  // The app exits and the launcher starts it again in the GPU environment with a new session token: reload when the
  // page served at / carries a different token.
  const status = $('mode-status');
  const token = (document.querySelector('meta[name="typelogic-token"]') as HTMLMetaElement).content;
  for (let i = 0; i < 240; i++) {
    await new Promise(r => setTimeout(r, 1500));
    try {
      const html = await (await fetch('/', { cache: 'no-store' })).text();
      const next = /name="typelogic-token" content="([^"]+)"/.exec(html)?.[1];
      if (next && next !== token) { window.location.reload(); return; }
    } catch { status.textContent = 'Restarting TypeLogic AI in the GPU environment…'; }
  }
  status.textContent = 'The app did not come back. Start it again with TypeLogic.bat.';
}

async function applyMode(): Promise<void> {
  const button = $button('apply-mode'), status = $('mode-status');
  const request = {
    mode: chosen(), hybrid_device: $select('hybrid-device').value, hybrid_combine: $select('hybrid-combine').value,
    ollama_model: ($('ollama-model') as HTMLInputElement).value.trim() || undefined,
  };
  button.disabled = true;
  status.textContent = 'Applying…';
  try {
    const result = await api<{ mode: string; restarting: boolean; note?: string | null }>('/api/mode', request);
    if (result.restarting) {
      status.textContent = 'Saved. Restarting TypeLogic AI in the GPU environment; this page reloads by itself.';
      await waitForRestart();
      return;
    }
    status.textContent = `Mode ${modeLabel(result.mode)} saved. Loading the engine…${result.note ? ' ' + result.note + '.' : ''}`;
    toast(`Engine mode: ${modeLabel(result.mode)}`);
    await refreshStatus();
    await loadModes();
  } catch (e) {
    status.textContent = '';
    reportError(e);
  } finally {
    button.disabled = false;
  }
}

export function installModes(): void {
  document.querySelectorAll<HTMLInputElement>('input[name="engine-mode"]').forEach(n => n.addEventListener('change', syncHybridOptions));
  $('apply-mode').addEventListener('click', () => { applyMode().catch(reportError); });
  loadModes().catch(reportError);
}
