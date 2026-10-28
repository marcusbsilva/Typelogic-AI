/** Typed client of the TypeLogic AI loopback API (`python tools/run_app.py`). Local only: no hosted service. */
import type { ArchiveInfo, CaseRequest, DecisionRequest, DecisionResponse, LiteDecision, StoredDecision } from './types.js';
export interface ClientOptions {
    /** e.g. http://127.0.0.1:8765 — the server only accepts its own loopback Host/Origin. */
    baseUrl?: string;
    /** Session token; when omitted it is read from the served page, exactly as the browser UI does. */
    token?: string;
    fetch?: typeof fetch;
}
export declare class ApiError extends Error {
    readonly status: number;
    readonly code?: string | undefined;
    constructor(status: number, message: string, code?: string | undefined);
}
export declare class TypeLogicClient {
    readonly baseUrl: string;
    private token;
    private readonly http;
    constructor(options?: ClientOptions);
    /** Read the per-process session token from the local page. */
    connect(): Promise<this>;
    private request;
    status(): Promise<{
        engine: Record<string, unknown>;
        stats: Record<string, number>;
    }>;
    load(): Promise<{
        loading: boolean;
        status: string;
    }>;
    evaluate(request: DecisionRequest | CaseRequest): Promise<DecisionResponse>;
    /** Decisions of a workspace, newest first; with a tag, only those carrying it. */
    decisions(space?: string, tag?: string): Promise<StoredDecision[]>;
    decision(id: string): Promise<StoredDecision>;
    decision(id: string, format: 'lite'): Promise<LiteDecision>;
    /** Delete one decision with its corrections and audit records. Never executes external actions. */
    delete(id: string): Promise<{
        id: string;
        deleted: boolean;
        external_action_executed: false;
    }>;
    /** Delete several decisions; each one reports its own result. */
    deleteMany(ids: string[]): Promise<{
        deleted: number;
        results: {
            id: string;
            deleted?: boolean;
            error?: string;
        }[];
    }>;
    /** Suggested tags and limits. */
    tags(): Promise<{
        suggested: string[];
        max_tags: number;
        max_chars: number;
    }>;
    /** Library limit (200 decisions) and the archive that would be written. */
    archiveInfo(): Promise<ArchiveInfo>;
    /** Compress every decision into <project>/data/DD-MM-YYYY.rar (or .zip) and remove them from the library. */
    archive(): Promise<{
        path: string;
        name: string;
        format: "rar" | "zip";
        decisions: number;
    }>;
    /** Store an observed label; the model is not retrained. */
    feedback(id: string, questionId: string, target: Record<string, number>): Promise<{
        saved: boolean;
        retrained: false;
    }>;
    export(): Promise<Record<string, unknown>>;
}
