import React, { useCallback, useEffect, useState } from 'react';
import { Dialog } from 'primereact/dialog';
import { Button } from 'primereact/button';
import { Checkbox } from 'primereact/checkbox';
import { InputNumber } from 'primereact/inputnumber';
import { InputText } from 'primereact/inputtext';
import { InputTextarea } from 'primereact/inputtextarea';
import { Message } from 'primereact/message';
import { SelectButton } from 'primereact/selectbutton';
import { TabPanel, TabView } from 'primereact/tabview';
import KeyValueEditor from './KeyValueEditor.jsx';
import ServiceEditor from './ServiceEditor.jsx';
import { applyConfig, checkConfig, convertConfig, daemonName, fetchConfig, fetchPackages } from './daemonsApi.js';
import {
    applyOutcome,
    duplicateService,
    formToState,
    guards,
    hasErrors,
    newService,
    restartWarning,
    stateToForm,
} from './configForm.js';

const MODES = [{ label: 'Form', value: 'form' }, { label: 'Raw TOML', value: 'raw' }];
const DAEMON_FIELDS = [
    { key: 'hostname', label: 'Hostname', type: 'text' },
    { key: 'port', label: 'Port', type: 'port' },
    { key: 'lcs', label: 'LCS port', type: 'port' },
    { key: 'orchestrator', label: 'Orchestrator', type: 'text', placeholder: 'host:port; empty for none' },
    { key: 'cert', label: 'Cert', type: 'text' },
];

// Edits a daemon's whole config and applies it with Configure, which
// replaces the config and restarts the daemon and every service it runs.
// The form's plugin args come from the daemon's installed packages. Apply
// checks the config first (validation, packages the daemon lacks), then asks
// for confirmation with the restart warning. The load's config_hash makes a
// concurrent change a conflict rather than an overwrite.
function ConfigureDialog({ daemon, onHide, onApplied }) {
    const [loading, setLoading] = useState(true);
    const [loadError, setLoadError] = useState(null);
    const [notice, setNotice] = useState(null);
    const [loaded, setLoaded] = useState(null); // {configHash, own, selfService, original}
    const [packages, setPackages] = useState([]);
    const [mode, setMode] = useState('form');
    const [state, setState] = useState(null);
    const [raw, setRaw] = useState('');
    const [expanded, setExpanded] = useState(() => new Set());
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState(null);
    const [errorList, setErrorList] = useState(null);
    const [conflict, setConflict] = useState(false);
    const [showErrors, setShowErrors] = useState(false);
    // The confirmation step: the check's result and the guards' confirmations
    const [confirm, setConfirm] = useState(null);
    const [ticked, setTicked] = useState(() => new Set());

    const name = daemonName(daemon);

    const load = useCallback(async () => {
        setLoading(true);
        setLoadError(null);
        setNotice(null);
        setError(null);
        setErrorList(null);
        setConflict(false);
        setShowErrors(false);
        setConfirm(null);
        const [cfg, pkgs] = await Promise.all([fetchConfig(daemon.ip), fetchPackages(daemon.ip)]);
        setLoading(false);
        if (cfg.error) {
            setLoadError(cfg.error);
            return;
        }
        const list = pkgs.packages ?? [];
        setPackages(list);
        const notices = [];
        if (pkgs.error) notices.push(`Couldn't list this daemon's packages (${pkgs.error}); plugin args show as free-form rows.`);
        const { config } = cfg;
        setRaw(config.raw_toml);
        if (config.form) {
            setState(formToState(config.form, list));
            setMode('form');
        } else {
            setState(null);
            setMode('raw');
            notices.push(`This config can't be shown as a form (${config.form_error}); edit it as raw TOML.`);
        }
        setLoaded({
            configHash: config.config_hash,
            own: config.own,
            selfService: config.self_service,
            original: config.form ? formToState(config.form, list) : null,
        });
        setNotice(notices.length ? notices.join(' ') : null);
    }, [daemon.ip]);

    useEffect(() => {
        // eslint-disable-next-line react-hooks/set-state-in-effect
        load();
    }, [load]);

    const { form, errors } = state ? stateToForm(state) : { form: null, errors: {} };
    const shown = showErrors ? errors : {};
    const guard = state && loaded?.original
        ? guards(state, loaded.original, { own: loaded.own, selfService: loaded.selfService, host: name })
        : { confirmations: [], fieldWarnings: {} };

    const switchMode = async (next) => {
        if (!next || next === mode) return;
        setError(null);
        setBusy(true);
        if (next === 'raw') {
            if (hasErrors(errors)) {
                setShowErrors(true);
                setError('Fix the errors in the form first.');
                setBusy(false);
                return;
            }
            const { result, error: err } = await convertConfig({ to: 'raw', form });
            if (err) setError(err);
            else {
                setRaw(result.raw_toml);
                setMode('raw');
            }
        } else {
            const { result, error: err } = await convertConfig({ to: 'form', raw_toml: raw });
            if (err) setError(err);
            else {
                setState(formToState(result.form, packages));
                setMode('form');
            }
        }
        setBusy(false);
    };

    const payload = () => (mode === 'form' ? { form } : { raw_toml: raw });

    // Apply, step one: check the config and show the confirmation
    const review = async () => {
        setError(null);
        setErrorList(null);
        if (mode === 'form' && hasErrors(errors)) {
            setShowErrors(true);
            return;
        }
        setBusy(true);
        const { result, error: err } = await checkConfig(daemon.ip, payload());
        setBusy(false);
        if (err) {
            setError(err);
            return;
        }
        if (result.errors.length) {
            setErrorList(result.errors);
            return;
        }
        setTicked(new Set());
        setConfirm({ ...result, confirmations: mode === 'form' ? guard.confirmations : [] });
    };

    // Apply, step two: send it
    const apply = async () => {
        setBusy(true);
        const result = await applyConfig(daemon.ip, loaded.configHash, payload());
        setBusy(false);
        const outcome = applyOutcome(result, { own: loaded.own });
        if (outcome === 'applied' || outcome === 'restarting') {
            onApplied(daemon.ip, outcome);
            return;
        }
        setConfirm(null);
        if (outcome === 'conflict') setConflict(true);
        else if (outcome === 'refused') {
            setError(result.error);
            setErrorList(result.errors);
        } else setError(result.error);
    };

    const updateService = (key, svc) => setState((s) => ({ ...s, services: s.services.map((x) => (x.key === key ? svc : x)) }));
    const toggle = (key) => setExpanded((prev) => {
        const next = new Set(prev);
        if (next.has(key)) next.delete(key);
        else next.add(key);
        return next;
    });
    const addService = (svc) => {
        setState((s) => ({ ...s, services: [...s.services, svc] }));
        setExpanded((prev) => new Set(prev).add(svc.key));
    };

    const serviceErrors = (key) => {
        const prefix = `services.${key}.`;
        return Object.fromEntries(Object.entries(shown).filter(([k]) => k.startsWith(prefix)).map(([k, v]) => [k.slice(prefix.length), v]));
    };

    const daemonField = ({ key, label, type, placeholder }) => {
        const err = shown[`daemon.${key}`];
        const warn = guard.fieldWarnings[`daemon.${key}`];
        const value = state.daemon[key];
        const set = (v) => setState((s) => ({ ...s, daemon: { ...s.daemon, [key]: v } }));
        return (
            <div key={key} className="flex flex-column gap-1" style={{ minWidth: '14rem' }}>
                <label className="text-sm">{label}</label>
                {type === 'port' ? (
                    <InputNumber value={value} onValueChange={(e) => set(e.value ?? null)} useGrouping={false} disabled={busy} invalid={!!err} />
                ) : (
                    <InputText value={value ?? ''} placeholder={placeholder} onChange={(e) => set(e.target.value === '' ? null : e.target.value)} disabled={busy} invalid={!!err} />
                )}
                {err && <small className="p-error">{err}</small>}
                {warn && <small style={{ color: 'var(--orange-500)' }}>{warn}</small>}
            </div>
        );
    };

    const footer = confirm ? (
        <div className="flex justify-content-end gap-2">
            <Button label="Back" text onClick={() => setConfirm(null)} disabled={busy} />
            <Button
                label={busy ? 'Applying...' : 'Apply and restart'}
                severity="danger"
                icon="pi pi-refresh"
                onClick={apply}
                disabled={busy || confirm.blocking || confirm.confirmations.some((_, i) => !ticked.has(i))}
            />
        </div>
    ) : (
        <div className="flex justify-content-end gap-2">
            <Button label="Cancel" text onClick={onHide} disabled={busy} />
            <Button label="Apply..." icon="pi pi-check" onClick={review} disabled={busy || loading || !!loadError || conflict} />
        </div>
    );

    return (
        <Dialog header={`Configure ${name}`} visible onHide={onHide} footer={footer} style={{ width: 'min(60rem, 95vw)' }} maximizable modal>
            {loading && <div className="text-color-secondary">Loading the config...</div>}
            {loadError && <Message severity="error" text={loadError} className="w-full" />}
            {!loading && !loadError && confirm && (
                <div className="flex flex-column gap-3">
                    <Message severity="warn" text={restartWarning({ own: loaded.own })} className="w-full" />
                    {confirm.blocking && (
                        <Message severity="error" className="w-full" text={`This can't be applied to the GCS's own daemon: it would take the GCS down with it. ${confirm.warnings.join('; ')}`} />
                    )}
                    {!confirm.blocking && confirm.warnings.map((w) => <Message key={w} severity="warn" text={w} className="w-full" />)}
                    {Object.values(guard.fieldWarnings).filter((w, i, a) => a.indexOf(w) === i).map((w) => (
                        <Message key={w} severity="warn" text={w} className="w-full" />
                    ))}
                    {confirm.confirmations.map((c, i) => (
                        <div key={c} className="flex align-items-center gap-2">
                            <Checkbox
                                inputId={`confirm-${i}`}
                                checked={ticked.has(i)}
                                onChange={(e) => setTicked((prev) => {
                                    const next = new Set(prev);
                                    if (e.checked) next.add(i);
                                    else next.delete(i);
                                    return next;
                                })}
                            />
                            <label htmlFor={`confirm-${i}`} className="text-sm">{c}</label>
                        </div>
                    ))}
                </div>
            )}
            {!loading && !loadError && !confirm && (
                <div className="flex flex-column gap-3">
                    {notice && <Message severity="warn" text={notice} className="w-full" />}
                    {conflict && (
                        <div className="flex align-items-center gap-2">
                            <Message severity="error" text="The config changed on the daemon since it was loaded." className="flex-1" />
                            <Button label="Reload" icon="pi pi-refresh" size="small" onClick={load} />
                        </div>
                    )}
                    {error && <Message severity="error" text={error} className="w-full" />}
                    {errorList && errorList.map((e) => <Message key={e} severity="error" text={e} className="w-full" />)}
                    <SelectButton value={mode} options={MODES} onChange={(e) => switchMode(e.value)} disabled={busy} />
                    {mode === 'raw' && (
                        <InputTextarea value={raw} onChange={(e) => setRaw(e.target.value)} rows={24} disabled={busy} style={{ fontFamily: 'monospace', width: '100%' }} />
                    )}
                    {mode === 'form' && state && (
                        <TabView>
                            <TabPanel header="Daemon">
                                <div className="flex flex-wrap gap-3">{DAEMON_FIELDS.map(daemonField)}</div>
                            </TabPanel>
                            <TabPanel header="Simulator">
                                <div className="flex flex-column gap-3">
                                    <div className="flex align-items-center gap-2">
                                        <Checkbox
                                            inputId="sim-present"
                                            checked={state.simulator.present}
                                            onChange={(e) => setState((s) => ({ ...s, simulator: { ...s.simulator, present: !!e.checked } }))}
                                            disabled={busy}
                                        />
                                        <label htmlFor="sim-present" className="text-sm">This daemon runs a simulator</label>
                                    </div>
                                    {state.simulator.present && (
                                        <>
                                            <div className="flex flex-column gap-1">
                                                <label className="text-sm">Package</label>
                                                <InputText
                                                    value={state.simulator.package}
                                                    placeholder="repo[//subdir][@version]"
                                                    onChange={(e) => setState((s) => ({ ...s, simulator: { ...s.simulator, package: e.target.value } }))}
                                                    disabled={busy}
                                                />
                                            </div>
                                            <span className="font-semibold text-sm">Simulator args</span>
                                            <KeyValueEditor
                                                rows={state.simulator.argRows}
                                                onChange={(argRows) => setState((s) => ({ ...s, simulator: { ...s.simulator, argRows } }))}
                                                disabled={busy}
                                            />
                                        </>
                                    )}
                                </div>
                            </TabPanel>
                            <TabPanel header={`Services (${state.services.length})${Object.keys(shown).some((k) => k.startsWith('services.')) ? ' ⚠' : ''}`}>
                                <div className="flex flex-column gap-2">
                                    {state.services.map((svc) => (
                                        <ServiceEditor
                                            key={svc.key}
                                            svc={svc}
                                            packages={packages}
                                            hasSimulator={state.simulator.present}
                                            errors={serviceErrors(svc.key)}
                                            expanded={expanded.has(svc.key)}
                                            onToggle={() => toggle(svc.key)}
                                            onChange={(next) => updateService(svc.key, next)}
                                            onRemove={() => setState((s) => ({ ...s, services: s.services.filter((x) => x.key !== svc.key) }))}
                                            onDuplicate={() => addService(duplicateService(svc))}
                                            disabled={busy}
                                        />
                                    ))}
                                    <div>
                                        <Button label="Add service" icon="pi pi-plus" text size="small" onClick={() => addService(newService())} disabled={busy} />
                                    </div>
                                </div>
                            </TabPanel>
                        </TabView>
                    )}
                </div>
            )}
        </Dialog>
    );
}

export default ConfigureDialog;
