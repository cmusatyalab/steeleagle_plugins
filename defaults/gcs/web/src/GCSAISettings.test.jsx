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

const keyStatus = (source) => ({ source, configured: source !== 'missing' });
const credentialStatus = (can_manage, openai) => ({ can_manage, openai: keyStatus(openai), anthropic: keyStatus('missing') });
const respond = (data) => ({ ok: true, json: async () => data });

function input(label) {
    return [...container.querySelectorAll('label')].find(element => element.textContent === label)?.control;
}

function button(label) {
    return container.querySelector(`button[aria-label="${label}"]`);
}

function typeValue(element, value) {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(element, value);
    element.dispatchEvent(new Event('input', { bubbles: true }));
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
    expect(container.querySelector('input[type="password"]')).toBeNull();
});

it('preserves malformed inference settings when editing another field', async () => {
    const initial = { ai: { inferred_mission: ['invalid'] } };
    const changed = vi.fn();
    await act(async () => root.render(<Form initial={initial} onChange={changed} />));
    expect(container.querySelector('[role="alert"]').textContent).toContain('Edit the value in Raw TOML');
    expect(input('Section seconds')).toBeUndefined();

    await act(async () => typeValue(input('Vision model'), 'vision-example'));
    expect(changed).toHaveBeenLastCalledWith({
        value: { ai: { ...initial.ai, vision_model: 'vision-example' } },
        errors: {},
    });
});

it('shows current credential status, and hides input when the server denies management', async () => {
    const fetch = vi.fn(async (url) => url.endsWith('/api/chat/status')
        ? respond({ model: 'local-example', temperature: 0 }) : respond(credentialStatus(false, 'environment')));
    vi.stubGlobal('fetch', fetch);
    await act(async () => root.render(<Form />));
    expect(fetch).not.toHaveBeenCalled();
    expect(container.textContent).not.toContain('key configured');
    await act(async () => root.render(<Form currentGCS />));
    expect(fetch.mock.calls[0][0]).toContain('/api/ai/credentials');
    expect(container.textContent).toContain('OpenAI key configured (from the environment)');
    expect(container.textContent).toContain('Anthropic key missing');
    expect(container.textContent).toContain('local-example · temperature 0');
    expect(container.textContent).toContain('Key editing is unavailable from this connection.');
    expect(container.querySelector('input[type="password"]')).toBeNull();
});

it('saves and clears a credential separately from plugin args', async () => {
    const changed = vi.fn();
    const fetch = vi.fn(async (url, options) => {
        if (url.endsWith('/api/chat/status')) return respond({});
        return respond(credentialStatus(true, options.method === 'PUT' ? 'saved' : 'environment'));
    });
    vi.stubGlobal('fetch', fetch);
    await act(async () => root.render(<Form currentGCS onChange={changed} />));
    const secret = input('OpenAI API key');
    expect(secret.type).toBe('password');
    expect(secret.value).toBe('');
    await act(async () => typeValue(secret, 'fake-openai-test-key'));
    await act(async () => button('Save OpenAI key').click());

    expect(fetch).toHaveBeenLastCalledWith(expect.stringContaining('/api/ai/credentials/openai'), expect.objectContaining({
        method: 'PUT', body: '{"key":"fake-openai-test-key"}',
    }));
    expect(secret.value).toBe('');
    expect(button('Replace OpenAI key')).toBeTruthy();
    expect(container.textContent).toContain('OpenAI key configured (saved on this GCS)');

    await act(async () => button('Clear saved OpenAI key').click());
    expect(fetch).toHaveBeenLastCalledWith(expect.stringContaining('/api/ai/credentials/openai'), expect.objectContaining({ method: 'DELETE' }));
    expect(button('Clear saved OpenAI key')).toBeNull();
    expect(container.textContent).toContain('OpenAI key configured (from the environment)');
    expect(changed).not.toHaveBeenCalled();
});

it('clears a draft key when leaving the current GCS and keeps server errors generic', async () => {
    const fetch = vi.fn(async (url, options) => {
        if (url.endsWith('/api/chat/status')) return respond({});
        if (options.method === 'PUT') return { ok: false, json: async () => ({ detail: 'fake-anthropic-test-key' }) };
        return respond(credentialStatus(true, 'missing'));
    });
    vi.stubGlobal('fetch', fetch);
    await act(async () => root.render(<Form currentGCS />));
    await act(async () => typeValue(input('Anthropic API key'), 'fake-anthropic-test-key'));
    await act(async () => button('Save Anthropic key').click());
    expect(container.querySelector('[role="alert"]').textContent).toBe('Could not save the Anthropic key.');

    await act(async () => root.render(<Form />));
    expect(input('Anthropic API key')).toBeUndefined();
    await act(async () => root.render(<Form currentGCS />));
    expect(input('Anthropic API key').value).toBe('');
});

it('exposes all video inference limits and stores MP4 sizes as integer bytes', async () => {
    const mib = 1024 * 1024;
    const initial = { ai: { inferred_mission: { max_video_bytes: 3 * mib + 1, future_limit: 42 } } };
    const changed = vi.fn();
    await act(async () => root.render(<Form initial={initial} onChange={changed} />));

    const maxSize = input('Maximum MP4 size (MiB)');
    expect(Number(maxSize.value)).toBe(3 + 1 / mib);
    expect(input('Longest sampled frame side (px)').placeholder).toBe('768');
    expect(input('Maximum MP4 duration (seconds)').placeholder).toBe('3600');
    expect(input('Maximum active runs').placeholder).toBe('4');
    await act(async () => {
        maxSize.value = '4.00000095367431640625';
        maxSize.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true }));
    });
    expect(changed).toHaveBeenLastCalledWith({
        value: { ai: { inferred_mission: { max_video_bytes: 4 * mib + 1, future_limit: 42 } } },
        errors: {},
    });
});
