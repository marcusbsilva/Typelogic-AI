// Engine status polling and the small amount of shared interface state.
import type { QuestionSpec, StatusResponse } from './types';
import { $, $button, api, reportError } from './dom';
import { renderEngineProfile } from './render';

const MODE_LABELS: Record<string, string> = { cpu: 'CPU', gpu: 'GPU', hybrid: 'Hybrid' };

let profile: Record<string, QuestionSpec> | null = null;
let statusTimer: number | undefined;
let engineReady = false;
let evaluating = false;

const ENGINE_BADGES: Record<string, string> = { not_loaded: 'Preparing engine…', loading: 'Loading engine…', ready: 'Local engine ready', error: 'Engine unavailable' };

export const isEngineReady = () => engineReady;
export const engineProfile = () => profile;
export function setEvaluating(value: boolean): void { evaluating = value; }

export async function refreshStatus(): Promise<void> {
  clearTimeout(statusTimer);
  const data = await api<StatusResponse>('/api/status'), status = data.engine.status;
  engineReady = status === 'ready';
  const badge = $('engine-badge');
  const mode = data.mode && MODE_LABELS[data.mode];
  badge.textContent = (ENGINE_BADGES[status] || status) + (mode ? ` · ${mode}` : '');
  badge.classList.toggle('ready', status === 'ready');
  $('retry-engine').classList.toggle('hidden', status !== 'error');
  $('startup-status').classList.toggle('hidden', engineReady);
  $('startup-status').textContent = status === 'error'
    ? `Could not load the engine: ${data.engine.error || 'local failure'}. Open Engine & privacy to retry.`
    : 'Loading the local engine automatically. You can enter your scenario while you wait.';
  const evaluate = $button('evaluate');
  evaluate.disabled = !engineReady || evaluating;
  evaluate.textContent = engineReady ? 'Evaluate decision ↗' : status === 'error' ? 'Engine unavailable' : 'Loading engine…';
  const total = data.stats.decisions, limit = data.library_limit ?? 200;
  $('library-count').textContent = String(total);
  $('library-count').title = `${total} of ${limit} decisions in the library`;
  const room = `${total} of ${limit} decisions in the library` + (total >= limit ? ' · full: evaluating offers to archive them' : total >= limit - 20 ? ` · ${limit - total} left before the archive` : '');
  $('library-room').textContent = room;
  $('library-capacity').textContent = `The library keeps up to ${limit} decisions (all workspaces): ${total} so far. At the limit, every decision is compressed into the project data folder and the library starts empty.`;
  renderEngineProfile(data.engine);
  $('engine-details').textContent = JSON.stringify(data.engine, null, 2);
  profile = data.engine.profile || null;
  $('apply-profile').classList.toggle('hidden', !profile);
  if (status === 'loading' || status === 'not_loaded') statusTimer = window.setTimeout(() => refreshStatus().catch(reportError), 1000);
}
