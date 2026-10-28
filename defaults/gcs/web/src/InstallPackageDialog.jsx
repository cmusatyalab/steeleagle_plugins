import React, { useState } from 'react';
import { Dialog } from 'primereact/dialog';
import { Button } from 'primereact/button';
import { InputText } from 'primereact/inputtext';
import { Message } from 'primereact/message';
import { installPackage } from './daemonsApi.js';

// Installs a package on a daemon by URI. The daemon downloads it now, which
// can take minutes, and uses it from its next restart.
function InstallPackageDialog({ daemon, onHide, onInstalled }) {
    const [uri, setUri] = useState('');
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState(null);

    const install = async () => {
        setBusy(true);
        setError(null);
        const failure = await installPackage(daemon.ip, uri.trim());
        setBusy(false);
        if (failure) setError(failure);
        else onInstalled(uri.trim());
    };

    const footer = (
        <div className="flex justify-content-end gap-2">
            <Button label="Cancel" text onClick={onHide} disabled={busy} />
            <Button label={busy ? 'Installing...' : 'Install'} icon="pi pi-download" onClick={install} disabled={busy || !uri.trim()} loading={busy} />
        </div>
    );

    return (
        <Dialog header="Install Package" visible onHide={busy ? () => {} : onHide} footer={footer} style={{ width: 'min(40rem, 95vw)' }} modal>
            <div className="flex flex-column gap-2">
                <label htmlFor="package-uri" className="text-sm">Package URI</label>
                <InputText
                    id="package-uri"
                    value={uri}
                    placeholder="repo[//subdir][@version]"
                    onChange={(e) => setUri(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && uri.trim() && !busy && install()}
                    disabled={busy}
                    autoFocus
                />
                <small className="text-color-secondary">
                    For example https://github.com/cmusatyalab/steeleagle_plugins//defaults/h264@v4_backend_rework. The daemon downloads it now
                    and uses it from its next restart.
                </small>
                {error && <Message severity="error" text={error} className="w-full" />}
            </div>
        </Dialog>
    );
}

export default InstallPackageDialog;
