import React from 'react';
import { Button } from 'primereact/button';
import { InputNumber } from 'primereact/inputnumber';
import { InputText } from 'primereact/inputtext';
import KeyValueEditor from './KeyValueEditor.jsx';
import { DEFAULT_INTERFACE, newWaypoint } from './simForm.js';

const number = (value, onChange, { placeholder, disabled, invalid, width = '9rem' }) => (
    <InputNumber
        value={value}
        onValueChange={(e) => onChange(e.value ?? null)}
        mode="decimal"
        locale="en-US"
        useGrouping={false}
        maxFractionDigits={8}
        placeholder={placeholder}
        disabled={disabled}
        invalid={invalid}
        inputStyle={{ width }}
    />
);

// Lat, lon and alt inputs for a point ({lat, lon, alt}, null where unset).
// raw is a point the form can't show, which is kept as it is.
export function PointInput({ label, point, raw, onChange, error, disabled }) {
    const set = (key) => (v) => onChange({ ...point, [key]: v });
    return (
        <div className="flex flex-column gap-1">
            <label className="text-sm">{label}</label>
            {raw !== undefined ? (
                <span className="text-sm text-color-secondary" style={{ fontFamily: 'monospace' }}>{JSON.stringify(raw)} (edit in Raw TOML)</span>
            ) : (
                <div className="flex gap-2 align-items-center">
                    {number(point.lat, set('lat'), { placeholder: 'latitude', disabled, invalid: !!error })}
                    {number(point.lon, set('lon'), { placeholder: 'longitude', disabled, invalid: !!error })}
                    {number(point.alt, set('alt'), { placeholder: 'altitude (0)', disabled })}
                </div>
            )}
            {error && <small className="p-error">{error}</small>}
        </div>
    );
}

// A simulated service's simulator entry, on its service card. errors has
// origin and args (by row).
export function SimEntryEditor({ type, sim, errors = {}, onChange, disabled }) {
    return (
        <div className="flex flex-column gap-3 p-2 border-round" style={{ background: 'var(--surface-ground)' }}>
            <small className="text-color-secondary">The simulator plays this service from these settings (its {type === 'vehicle' ? 'vehicles' : 'engines'} entry of this name); the plugin's args, isolation and environment are ignored.</small>
            <div className="flex flex-column gap-1">
                <label className="text-sm">Interface</label>
                <InputText value={sim.interface} placeholder={DEFAULT_INTERFACE[type]} onChange={(e) => onChange({ ...sim, interface: e.target.value })} disabled={disabled} style={{ width: '14rem' }} />
            </div>
            {type === 'vehicle' && (
                <PointInput label="Origin" point={sim.origin} raw={sim.originRaw} onChange={(origin) => onChange({ ...sim, origin })} error={errors.origin} disabled={disabled} />
            )}
            <div className="flex flex-column gap-1">
                <span className="font-semibold text-sm">Simulator args</span>
                <KeyValueEditor rows={sim.argRows} onChange={(argRows) => onChange({ ...sim, argRows })} disabled={disabled} />
            </div>
        </div>
    );
}

function WaypointsEditor({ waypoints, errors = {}, onChange, disabled }) {
    const update = (key, patch) => onChange(waypoints.map((w) => (w.key === key ? { ...w, ...patch } : w)));
    return (
        <div className="flex flex-column gap-2">
            {waypoints.length === 0 && <div className="text-color-secondary text-sm">No waypoints: the actor stays at its origin.</div>}
            {waypoints.map((w, i) => (
                <div key={w.key} className="flex flex-column gap-1">
                    <div className="flex gap-2 align-items-center flex-wrap">
                        <span className="text-sm text-color-secondary" style={{ width: '1.5rem' }}>{i + 1}</span>
                        {number(w.lat, (v) => update(w.key, { lat: v }), { placeholder: 'latitude', disabled, invalid: !!errors[w.key] })}
                        {number(w.lon, (v) => update(w.key, { lon: v }), { placeholder: 'longitude', disabled, invalid: !!errors[w.key] })}
                        {number(w.alt, (v) => update(w.key, { alt: v }), { placeholder: 'altitude (0)', disabled })}
                        {number(w.speed, (v) => update(w.key, { speed: v }), { placeholder: 'speed m/s', disabled, width: '7rem' })}
                        {number(w.delay, (v) => update(w.key, { delay: v }), { placeholder: 'delay s', disabled, width: '7rem' })}
                        <Button icon="pi pi-times" text rounded size="small" severity="secondary" aria-label="Remove waypoint" onClick={() => onChange(waypoints.filter((x) => x.key !== w.key))} disabled={disabled} />
                    </div>
                    {errors[w.key] && <small className="p-error">{errors[w.key]}</small>}
                </div>
            ))}
            <div>
                <Button label="Add waypoint" icon="pi pi-plus" text size="small" onClick={() => onChange([...waypoints, newWaypoint()])} disabled={disabled} />
            </div>
        </div>
    );
}

// One actor in the Simulator tab. errors has tag, origin, waypoints (by
// waypoint) and args (by row).
export function ActorEditor({ actor, errors = {}, onChange, onRemove, disabled }) {
    const set = (field) => (value) => onChange({ ...actor, [field]: value });
    return (
        <div className="flex flex-column gap-3 p-3 border-round" style={{ border: '1px solid var(--surface-border)' }}>
            <div className="flex gap-2 align-items-start">
                <div className="flex flex-column gap-1">
                    <label className="text-sm">Tag</label>
                    <InputText value={actor.tag} placeholder="person" onChange={(e) => set('tag')(e.target.value)} disabled={disabled} invalid={!!errors.tag} />
                    {errors.tag && <small className="p-error">{errors.tag}</small>}
                </div>
                <div className="flex flex-column gap-1 flex-1">
                    <label className="text-sm">Name</label>
                    <InputText value={actor.name} placeholder="optional; tag and number if empty" onChange={(e) => set('name')(e.target.value)} disabled={disabled} />
                </div>
                <Button icon="pi pi-trash" text rounded size="small" severity="danger" aria-label="Remove actor" tooltip="Remove" tooltipOptions={{ position: 'top' }} onClick={onRemove} disabled={disabled} className="mt-4" />
            </div>
            <PointInput label="Origin" point={actor.origin} raw={actor.originRaw} onChange={set('origin')} error={errors.origin} disabled={disabled} />
            <div className="flex flex-column gap-1">
                <span className="font-semibold text-sm">Waypoints</span>
                {actor.waypointsRaw !== undefined ? (
                    <span className="text-sm text-color-secondary" style={{ fontFamily: 'monospace' }}>{JSON.stringify(actor.waypointsRaw)} (edit in Raw TOML)</span>
                ) : (
                    <WaypointsEditor waypoints={actor.waypoints} errors={errors.waypoints} onChange={set('waypoints')} disabled={disabled} />
                )}
            </div>
            <div className="flex flex-column gap-1">
                <span className="font-semibold text-sm">Args</span>
                <small className="text-color-secondary">e.g. length, width, height (m), waypoint_cycle</small>
                <KeyValueEditor rows={actor.argRows} onChange={set('argRows')} disabled={disabled} />
            </div>
        </div>
    );
}
