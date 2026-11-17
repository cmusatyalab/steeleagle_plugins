// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import FieldInput from './FieldInput.jsx';
import { missionRequestBody } from './missionGraph.js';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const cameraField = { name: 'Camera', go_type: 'params.Camera', enum_type: 'Camera', required: true };
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
});

it('does not expose a selectable row when no camera is available', async () => {
    const onChange = vi.fn();
    await act(async () => root.render(<FieldInput field={cameraField} onChange={onChange} />));
    expect(container.textContent).toContain('No camera available');
    const dropdown = container.querySelector('.p-dropdown');
    expect(dropdown).not.toBeNull();
    await act(async () => dropdown.click());
    expect(document.querySelector('[role="option"]')).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
});

it('selects a real camera through PrimeReact and sends the SDK identifier in the compile graph', async () => {
    const node = { id: 'node-1', data: { instance_id: 'track', type_name: 'Track', params: {} } };
    const onChange = value => { node.data.params.Camera = value; };
    await act(async () => root.render(<FieldInput
        field={cameraField}
        value={{ label: '(no values)', value: null }}
        enums={{ Camera: { values: ['Camera_front'] } }}
        onChange={onChange}
    />));
    await act(async () => container.querySelector('.p-dropdown').click());
    const option = document.querySelector('[role="option"]');
    expect(option).not.toBeNull();
    expect(option.textContent).toBe('front');
    await act(async () => option.click());
    const payload = missionRequestBody([node], [], [], 'node-1');
    expect(payload.nodes[0].params).toEqual({ Camera: 'Camera_front' });
});
