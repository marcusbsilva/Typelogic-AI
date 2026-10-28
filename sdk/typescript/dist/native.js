/** Node.js bindings of the Rust core (`typelogic_core.node`, built from rust/typelogic-node). */
import { createRequire } from 'node:module';
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
/** Error raised by the core, carrying the Python-compatible error class name (ValueError, RuntimeError, ...). */
export class CoreError extends Error {
    type;
    constructor(type, message) {
        super(message);
        this.type = type;
        this.name = `CoreError(${type})`;
    }
}
let addon = null;
function candidates() {
    const here = dirname(fileURLToPath(import.meta.url));
    const list = [process.env.TYPELOGIC_NODE_ADDON, join(here, '../native/typelogic_core.node'), join(here, '../../native/typelogic_core.node')];
    return list.filter((p) => !!p);
}
/** Load the addon once. Throws with the searched paths when it has not been built for this platform. */
export function loadAddon(path) {
    if (addon && !path)
        return addon;
    const require = createRequire(import.meta.url);
    const tried = path ? [path] : candidates();
    for (const candidate of tried) {
        if (existsSync(candidate)) {
            addon = require(candidate);
            return addon;
        }
    }
    throw new Error(`typelogic_core.node not found (searched: ${tried.join(', ')}). Build it with scripts/build-native.`);
}
/** Generic JSON contract (same as Python `typelogic._core.call` and the Lua module). */
export function call(method, params = {}) {
    const response = JSON.parse(loadAddon().call(method, JSON.stringify(params)));
    if (response.error)
        throw new CoreError(response.error.type, response.error.message);
    return response.ok;
}
export const coreVersion = () => loadAddon().version();
export const validate = (payload) => call('validate', { payload });
export const answers = (questions, rows, threshold, calibration = 'unvalidated') => call('answers', { questions, rows, threshold, calibration });
export const canonical = (value, compact = false) => call('canonical', { value, compact });
export const digests = (record) => call('digests', { record });
export const verification = (record) => call('verification', { record });
export const lite = (record) => call('lite', { record });
export const exportFilename = (id, format = 'full') => call('export_filename', { id, format });
export const hash = (algorithm, text) => call('hash', { algorithm, text });
export const route = (request, profiles) => call('route', { request, profiles });
