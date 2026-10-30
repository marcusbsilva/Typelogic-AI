// Engine mode selector (Engine & privacy): Automatic or CPU only, saved by the server in <data>/settings.json.
import type { ModeInfo } from './types';
import { $, $button, api, reportError, toast } from './dom';
import { refreshStatus } from './status';

const MODES = ['auto', 'cpu'] as const;
const LABELS: Record<string, string> = { auto: 'Automatic', cpu: 'CPU only' };

export const modeLabel = (mode: string | undefined) => (mode && LABELS[mode]) || '';

const chosen = (): string => (document.querySelector<HTMLInputElement>('input[name="engine-mode"]:checked')?.value) || 'auto';
const combination = (): string => (document.querySelector<HTMLInputElement>('input[name="engine-combine"]:checked')?.value) || 'route';
let currentMode = '';

export async function loadModes(): Promise<void> {
  const info = await api<ModeInfo>('/api/modes');
  $('mode-current').textContent = `Current mode: ${modeLabel(info.mode) || info.mode}${info.engine_status ? ' · engine ' + info.engine_status.replaceAll('_', ' ') : ''}`;
  for (const mode of MODES) {
    const option = info.availability[mode];
    const input = document.querySelector<HTMLInputElement>(`input[name="engine-mode"][value="${mode}"]`);
    if (!input || !option) continue;
    input.checked = info.mode === mode;
    input.disabled = !option.available && info.mode !== mode;
    input.closest('label')?.classList.toggle('unavailable', !option.available);
    $(`mode-detail-${mode}`).textContent = option.detail || '';
  }
  $('mode-plan').textContent = info.plan ? `Loaded now: ${info.plan}.` : '';
  currentMode = info.mode;
  const combine = info.settings?.combine || 'route';
  for (const input of document.querySelectorAll<HTMLInputElement>('input[name="engine-combine"]')) input.checked = input.value === combine;
}

async function applyMode(): Promise<void> {
  const button = $button('apply-mode'), status = $('mode-status');
  button.disabled = true;
  status.textContent = 'Applying…';
  try {
    const restart = chosen() !== currentMode;
    const result = await api<{ mode: string; loading?: boolean; note?: string | null }>('/api/mode', { mode: chosen(), combine: combination() });
    const how = combination() === 'ensemble' ? 'combined' : 'routed';
    status.textContent = result.loading
      ? `Mode ${modeLabel(result.mode)} saved, questions ${how}. Loading the models…${result.note ? ' ' + result.note + '.' : ''}`
      : `Saved: questions ${how}. The models stay loaded.`;
    toast(restart ? `Engine mode: ${modeLabel(result.mode)}` : `Questions up to 16 options: ${how}`);
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
  $('apply-mode').addEventListener('click', () => { applyMode().catch(reportError); });
  loadModes().catch(reportError);
}
