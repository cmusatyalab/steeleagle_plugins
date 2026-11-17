import { useEffect, useRef, useState } from 'react';
import { Button } from 'primereact/button';
import { InputText } from 'primereact/inputtext';
import { Tag } from 'primereact/tag';
import { getApiUrl } from './urls.js';

const endpoint = '/api/ai/credentials';
const providers = { openai: 'OpenAI', anthropic: 'Anthropic' };
const loopbackHosts = ['localhost', '127.0.0.1', '[::1]'];

async function readJSON(path, options) {
    const response = await fetch(getApiUrl(path), { cache: 'no-store', ...options });
    if (!response.ok) throw new Error('Request failed');
    return response.json();
}

function CredentialField({ name, status, canManage, disabled, onUpdate }) {
    const [key, setKey] = useState('');
    const [message, setMessage] = useState(null);
    const label = providers[name];
    const saved = status.source === 'saved';
    const source = saved ? 'saved on this GCS' : 'from the environment';

    async function save(event) {
        event.preventDefault();
        if (!disabled && key.trim()) await update(key.trim());
    }

    async function update(nextKey) {
        setMessage(null);
        const clearing = nextKey == null;
        if (await onUpdate(name, nextKey)) {
            setKey('');
            setMessage({ text: clearing ? `${label} saved key cleared.` : `${label} key saved.` });
        } else {
            setMessage({ error: true, text: `Could not ${clearing ? 'clear' : 'save'} the ${label} key.` });
        }
    }

    return (
        <div className="flex flex-column gap-2">
            <div>
                <Tag value={status.configured ? `${label} key configured (${source})` : `${label} key missing`}
                    severity={status.configured ? 'success' : name === 'openai' ? 'warning' : 'secondary'} />
            </div>
            {canManage && <form className="flex align-items-end flex-wrap gap-2" onSubmit={save}>
                <label className="flex flex-column gap-1">
                    <span className="text-sm">{label} API key</span>
                    <InputText type="password" value={key} onChange={event => setKey(event.target.value)}
                        autoComplete="new-password" spellCheck={false} autoCapitalize="none" disabled={disabled} />
                </label>
                <Button type="submit" size="small" label={saved ? 'Replace key' : 'Save key'}
                    aria-label={`${saved ? 'Replace' : 'Save'} ${label} key`} disabled={disabled || !key.trim()} />
                {saved && <Button type="button" size="small" outlined severity="secondary" label="Clear saved key"
                    aria-label={`Clear saved ${label} key`} disabled={disabled} onClick={() => update(null)} />}
            </form>}
            {message && <small role={message.error ? 'alert' : 'status'} className={message.error ? 'p-error' : 'text-color-secondary'}>
                {message.text}
            </small>}
        </div>
    );
}

export default function GCSCredentials({ disabled }) {
    const [status, setStatus] = useState(null);
    const [chat, setChat] = useState(null);
    const [pending, setPending] = useState(false);
    const controller = useRef(null);

    useEffect(() => {
        const abort = new AbortController();
        controller.current = abort;
        const { signal } = abort;
        readJSON(endpoint, { signal })
            .then(data => { if (!signal.aborted) setStatus(data); })
            .catch(() => { if (!signal.aborted) setStatus({}); });
        readJSON('/api/chat/status', { signal })
            .then(data => { if (!signal.aborted) setChat(data); })
            .catch(() => {});
        return () => abort.abort();
    }, []);

    async function updateKey(provider, key) {
        const signal = controller.current.signal;
        const options = key == null ? { method: 'DELETE' } : {
            method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key }),
        };
        setPending(true);
        try {
            const updated = await readJSON(`${endpoint}/${provider}`, { ...options, signal });
            if (!signal.aborted) setStatus(updated);
            return true;
        } catch {
            return false;
        } finally {
            if (!signal.aborted) setPending(false);
        }
    }

    const localPage = loopbackHosts.includes(window.location.hostname);
    const canManage = localPage && status?.can_manage === true;
    return (
        <div className="flex flex-column gap-2" aria-label="API credentials">
            <span className="font-semibold text-sm">API credentials</span>
            <small className="text-color-secondary">OpenAI supplies vision and OpenAI Chat. Anthropic supplies Anthropic Chat.</small>
            {status?.openai && status?.anthropic ? <>
                {canManage && <small className="text-color-secondary">
                    Saving a key takes effect immediately. Clearing a saved key restores its environment value if one is set.
                </small>}
                {localPage && !canManage && <small className="text-color-secondary">
                    Key editing is unavailable from this connection. Open the GCS directly and check that its private
                    credential storage is available.
                </small>}
                {!localPage && <small className="text-color-secondary">
                    To edit keys, open the GCS at <code>http://localhost</code> on its configured port. From another machine,
                    forward that port over SSH and open the forwarded localhost address.
                </small>}
                {Object.keys(providers).map(name => (
                    <CredentialField key={name} name={name} status={status[name]}
                        canManage={canManage} disabled={disabled || pending} onUpdate={updateKey} />
                ))}
            </> : <small className="text-color-secondary" role="status">
                {status ? 'Credential status unavailable.' : 'Checking credentials…'}
            </small>}
            {chat?.model && <small className="text-color-secondary">
                Running Chat model: {chat.model}{chat.temperature != null && chat.temperature !== '' ? ` · temperature ${chat.temperature}` : ''}
            </small>}
        </div>
    );
}
