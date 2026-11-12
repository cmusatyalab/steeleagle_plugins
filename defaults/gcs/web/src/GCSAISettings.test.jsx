// @vitest-environment jsdom
import { act, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import ArgsEditor from './ArgsEditor.jsx';
import { argsState, argsValue } from './configForm.js';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const manifest = { args: [{ name: 'ai', type: 'object', default: {} }] };
let container;
let root;

beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
});

afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
});

function Form({ initial = {}, currentGCS = false, onChange = () => {} }) {
    const [args, setArgs] = useState(() => argsState(initial, manifest));
    return <ArgsEditor args={args} gcsAI currentGCS={currentGCS} onChange={state => {
        setArgs(state);
        onChange(argsValue(state));
    }} />;
}

it('preserves other plugin and inference args when a provider is selected', async () => {
    const original = {
        ai: { future_setting: 'keep me', chat_temperature: 0, inferred_mission: { history_sections: 5, future_limit: 42 } },
        http_addr: ':8080',
    };
    const changed = vi.fn();
    await act(async () => root.render(<Form initial={original} onChange={changed} />));
    const provider = container.querySelector('select');
    expect(provider.value).toBe('openai');
    await act(async () => {
        provider.value = 'anthropic';
        provider.dispatchEvent(new Event('change', { bubbles: true }));
    });
    expect(changed).toHaveBeenLastCalledWith({
        value: { ...original, ai: { ...original.ai, chat_provider: 'anthropic' } },
        errors: {},
    });
    expect(container.textContent).toContain('ANTHROPIC_API_KEY');
    expect(container.querySelector('input[type="password"]')).toBeNull();
});

it('shows credential status from the current GCS and skips the request for a remote service', async () => {
    const fetch = vi.fn(async () => ({
        ok: true,
        json: async () => ({
            model: 'local-example',
            temperature: 0,
            credentials: { openai_configured: true, anthropic_configured: false },
        }),
    }));
    vi.stubGlobal('fetch', fetch);
    await act(async () => root.render(<Form />));
    expect(fetch).not.toHaveBeenCalled();
    expect(container.textContent).not.toContain('key configured');
    await act(async () => root.render(<Form currentGCS />));
    expect(fetch).toHaveBeenCalledOnce();
    expect(fetch.mock.calls[0][0]).toContain('/api/chat/status');
    expect(container.textContent).toContain('OpenAI key configured');
    expect(container.textContent).toContain('Anthropic key missing');
    expect(container.textContent).toContain('local-example · temperature 0');
});
