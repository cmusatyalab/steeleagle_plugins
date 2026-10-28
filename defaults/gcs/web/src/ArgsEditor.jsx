import React from 'react';
import { Button } from 'primereact/button';
import { InputNumber } from 'primereact/inputnumber';
import { InputSwitch } from 'primereact/inputswitch';
import { InputText } from 'primereact/inputtext';
import { InputTextarea } from 'primereact/inputtextarea';
import { Tag } from 'primereact/tag';
import KeyValueEditor from './KeyValueEditor.jsx';
import { JSON_TYPES, defaultValue } from './configForm.js';

// A service's plugin args: first the args its plugin TOML declares, each
// with its description and an input of its type, prefilled with its default;
// then free-form rows for any others. `args` is configForm's argsState;
// `errors` maps an arg name or row id to its message.
function ArgsEditor({ args, errors = {}, onChange, disabled }) {
    const update = (name, patch) => onChange({
        ...args,
        declared: args.declared.map((d) => (d.name === name ? { ...d, ...patch } : d)),
    });
    // Back to the default: not set in the config, so it isn't written
    const reset = (d) => {
        const value = defaultValue(d);
        update(d.name, { present: false, value, ...(JSON_TYPES.includes(d.type) ? { text: JSON.stringify(value) } : {}) });
    };

    const input = (d) => {
        switch (d.type) {
            case 'boolean':
                return <InputSwitch checked={!!d.value} onChange={(e) => update(d.name, { value: e.value })} disabled={disabled} />;
            case 'number':
                return (
                    <InputNumber
                        value={d.value}
                        onValueChange={(e) => update(d.name, { value: e.value ?? null })}
                        useGrouping={false}
                        maxFractionDigits={10}
                        disabled={disabled}
                        invalid={!!errors[d.name]}
                    />
                );
            case 'array':
            case 'object':
                return (
                    <InputTextarea
                        value={d.text}
                        onChange={(e) => update(d.name, { text: e.target.value })}
                        rows={2}
                        autoResize
                        disabled={disabled}
                        invalid={!!errors[d.name]}
                        style={{ fontFamily: 'monospace', width: '100%' }}
                    />
                );
            default:
                return (
                    <InputText
                        value={d.value ?? ''}
                        onChange={(e) => update(d.name, { value: e.target.value })}
                        disabled={disabled}
                        invalid={!!errors[d.name]}
                        className="w-full"
                    />
                );
        }
    };

    const rowErrors = Object.entries(errors).filter(([k]) => args.rows.some((r) => r.id === k));

    return (
        <div className="flex flex-column gap-3">
            {args.declared.map((d) => (
                <div key={d.name} className="flex flex-column gap-1">
                    <div className="flex align-items-center gap-2">
                        <span className="text-sm font-semibold" style={{ fontFamily: 'monospace' }}>{d.name}</span>
                        <Tag value={d.type} severity="secondary" />
                        {!d.present && <span className="text-xs text-color-secondary">default</span>}
                        {d.present && (
                            <Button label="Reset to default" link size="small" className="p-0 text-xs" onClick={() => reset(d)} disabled={disabled} />
                        )}
                    </div>
                    {d.description && <small className="text-color-secondary">{d.description}</small>}
                    <div>{input(d)}</div>
                    {errors[d.name] && <small className="p-error">{errors[d.name]}</small>}
                </div>
            ))}
            <div className="flex flex-column gap-1">
                {args.declared.length > 0 && <span className="text-sm text-color-secondary">Other args</span>}
                <KeyValueEditor rows={args.rows} onChange={(rows) => onChange({ ...args, rows })} disabled={disabled} />
                {rowErrors.map(([id, msg]) => <small key={id} className="p-error">{msg}</small>)}
            </div>
        </div>
    );
}

export default ArgsEditor;
