// Uploading files and models to a daemon over the GCS's
// /ws/daemons/transfer websocket (see internal/daemons/transfer.go), and the
// Orchestrate page's list of the uploads made since it opened.

// The websocket path that uploads a file of size bytes to the daemon at ip,
// as a file or a model, saved as name.
export function transferPath(ip, kind, name, size) {
    const q = new URLSearchParams({ ip, kind, name, size: String(size) });
    return `/ws/daemons/transfer?${q}`;
}

// Uploads file over a transfer socket at url, sending a chunk of the size the
// GCS asks for after each one the daemon acknowledges, and calling
// onProgress(sent, total) in bytes as it does. done resolves with {size} once
// the daemon has saved the file, or rejects with the reason it wasn't;
// cancel() abandons the upload, and done then rejects with an error whose
// cancelled is true.
export function startTransfer({ url, file, onProgress, WebSocketImpl = WebSocket }) {
    const ws = new WebSocketImpl(url);
    let settled = false;
    let resolve;
    let reject;
    const done = new Promise((res, rej) => {
        resolve = res;
        reject = rej;
    });
    const finish = (fn, value) => {
        if (settled) return;
        settled = true;
        fn(value);
        ws.close();
    };
    let chunkSize = 0;
    const sendFrom = (offset) => ws.send(file.slice(offset, Math.min(offset + chunkSize, file.size)));

    ws.onmessage = (e) => {
        let msg;
        try {
            msg = JSON.parse(e.data);
        } catch {
            finish(reject, new Error('The GCS sent an unreadable message'));
            return;
        }
        switch (msg.type) {
            case 'ready':
                chunkSize = msg.chunk_size;
                sendFrom(0);
                break;
            case 'progress':
                onProgress(msg.sent, msg.total);
                sendFrom(msg.sent);
                break;
            case 'done':
                onProgress(msg.size, msg.size);
                finish(resolve, { size: msg.size });
                break;
            case 'error':
                finish(reject, new Error(msg.detail));
                break;
            default:
                break;
        }
    };
    ws.onclose = () => finish(reject, new Error('The connection to the GCS closed before the upload finished'));

    const cancel = () => {
        const err = new Error('Cancelled');
        err.cancelled = true;
        finish(reject, err);
    };
    return { done, cancel };
}

export const FILE_NAME_RULE = "a plain file name, without '/' or '\\', and not '.' or '..'";

// Returns null for a name the daemon accepts, else the rule. Mirrors the
// daemon's CheckFilename.
export function validFileName(name) {
    if (!name || name === '.' || name === '..' || /[/\\\0]/.test(name)) return FILE_NAME_RULE;
    return null;
}

// The upload list is {ip: [upload, ...]}, newest first. An upload is
// {id, name, kind, size, sent, status: 'uploading' | 'done' | 'failed' |
// 'cancelled', detail}.

export function addUpload(uploads, ip, upload) {
    return { ...uploads, [ip]: [upload, ...(uploads[ip] ?? [])] };
}

export function patchUpload(uploads, ip, id, patch) {
    return { ...uploads, [ip]: (uploads[ip] ?? []).map((u) => (u.id === id ? { ...u, ...patch } : u)) };
}

export function isUploading(uploads, ip) {
    return (uploads[ip] ?? []).some((u) => u.status === 'uploading');
}
