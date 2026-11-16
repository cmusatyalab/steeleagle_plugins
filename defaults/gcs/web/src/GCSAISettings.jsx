import { useId } from 'react';
import { InputNumber } from 'primereact/inputnumber';
import { InputText } from 'primereact/inputtext';
import GCSCredentials from './GCSCredentials.jsx';

const OPENAI_MODEL = 'gpt-6.1-sol';
const ANTHROPIC_MODEL = 'claude-sonnet-4-20250514';
const MEBIBYTE = 1024 * 1024;

const isObject = (value) => value != null && typeof value === 'object' && !Array.isArray(value);

function parseObject(text) {
    try {
        const value = JSON.parse(text);
        return isObject(value) ? value : null;
    } catch {
        return null;
    }
}

function withField(object, key, value) {
    const updated = { ...object };
    if (value == null || value === '') delete updated[key];
    else updated[key] = value;
    return updated;
}

// Keep the manifest's object arg as JSON in the parent editor. Edits update
// only the selected keys so settings from a newer plugin are retained.
export default function GCSAISettings({ text, onChange, disabled, currentGCS }) {
    const id = useId();
    const value = parseObject(text);
    if (!value) return <small className="p-error">AI settings must be a JSON object. Reset to default or edit in Raw TOML.</small>;

    const inference = value.inferred_mission ?? {};
    const provider = value.chat_provider || 'openai';
    const update = (key, v) => onChange(JSON.stringify(withField(value, key, v)));
    const updateInference = (key, v) => {
        const next = withField(inference, key, v);
        update('inferred_mission', Object.keys(next).length ? next : undefined);
    };

    const model = (label, key, defaultID, example) => (
        <label className="flex flex-column gap-1 flex-1">
            <span className="text-sm">{label}</span>
            <InputText value={value[key] ?? ''} placeholder={defaultID} list={`${id}-${key}`}
                onChange={e => update(key, e.target.value)} disabled={disabled} />
            <datalist id={`${id}-${key}`}>
                <option value={defaultID} />
                {example && <option value={example} />}
            </datalist>
        </label>
    );

    const number = (label, key, data, set, {
        min, max, placeholder, step = 1, maxFractionDigits = step === 1 ? 0 : 2, scale = 1,
    }) => (
        <div className="flex flex-column gap-1 flex-1">
            <label className="text-sm" htmlFor={`${id}-${key}`}>{label}</label>
            <InputNumber inputId={`${id}-${key}`} value={data[key] == null ? null : data[key] / scale}
                onValueChange={({ value }) => set(key, value == null || scale === 1 ? value : Math.round(value * scale))}
                min={min} max={max} placeholder={placeholder} step={step} useGrouping={false}
                maxFractionDigits={maxFractionDigits} disabled={disabled} />
        </div>
    );

    const reasoning = (label, key) => (
        <label className="flex flex-column gap-1 flex-1">
            <span className="text-sm">{label}</span>
            <InputText value={value[key] ?? ''} placeholder="Model default" list={`${id}-effort`}
                onChange={e => update(key, e.target.value)} disabled={disabled} />
        </label>
    );

    const temp = { min: 0, max: 1, placeholder: 'Provider default', step: 0.1 };

    return (
        <div className="flex flex-column gap-3 p-3 border-1 surface-border border-round">
            {currentGCS ? <GCSCredentials disabled={disabled} /> : <small className="text-color-secondary">
                Open the selected GCS directly over localhost or an SSH tunnel to manage its API keys.
            </small>}
            <label className="flex flex-column gap-1">
                <span className="text-sm">Chat provider</span>
                <select className="p-inputtext p-component" value={provider} onChange={e => update('chat_provider', e.target.value)} disabled={disabled}>
                    {!['openai', 'anthropic'].includes(provider) && <option value={provider}>{provider}</option>}
                    <option value="openai">OpenAI</option>
                    <option value="anthropic">Anthropic</option>
                </select>
            </label>
            <div className="flex flex-wrap gap-2">
                {model('Chat / DSL model', 'chat_model', provider === 'anthropic' ? ANTHROPIC_MODEL : OPENAI_MODEL, provider === 'openai' ? 'gpt-4.1-mini' : null)}
                {model('Vision model', 'vision_model', OPENAI_MODEL, 'gpt-4.1-mini')}
            </div>
            <div className="flex flex-wrap gap-2">
                {number('Chat / DSL temperature', 'chat_temperature', value, update, temp)}
                {number('Vision temperature', 'vision_temperature', value, update, temp)}
            </div>
            <small className="text-color-secondary">
                Set temperature from 0 to 1 only for models that support it. Leave it blank for reasoning models such as {OPENAI_MODEL}.
            </small>
            <div className="flex flex-wrap gap-2">
                {provider === 'openai' && reasoning('Chat / DSL reasoning effort', 'chat_reasoning_effort')}
                {reasoning('Vision reasoning effort', 'vision_reasoning_effort')}
                <datalist id={`${id}-effort`}>
                    {['low', 'medium', 'high', 'xhigh', 'max'].map(effort => <option key={effort} value={effort} />)}
                </datalist>
            </div>
            <small className="text-color-secondary">Use Apply to save model and inference settings.</small>
            {isObject(inference) ? <details>
                <summary className="cursor-pointer text-sm font-semibold">Video inference settings</summary>
                <div className="flex flex-column gap-2 mt-2">
                    <span className="text-sm font-semibold">Windows and frame sampling</span>
                    <label className="flex flex-column gap-1">
                        <span className="text-sm">Section window</span>
                        <select className="p-inputtext p-component" value={inference.section_mode ?? ''}
                            onChange={e => updateInference('section_mode', e.target.value)} disabled={disabled}>
                            <option value="">Default (Non-overlapping)</option>
                            <option value="non_overlapping">Non-overlapping</option>
                            <option value="sliding">Sliding (1-second step)</option>
                        </select>
                    </label>
                    <div className="flex flex-wrap gap-2">
                        {number('Section seconds', 'section_seconds', inference, updateInference, { min: 1, max: 60, placeholder: '15', step: 0.5 })}
                        {number('Frames per second', 'frame_fps', inference, updateInference, { min: 0.01, max: 60, placeholder: '1', step: 0.1 })}
                    </div>
                    <small className="text-color-secondary">Set the window length and sample rate to use 1–60 frames per window.</small>
                    <span className="text-sm font-semibold mt-2">History</span>
                    <div className="flex flex-wrap gap-2">
                        <label className="flex flex-column gap-1 flex-1">
                            <span className="text-sm">History mode</span>
                            <select className="p-inputtext p-component" value={inference.history_mode ?? ''}
                                onChange={e => updateInference('history_mode', e.target.value)} disabled={disabled}>
                                <option value="">Default (recency)</option>
                                <option value="recency">Recency</option>
                                <option value="uniform">Uniform</option>
                                <option value="no_memory">No memory</option>
                            </select>
                        </label>
                        {number('History sections', 'history_sections', inference, updateInference, { min: 0, max: 30, placeholder: '10' })}
                        {number('History half-life (sections)', 'half_life_sections', inference, updateInference,
                            { min: 0.01, placeholder: '3', step: 0.5 })}
                    </div>
                    <details>
                        <summary className="cursor-pointer text-sm font-semibold">Advanced inference settings</summary>
                        <div className="flex flex-column gap-2 mt-2">
                            <div className="flex flex-wrap gap-2">
                                {number('Longest sampled frame side (px)', 'max_frame_side', inference, updateInference, { min: 32, max: 2048, placeholder: '768' })}
                                {number('Maximum active runs', 'max_active_runs', inference, updateInference, { min: 1, max: 8, placeholder: '4' })}
                            </div>
                            <div className="flex flex-wrap gap-2">
                                {number('Vision timeout (seconds)', 'request_timeout', inference, updateInference, { min: 1, max: 600, placeholder: '60' })}
                                {number('DSL timeout (seconds)', 'generation_timeout', inference, updateInference, { min: 1, max: 600, placeholder: '120' })}
                            </div>
                            <div className="flex flex-wrap gap-2">
                                {number('Maximum MP4 size (MiB)', 'max_video_bytes', inference, updateInference, {
                                    min: 1 / MEBIBYTE, max: 16384, placeholder: '1024', scale: MEBIBYTE, maxFractionDigits: 20,
                                })}
                                {number('Maximum MP4 duration (seconds)', 'max_video_seconds', inference, updateInference, {
                                    min: 1, max: 86400, placeholder: '3600', maxFractionDigits: 2,
                                })}
                            </div>
                            <small className="text-color-secondary">The maximum MP4 duration must be at least one window long.</small>
                            <label className="flex flex-column gap-1">
                                <span className="text-sm">Analysis data directory</span>
                                <InputText value={value.data_dir ?? ''} placeholder="Default GCS data directory"
                                    onChange={e => update('data_dir', e.target.value)} disabled={disabled} />
                            </label>
                        </div>
                    </details>
                </div>
            </details> : <small className="p-error" role="alert">
                Video inference settings must be an object. Edit the value in Raw TOML.
            </small>}
        </div>
    );
}
