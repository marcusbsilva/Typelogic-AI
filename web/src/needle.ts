import type { NeedleStatus } from './types';
import { api } from './dom';

// Needle 3 status line on the Engine & privacy page. Evidence extraction itself lives in casebuilder.ts.

export const CHECK_LABELS: Record<string, string> = {
  in_text: 'Written in the scenario', not_in_text: 'Not found in the scenario - check it',
  inferred: 'Inferred - check it', not_stated: 'Not stated',
};

export async function loadNeedle(): Promise<NeedleStatus | null> {
  let status: NeedleStatus | null = null;
  try { status = await api<NeedleStatus>('/api/needle'); } catch { status = null; }
  const info = document.getElementById('needle-settings');
  if (info) info.textContent = !status ? 'Status unavailable.' : status.available
    ? `Installed (models/needle, hashes verified on first use). Used for short texts, up to 1,500 characters. Network: none - TypeLogic uses only the native Needle engine; the needle Python package and its telemetry are not used.`
    : `${status.reason} Network: none - the needle Python package and its telemetry are never used.`;
  return status;
}
