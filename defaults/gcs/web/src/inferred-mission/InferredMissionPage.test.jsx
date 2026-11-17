// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import InferredMissionPage from './InferredMissionPage.jsx';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const settings = { section_seconds: 5, section_mode: 'non_overlapping', history_mode: 'recency' };
const config = { configured: true, media_available: true, settings, models: {} };
let root;
let container;
let sessions;
let events;

class RunEvents {
    constructor() { events = this; }
    addEventListener(name, receive) { if (name === 'update') this.receive = receive; }
    removeEventListener() { this.receive = null; }
    close() {}
    update(session) { this.receive({ data: JSON.stringify(session) }); }
}

const button = label => [...document.querySelectorAll('button')].find(node => node.textContent.trim() === label);
const timeline = () => [...container.querySelectorAll('.im-section')];
const selected = () => container.querySelector('.im-section[aria-current=true] strong')?.textContent;
const response = data => ({ ok: true, json: async () => data });

function run(overrides = {}) {
    return {
        id: 'run-1', revision: 1, status: 'paused', created_at: 100, updated_at: 115,
        vehicle_id: 'alpha', source_type: 'uploaded_video', settings: { section_mode: 'sliding' }, sections: [],
        ...overrides,
    };
}

async function render(gcsProps = {}) {
    await act(async () => root.render(<InferredMissionPage activeTab={false} {...gcsProps} />));
}

beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    sessions = [];
    events = null;
    vi.stubGlobal('EventSource', RunEvents);
    vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
        const { pathname, search } = new URL(url);
        if (pathname.endsWith('/status')) return response(config);
        if (pathname.endsWith('/sessions')) {
            if (options.method === 'POST') {
                const created = run({ id: 'new-run' });
                sessions.unshift(created);
                return response(created);
            }
            expect(search).toBe('?include_archived=true');
            return response(sessions);
        }
        if (options.method === 'PATCH') {
            const i = sessions.findIndex(value => pathname.endsWith(`/sessions/${value.id}`));
            const changes = JSON.parse(options.body);
            const { archived, ...meta } = changes;
            sessions[i] = { ...sessions[i], ...meta, revision: sessions[i].revision + 1 };
            if (archived != null) sessions[i].archived_at = archived ? 140 : undefined;
            const { sections: _sections, ...metadata } = sessions[i];
            return response(metadata);
        }
        const section = pathname.match(/\/sections\/(\d+)$/);
        if (section) return response(sessions[0].sections.find(row => row.index === Number(section[1])));
        return response(sessions.find(item => pathname.endsWith(`/sessions/${item.id}`)));
    }));
});

afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
});

it('updates MP4 preview count and sends the chosen sliding mode', async () => {
    class Upload {
        upload = {};
        status = 201;
        responseText = JSON.stringify({ id: 'asset', name: 'flight.mp4', duration: 6.25 });
        open() {}
        send() { this.onload(); }
    }
    vi.stubGlobal('XMLHttpRequest', Upload);
    await render();
    await act(async () => button('Upload MP4').click());
    const uploadInput = container.querySelector('input[type=file]');
    Object.defineProperty(uploadInput, 'files', { value: [new File(['data'], 'flight.mp4')] });
    await act(async () => uploadInput.dispatchEvent(new Event('change', { bubbles: true })));
    expect(container.querySelector('.im-upload-info').textContent).toContain('2 sections');

    const sectionMode = [...container.querySelectorAll('select')].find(select => select.querySelector('[value=sliding]'));
    expect(sectionMode.options[2].text).toBe('Sliding (1-second step, up to 5 in parallel)');
    await act(async () => {
        sectionMode.value = 'sliding';
        sectionMode.dispatchEvent(new Event('change', { bubbles: true }));
    });
    expect(container.querySelector('.im-upload-info').textContent).toContain('3 sections');
    await act(async () => button('Start analysis').click());
    const post = fetch.mock.calls.find(([, options]) => options?.method === 'POST');
    expect(JSON.parse(post[1].body)).toEqual({ source_type: 'uploaded_video', section_mode: 'sliding', video_asset_id: 'asset' });
});

it('keeps manual selection as parallel results arrive and restores follow latest', async () => {
    const first = { index: 1, start: 0, end: 5, status: 'generating', inference_text: 'First', inference_elapsed: 4 };
    const third = { index: 3, start: 2, end: 7, status: 'inferring' };
    sessions = [run({ status: 'analyzing', sections: [third, first] })];
    await render();
    expect(timeline().map(node => node.querySelector('strong').textContent)).toEqual(['#001', '#003']);
    expect(selected()).toBe('#001');
    expect(button('Archive').disabled).toBe(true);
    await act(async () => timeline()[0].click());
    sessions[0] = { ...sessions[0], revision: 2, sections: [
        { ...third, status: 'completed', compile_status: 'compiled', inference_text: 'Third', inference_elapsed: 2, elapsed: 6, dsl: 'compiled draft' },
        first,
    ] };
    await act(async () => events.update(sessions[0]));
    expect(selected()).toBe('#001');
    expect(timeline()[1].textContent).toContain('Vision 2.00 sDSL ready 6.00 s');
    await act(async () => button('Follow latest').click());
    expect(selected()).toBe('#003');
    expect(container.querySelector('[aria-label="Section timing"]').textContent).toContain('Vision ready2.00 sDSL ready6.00 sFrame prep—');
    expect([...container.querySelectorAll('[aria-label="Run timing"] dt')].map(node => node.textContent)).toEqual([
        'Run elapsed', 'Avg. vision inference', 'Avg. section to DSL ready',
    ]);
    expect([...container.querySelectorAll('[aria-label="Run timing"] dd')].slice(1).map(node => node.textContent)).toEqual(['3.00 s', '6.00 s']);
});

it('renames, archives, and restores a paused run without changing its timing', async () => {
    const first = { index: 1, start: 0, end: 5, status: 'completed', compile_status: 'compiled', elapsed: 1, dsl: 'reviewed draft' };
    sessions = [run({ sections: [first] })];
    await render();
    expect(container.querySelector('[aria-label="Run timing"] dd').textContent).toBe('15.00 s');
    await act(async () => button('Rename').click());
    const name = document.querySelector('.im-rename input');
    await act(async () => {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(name, '  录像 · Recency  ');
        name.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await act(async () => button('Save').click());
    expect(sessions[0].display_name).toBe('录像 · Recency');
    expect(selected()).toBe('#001');
    expect(container.querySelector('option[value="run-1"]').textContent).toContain('录像 · Recency');
    expect(container.querySelector('[aria-label="Run timing"] dd').textContent).toBe('15.00 s');

    await act(async () => button('Archive').click());
    expect(container.querySelector('option[value="run-1"]')).toBeNull();
    await act(async () => button('Archived runs').click());
    expect(button('Resume')).toBeUndefined();
    await act(async () => button('Restore').click());
    await act(async () => button('Analysis runs').click());
    expect(button('Resume')).toBeDefined();
    expect(container.querySelector('[aria-label="Run timing"] dd').textContent).toBe('15.00 s');
});
