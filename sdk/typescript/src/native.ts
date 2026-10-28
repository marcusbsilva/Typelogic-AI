/** Node.js bindings of the Rust core (`typelogic_core.node`, built from rust/typelogic-node). */
import { createRequire } from 'node:module';
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Answer, Integrity, LiteDecision, RouteResult, RouterProfile, RouterRequest, Validated, ValidatedQuestion } from './types.js';

interface Addon { call(method: string, paramsJson: string): string; version(): string; }

/** Error raised by the core, carrying the Python-compatible error class name (ValueError, RuntimeError, ...). */
export class CoreError extends Error {
  constructor(readonly type: string, message: string) {
    super(message);
    this.name = `CoreError(${type})`;
  }
}

let addon: Addon | null = null;

function candidates(): string[] {
  const here = dirname(fileURLToPath(import.meta.url));
  const list = [process.env.TYPELOGIC_NODE_ADDON, join(here, '../native/typelogic_core.node'), join(here, '../../native/typelogic_core.node')];
  return list.filter((p): p is string => !!p);
}

/** Load the addon once. Throws with the searched paths when it has not been built for this platform. */
export function loadAddon(path?: string): Addon {
  if (addon && !path) return addon;
  const require = createRequire(import.meta.url);
  const tried = path ? [path] : candidates();
  for (const candidate of tried) {
    if (existsSync(candidate)) {
      addon = require(candidate) as Addon;
      return addon;
    }
  }
  throw new Error(`typelogic_core.node not found (searched: ${tried.join(', ')}). Build it with scripts/build-native.`);
}

/** Generic JSON contract (same as Python `typelogic._core.call` and the Lua module). */
export function call<T>(method: string, params: Record<string, unknown> = {}): T {
  const response = JSON.parse(loadAddon().call(method, JSON.stringify(params))) as { ok?: T; error?: { type: string; message: string } };
  if (response.error) throw new CoreError(response.error.type, response.error.message);
  return response.ok as T;
}

export const coreVersion = (): string => loadAddon().version();
export const validate = (payload: unknown): Validated => call('validate', { payload });
export const answers = (questions: ValidatedQuestion[], rows: number[][], threshold: number, calibration: unknown = 'unvalidated'): Record<string, Answer> =>
  call('answers', { questions, rows, threshold, calibration });
export const canonical = (value: unknown, compact = false): string => call('canonical', { value, compact });
export const digests = (record: Record<string, unknown>): { sha512: string; md5: string } => call('digests', { record });
export const verification = (record: Record<string, unknown>): Integrity => call('verification', { record });
export const lite = (record: Record<string, unknown>): LiteDecision => call('lite', { record });
export const exportFilename = (id: string, format: 'full' | 'lite' = 'full'): string => call('export_filename', { id, format });
export const hash = (algorithm: 'sha256' | 'sha512' | 'md5' | 'blake2s', text: string): string => call('hash', { algorithm, text });
export const route = (request: RouterRequest, profiles: RouterProfile[]): RouteResult => call('route', { request, profiles });
