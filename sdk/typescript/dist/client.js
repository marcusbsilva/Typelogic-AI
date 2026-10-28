export class ApiError extends Error {
    status;
    code;
    constructor(status, message, code) {
        super(message);
        this.status = status;
        this.code = code;
        this.name = 'ApiError';
    }
}
export class TypeLogicClient {
    baseUrl;
    token;
    http;
    constructor(options = {}) {
        this.baseUrl = (options.baseUrl || 'http://127.0.0.1:8765').replace(/\/$/, '');
        this.token = options.token;
        this.http = options.fetch || fetch;
        const host = new URL(this.baseUrl).hostname;
        if (host !== '127.0.0.1')
            throw new Error('TypeLogic AI serves loopback only (127.0.0.1)');
    }
    /** Read the per-process session token from the local page. */
    async connect() {
        if (this.token)
            return this;
        const page = await (await this.http(this.baseUrl + '/')).text();
        const match = /<meta name="typelogic-token" content="([^"]+)">/.exec(page);
        if (!match)
            throw new Error('Session token not found; is this a TypeLogic AI server?');
        this.token = match[1];
        return this;
    }
    async request(path, payload) {
        await this.connect();
        const response = await this.http(this.baseUrl + path, {
            method: payload === undefined ? 'GET' : 'POST',
            headers: { 'X-TypeLogic-Token': this.token, 'Content-Type': 'application/json' },
            ...(payload === undefined ? {} : { body: JSON.stringify(payload) }),
        });
        const data = await response.json();
        if (!response.ok)
            throw new ApiError(response.status, data.error || `HTTP ${response.status}`, data.code);
        return data;
    }
    status() { return this.request('/api/status'); }
    load() { return this.request('/api/load', {}); }
    evaluate(request) { return this.request('/api/evaluate', request); }
    /** Decisions of a workspace, newest first; with a tag, only those carrying it. */
    decisions(space = 'default', tag) {
        return this.request(`/api/decisions?space=${encodeURIComponent(space)}`)
            .then(r => tag === undefined ? r.decisions : r.decisions.filter(d => (d.request.tags || []).includes(tag.trim().split(/\s+/).join(' ').toLowerCase())));
    }
    decision(id, format = 'full') { return this.request(`/api/decision?id=${encodeURIComponent(id)}&format=${format}`); }
    /** Delete one decision with its corrections and audit records. Never executes external actions. */
    delete(id) { return this.request('/api/delete', { id }); }
    /** Delete several decisions; each one reports its own result. */
    deleteMany(ids) { return this.request('/api/delete', { ids }); }
    /** Suggested tags and limits. */
    tags() { return this.request('/api/tags'); }
    /** Library limit (200 decisions) and the archive that would be written. */
    archiveInfo() { return this.request('/api/archive'); }
    /** Compress every decision into <project>/data/DD-MM-YYYY.rar (or .zip) and remove them from the library. */
    archive() { return this.request('/api/archive', { confirm: true }); }
    /** Store an observed label; the model is not retrained. */
    feedback(id, questionId, target) { return this.request('/api/feedback', { id, question_id: questionId, target }); }
    export() { return this.request('/api/export'); }
}
