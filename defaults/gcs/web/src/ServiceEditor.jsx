import React from 'react';
import { Button } from 'primereact/button';
import { Checkbox } from 'primereact/checkbox';
import { Dropdown } from 'primereact/dropdown';
import { InputText } from 'primereact/inputtext';
import ArgsEditor from './ArgsEditor.jsx';
import { SimEntryEditor } from './SimEditors.jsx';
import { AUTH_LEVELS, SERVICE_TYPES, setPlugin } from './configForm.js';
import { SIM_LISTS, prefillPlugin } from './simForm.js';

const TYPE_OPTIONS = SERVICE_TYPES.map((t) => ({ label: t, value: t }));
// The Dropdown takes an option valued '' for one with no value and hands
// back the whole option, so the daemon's default ('') is picked as this
const DEFAULT_AUTH = 'default';
const AUTH_OPTIONS = AUTH_LEVELS.map((a) => ({ label: a || 'default', value: a || DEFAULT_AUTH }));
// A plugin is picked by package and name together
const SEP = '\u0000';

// One service in the Configure dialog: a collapsible card with its name,
// type, auth, plugin (picked from the daemon's installed packages, or typed
// in) and the plugin's args. `errors` maps a field to its message, with
// `args` mapping an arg to its message and `sim` its simulator entry's
// fields to theirs. hasSimulator is whether the config has a [simulator],
// which a simulated service needs; simPackage is its package, which a newly
// simulated service's plugin is taken from.
function ServiceEditor({ svc, packages, hasSimulator, simPackage, errors = {}, expanded, onToggle, onChange, onRemove, onDuplicate, disabled }) {
    const pluginOptions = (packages ?? []).flatMap((p) => p.plugins.map((pl) => ({
        label: `${pl.name} — ${p.uri}`,
        value: `${p.uri}${SEP}${pl.name}`,
    })));
    const picked = `${svc.package}${SEP}${svc.plugin}`;
    const installed = pluginOptions.some((o) => o.value === picked);

    // A typed plugin or package re-reads the args against that plugin's spec
    const set = (field, value) => {
        if (field === 'plugin') return onChange(setPlugin(svc, packages, svc.package, value));
        if (field === 'package') return onChange(setPlugin(svc, packages, value, svc.plugin));
        return onChange({ ...svc, [field]: value });
    };

    const text = (label, field, props = {}) => (
        <div className="flex flex-column gap-1 flex-1">
            <label className="text-sm">{label}</label>
            <InputText value={svc[field]} onChange={(e) => set(field, e.target.value)} disabled={disabled} invalid={!!errors[field]} {...props} />
            {errors[field] && <small className="p-error">{errors[field]}</small>}
        </div>
    );

    return (
        <div id={`service-${svc.key}`} className="border-round" style={{ border: '1px solid var(--surface-border)' }}>
            <div className="flex align-items-center gap-2 p-2 cursor-pointer" onClick={onToggle}>
                <i className={expanded ? 'pi pi-chevron-down' : 'pi pi-chevron-right'} />
                <span className="font-semibold">{svc.name.trim() || <span className="text-color-secondary">(unnamed)</span>}</span>
                <span className="text-sm text-color-secondary">{svc.type}</span>
                {svc.simulate && <span className="text-sm" style={{ color: 'var(--primary-color)' }}>simulated</span>}
                <span className="text-sm text-color-secondary flex-1 white-space-nowrap overflow-hidden text-overflow-ellipsis">{svc.plugin}</span>
                {Object.keys(errors).length > 0 && <i className="pi pi-exclamation-circle" style={{ color: 'var(--red-500)' }} title="Has errors" />}
                <span className="flex gap-1" onClick={(e) => e.stopPropagation()}>
                    <Button icon="pi pi-clone" size="small" text rounded tooltip="Duplicate" tooltipOptions={{ position: 'top' }} aria-label="Duplicate service" onClick={onDuplicate} disabled={disabled} />
                    <Button icon="pi pi-trash" size="small" text rounded severity="danger" tooltip="Remove" tooltipOptions={{ position: 'top' }} aria-label="Remove service" onClick={onRemove} disabled={disabled} />
                </span>
            </div>
            {expanded && (
                <div className="flex flex-column gap-3 p-3 pt-1">
                    <div className="flex gap-2 align-items-start">
                        {text('Name', 'name')}
                        <div className="flex flex-column gap-1">
                            <label className="text-sm">Type</label>
                            <Dropdown value={svc.type} options={TYPE_OPTIONS} onChange={(e) => onChange({ ...svc, type: e.value })} disabled={disabled} invalid={!!errors.type} style={{ width: '9rem' }} />
                            {errors.type && <small className="p-error">{errors.type}</small>}
                        </div>
                        <div className="flex flex-column gap-1">
                            <label className="text-sm">Auth</label>
                            <Dropdown value={svc.auth || DEFAULT_AUTH} options={AUTH_OPTIONS} onChange={(e) => onChange({ ...svc, auth: e.value === DEFAULT_AUTH ? '' : e.value })} disabled={disabled} style={{ width: '9rem' }} />
                        </div>
                    </div>
                    <div className="flex flex-column gap-1">
                        <div className="flex align-items-center gap-2">
                            {/* Unchecking stays possible without a simulator, so a config that needs one can be fixed */}
                            <Checkbox
                                inputId={`${svc.key}-simulate`}
                                checked={svc.simulate}
                                onChange={(e) => onChange(e.checked ? prefillPlugin({ ...svc, simulate: true }, simPackage, packages) : { ...svc, simulate: false })}
                                disabled={disabled || (!hasSimulator && !svc.simulate)}
                            />
                            <label htmlFor={`${svc.key}-simulate`} className="text-sm">Simulated: this daemon's simulator plays this service instead of its plugin</label>
                        </div>
                        {errors.simulate ? (
                            <small className="p-error">{errors.simulate}</small>
                        ) : !hasSimulator && <small className="text-color-secondary">Needs a simulator (Simulator tab).</small>}
                    </div>
                    <div className="flex flex-column gap-1">
                        <label className="text-sm">Plugin</label>
                        <Dropdown
                            value={installed ? picked : null}
                            options={pluginOptions}
                            placeholder={pluginOptions.length ? 'Pick an installed plugin' : 'No packages installed on this daemon'}
                            onChange={(e) => {
                                const [uri, name] = e.value.split(SEP);
                                onChange(setPlugin(svc, packages, uri, name));
                            }}
                            filter
                            disabled={disabled || !pluginOptions.length}
                        />
                    </div>
                    <div className="flex gap-2">
                        {text('Plugin name', 'plugin')}
                        {text('Package', 'package', { placeholder: 'repo[//subdir][@version]' })}
                    </div>
                    {!svc.simulate && (
                        <>
                            {text('Isolation', 'isolation', { placeholder: 'empty for a process; "sandbox", or a container image' })}
                            <div className="flex flex-column gap-1">
                                <span className="font-semibold text-sm">Plugin args</span>
                                <ArgsEditor args={svc.args} errors={errors.args ?? {}} onChange={(args) => onChange({ ...svc, args })} disabled={disabled} />
                            </div>
                        </>
                    )}
                    {svc.simulate && SIM_LISTS[svc.type] && (
                        <SimEntryEditor type={svc.type} sim={svc.sim} errors={errors.sim ?? {}} onChange={(sim) => onChange({ ...svc, sim })} disabled={disabled} />
                    )}
                </div>
            )}
        </div>
    );
}

export default ServiceEditor;
