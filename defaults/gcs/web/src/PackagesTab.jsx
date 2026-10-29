import React, { useCallback, useEffect, useRef, useState } from 'react';
import { DataTable } from 'primereact/datatable';
import { Column } from 'primereact/column';
import { Button } from 'primereact/button';
import { Message } from 'primereact/message';
import { Tag } from 'primereact/tag';
import { confirmDialog } from 'primereact/confirmdialog';
import InstallPackageDialog from './InstallPackageDialog.jsx';
import { fetchPackages, installPackage, removePackage } from './daemonsApi.js';

const formatTime = (iso) => {
    const t = new Date(iso);
    return Number.isNaN(t.getTime()) || t.getFullYear() < 2000 ? '—' : t.toLocaleString();
};

// The Packages tab: the packages installed on one daemon, with the plugins
// each holds, installing, reinstalling and removing them. Reinstalling
// installs a package's own URI again, fetching its branch's latest commit. Changes take effect when the
// daemon next restarts; `pending` holds the ones this page made since then
// ({uri: 'install' | 'remove'}), and onRestartNow restarts it.
function PackagesTab({ daemon, toast, pending, onPending, onRestartNow, disabled }) {
    const [packages, setPackages] = useState(null);
    const [error, setError] = useState(null);
    const [installing, setInstalling] = useState(false);
    // The packages being reinstalled or removed: {uri: 'reinstall' | 'remove'}
    const [busy, setBusy] = useState({});

    const latest = useRef(0);

    // Only the newest answer counts, and a failed one clears the list, so
    // another daemon's packages are never shown (or removed) under this one
    const load = useCallback(async () => {
        const request = ++latest.current;
        const res = await fetchPackages(daemon.ip);
        if (request !== latest.current) return;
        setError(res.error);
        setPackages(res.packages);
    }, [daemon.ip]);

    useEffect(() => {
        // eslint-disable-next-line react-hooks/set-state-in-effect
        load();
    }, [load, daemon.state]);

    // Runs request on uri, marking it busy with action until it answers
    const run = async (uri, action, request) => {
        setBusy((prev) => ({ ...prev, [uri]: action }));
        const failure = await request(daemon.ip, uri);
        setBusy((prev) => {
            const next = { ...prev };
            delete next[uri];
            return next;
        });
        return failure;
    };

    const remove = async (uri) => {
        const failure = await run(uri, 'remove', removePackage);
        if (failure) {
            toast.current.show({ severity: 'error', summary: 'Remove package', detail: failure });
            return;
        }
        onPending(uri, 'remove');
        load();
    };

    const reinstall = async (uri) => {
        const failure = await run(uri, 'reinstall', installPackage);
        if (failure) {
            toast.current.show({ severity: 'error', summary: 'Reinstall package', detail: failure });
            return;
        }
        toast.current.show({ severity: 'success', summary: 'Reinstall package', detail: `Reinstalled ${uri}` });
        onPending(uri, 'install');
        load();
    };

    const confirmRemove = (uri) => confirmDialog({
        message: `Remove ${uri}? Services that use it won't start after the daemon next restarts.`,
        header: 'Remove Package',
        icon: 'pi pi-trash',
        acceptLabel: 'Remove',
        acceptClassName: 'p-button-danger',
        accept: () => remove(uri),
    });

    const pendingCount = Object.keys(pending).length;

    return (
        <div className="flex flex-column gap-2">
            {installing && (
                <InstallPackageDialog
                    daemon={daemon}
                    onHide={() => setInstalling(false)}
                    onInstalled={(uri) => {
                        setInstalling(false);
                        toast.current.show({ severity: 'success', summary: 'Install package', detail: `Installed ${uri}` });
                        onPending(uri, 'install');
                        load();
                    }}
                />
            )}
            <div>
                <Button label="Install Package..." icon="pi pi-download" size="small" onClick={() => setInstalling(true)} disabled={disabled} />
            </div>
            {pendingCount > 0 && (
                <div className="flex align-items-center gap-2">
                    <Message severity="info" text="Takes effect when the daemon next restarts." className="flex-1" />
                    <Button label="Restart now" icon="pi pi-refresh" size="small" severity="danger" onClick={onRestartNow} disabled={disabled} />
                </div>
            )}
            {error && <Message severity="error" text={error} className="w-full" />}
            <DataTable value={packages ?? []} size="small" dataKey="uri" sortField="uri" sortOrder={1}
                emptyMessage={packages === null ? (error ? 'No packages listed.' : 'Loading packages...') : 'No packages installed.'}>
                <Column field="uri" header="Package" sortable body={(p) => (
                    <span className="flex align-items-center gap-2">
                        <span style={{ fontFamily: 'monospace' }} className="text-sm">{p.uri}</span>
                        {pending[p.uri] === 'install' && <Tag value="pending install" severity="info" />}
                        {pending[p.uri] === 'remove' && <Tag value="pending removal" severity="warning" />}
                    </span>
                )} />
                <Column header="Plugins" body={(p) => p.plugins.map((pl) => pl.name).join(', ') || '—'} />
                <Column field="modified" header="Modified" sortable body={(p) => formatTime(p.modified)} />
                <Column header="" style={{ width: '14rem' }} body={(p) => (
                    <span className="flex gap-1 justify-content-end">
                        <Button label="Reinstall" icon="pi pi-sync" size="small" text onClick={() => reinstall(p.uri)}
                            tooltip="Fetch this package again from its URI" tooltipOptions={{ position: 'top' }}
                            loading={busy[p.uri] === 'reinstall'} disabled={disabled || p.uri in busy} />
                        <Button label="Remove" icon="pi pi-trash" size="small" text severity="danger" onClick={() => confirmRemove(p.uri)}
                            loading={busy[p.uri] === 'remove'} disabled={disabled || p.uri in busy || pending[p.uri] === 'remove'} />
                    </span>
                )} />
            </DataTable>
        </div>
    );
}

export default PackagesTab;
