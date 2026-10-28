// DOM helpers, escaping and the authenticated local API client.

export function $(id: string): HTMLElement {
  const node = document.getElementById(id);
  if (!node) throw Error(`Missing interface element #${id}`);
  return node;
}
export const $input = (id: string) => $(id) as HTMLInputElement;
export const $select = (id: string) => $(id) as HTMLSelectElement;
export const $area = (id: string) => $(id) as HTMLTextAreaElement;
export const $button = (id: string) => $(id) as HTMLButtonElement;

const sessionToken: string = (document.querySelector('meta[name="typelogic-token"]') as HTMLMetaElement).content;

const ESCAPES: Record<string, string> = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
/** Escape any value for HTML text and attribute contexts. Every dynamic value passes through here. */
export function esc(value: unknown): string {
  return String(value).replace(/[&<>"']/g, c => ESCAPES[c]);
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export async function api<T>(path: string, payload?: unknown): Promise<T> {
  const response = await fetch(path, {
    method: payload === undefined ? 'GET' : 'POST',
    headers: { 'X-TypeLogic-Token': sessionToken, 'Content-Type': 'application/json' },
    ...(payload === undefined ? {} : { body: JSON.stringify(payload) }),
  });
  const data = await response.json();
  if (!response.ok) throw Error((data && data.error) || `Failure ${response.status}`);
  return data as T;
}

let toastTimer: number | undefined;
export function toast(message: string): void {
  const node = $('toast');
  node.textContent = message;
  node.style.display = 'block';
  clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => { node.style.display = 'none'; }, 7000);
}
export const reportError = (error: unknown) => toast(errorMessage(error));

export function download(name: string, value: unknown): void {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

export function downloadText(name: string, text: string, type = 'application/x-ndjson'): void {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

export const space = () => $input('space').value.trim() || 'default';

/** Same Base32(SHA-256("<id>:<format>")[:5]) scheme as exports.export_filename and the Rust core. */
export async function shortExportName(id: string, format: 'full' | 'lite'): Promise<string> {
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(`${id}:${format}`)));
  const alphabet = 'abcdefghijklmnopqrstuvwxyz234567';
  let buffer = 0, bits = 0, text = '';
  for (const byte of digest.slice(0, 5)) {
    buffer = (buffer << 8) | byte;
    bits += 8;
    while (bits >= 5) { bits -= 5; text += alphabet[(buffer >>> bits) & 31]; }
  }
  return text + '.json';
}

export async function exportDecision(id: string, format: 'full' | 'lite'): Promise<void> {
  try {
    download(await shortExportName(id, format), await api(`/api/decision?id=${encodeURIComponent(id)}&format=${format}`));
  } catch (e) { reportError(e); }
}
