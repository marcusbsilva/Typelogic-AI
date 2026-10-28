import type { Answer, Integrity, LiteDecision, RouteResult, RouterProfile, RouterRequest, Validated, ValidatedQuestion } from './types.js';
interface Addon {
    call(method: string, paramsJson: string): string;
    version(): string;
}
/** Error raised by the core, carrying the Python-compatible error class name (ValueError, RuntimeError, ...). */
export declare class CoreError extends Error {
    readonly type: string;
    constructor(type: string, message: string);
}
/** Load the addon once. Throws with the searched paths when it has not been built for this platform. */
export declare function loadAddon(path?: string): Addon;
/** Generic JSON contract (same as Python `typelogic._core.call` and the Lua module). */
export declare function call<T>(method: string, params?: Record<string, unknown>): T;
export declare const coreVersion: () => string;
export declare const validate: (payload: unknown) => Validated;
export declare const answers: (questions: ValidatedQuestion[], rows: number[][], threshold: number, calibration?: unknown) => Record<string, Answer>;
export declare const canonical: (value: unknown, compact?: boolean) => string;
export declare const digests: (record: Record<string, unknown>) => {
    sha512: string;
    md5: string;
};
export declare const verification: (record: Record<string, unknown>) => Integrity;
export declare const lite: (record: Record<string, unknown>) => LiteDecision;
export declare const exportFilename: (id: string, format?: "full" | "lite") => string;
export declare const hash: (algorithm: "sha256" | "sha512" | "md5" | "blake2s", text: string) => string;
export declare const route: (request: RouterRequest, profiles: RouterProfile[]) => RouteResult;
export {};
