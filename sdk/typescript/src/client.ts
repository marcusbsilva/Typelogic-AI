/** Typed client of the TypeLogic AI loopback API (`python tools/run_app.py`). Local only: no hosted service. */
import type { ArchiveInfo, CaseRequest, DecisionRequest, DecisionResponse, LiteDecision, StoredDecision } from './types.js';

export interface ClientOptions {
  /** e.g. http://127.0.0.1:8765 — the server only accepts its own loopback Host/Origin. */
  baseUrl?: string;
  /** Session token; when omitted it is read from the served page, exactly as the browser UI does. */
  token?: string;
  fetch?: typeof fetch;
}

export class ApiError extends Error {
  constructor(readonly status: number, message: string, readonly code?: string) { super(message); this.name = 'ApiError'; }
}

export class TypeLogicClient {
  readonly baseUrl: string;
  private token: string | undefined;
  private readonly http: typeof fetch;

  constructor(options: ClientOptions = {}) {
    this.baseUrl = (options.baseUrl || 'http://127.0.0.1:8765').replace(/\/$/, '');
    this.token = options.token;
    this.http = options.fetch || fetch;
    const host = new URL(this.baseUrl).hostname;
    if (host !== '127.0.0.1') throw new Error('TypeLogic AI serves loopback only (127.0.0.1)');
  }

  /** Read the per-process session token from the local page. */
  async connect(): Promise<this> {
    if (this.token) return this;
    const page = await (await this.http(this.baseUrl + '/')).text();
    const match = /<meta name="typelogic-token" content="([^"]+)">/.exec(page);
    if (!match) throw new Error('Session token not found; is this a TypeLogic AI server?');
    this.token = match[1];
    return this;
  }

  private async request<T>(path: string, payload?: unknown): Promise<T> {
    await this.connect();
    const response = await this.http(this.baseUrl + path, {
      method: payload === undefined ? 'GET' : 'POST',
      headers: { 'X-TypeLogic-Token': this.token as string, 'Content-Type': 'application/json' },
      ...(payload === undefined ? {} : { body: JSON.stringify(payload) }),
    });
    const data = await response.json() as T & { error?: string; code?: string };
    if (!response.ok) throw new ApiError(response.status, data.error || `HTTP ${response.status}`, data.code);
    return data;
  }

  status() { return this.request<{ engine: Record<string, unknown>; stats: Record<string, number> }>('/api/status'); }
  load() { return this.request<{ loading: boolean; status: string }>('/api/load', {}); }
  evaluate(request: DecisionRequest | CaseRequest) { return this.request<DecisionResponse>('/api/evaluate', request); }
  /** Decisions of a workspace, newest first; with a tag, only those carrying it. */
  decisions(space = 'default', tag?: string) {
    return this.request<{ decisions: StoredDecision[] }>(`/api/decisions?space=${encodeURIComponent(space)}`)
      .then(r => tag === undefined ? r.decisions : r.decisions.filter(d => (d.request.tags || []).includes(tag.trim().split(/\s+/).join(' ').toLowerCase())));
  }
  decision(id: string): Promise<StoredDecision>;
  decision(id: string, format: 'lite'): Promise<LiteDecision>;
  decision(id: string, format: 'full' | 'lite' = 'full') { return this.request<StoredDecision | LiteDecision>(`/api/decision?id=${encodeURIComponent(id)}&format=${format}`); }
  /** Delete one decision with its corrections and audit records. Never executes external actions. */
  delete(id: string) { return this.request<{ id: string; deleted: boolean; external_action_executed: false }>('/api/delete', { id }); }
  /** Delete several decisions; each one reports its own result. */
  deleteMany(ids: string[]) { return this.request<{ deleted: number; results: { id: string; deleted?: boolean; error?: string }[] }>('/api/delete', { ids }); }
  /** Suggested tags and limits. */
  tags() { return this.request<{ suggested: string[]; max_tags: number; max_chars: number }>('/api/tags'); }
  /** Library limit (200 decisions) and the archive that would be written. */
  archiveInfo() { return this.request<ArchiveInfo>('/api/archive'); }
  /** Compress every decision into <project>/data/DD-MM-YYYY.rar (or .zip) and remove them from the library. */
  archive() { return this.request<{ path: string; name: string; format: 'rar' | 'zip'; decisions: number }>('/api/archive', { confirm: true }); }
  /** Store an observed label; the model is not retrained. */
  feedback(id: string, questionId: string, target: Record<string, number>) { return this.request<{ saved: boolean; retrained: false }>('/api/feedback', { id, question_id: questionId, target }); }
  export() { return this.request<Record<string, unknown>>('/api/export'); }
}
