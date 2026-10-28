import { useMemo } from 'react';
import { Dropdown } from 'primereact/dropdown';
import { ConfirmDialog, confirmDialog } from 'primereact/confirmdialog';
import { vehicleLabel, vehicleOptions } from './flowCatalog.js';
import { displayName } from './vehicleNames.js';
import { setVehicle } from './flowModel.js';

// The Plan page's vehicle, shown on both the Mission Builder and Flow Builder
// toolbars. It is the flow's vehicle: the flow's producers stream from it,
// and the mission's types are those its driver cap supports. `group` names
// this picker's confirm dialog, since both tabs are mounted at once.
function VehiclePicker({ group, flow, setFlow, catalog, onPicked }) {
    const options = useMemo(() => (catalog ? vehicleOptions(catalog, flow.vehicle) : []), [catalog, flow.vehicle]);

    function pick(vehicle) {
        if (!vehicle || vehicle === flow.vehicle) return;
        const producers = flow.nodes.filter((n) => n.kind === 'producer').length;
        const apply = () => { setFlow((f) => setVehicle(f, vehicle)); onPicked?.(); };
        if (!producers) {
            apply();
            return;
        }
        confirmDialog({
            group,
            header: 'Change vehicle',
            message: `Switch to ${vehicleLabel(vehicle, options)}? This removes the ${producers} producer node(s) for ${displayName(flow.vehicle)} and their edges from the flow.`,
            icon: 'pi pi-exclamation-triangle',
            accept: apply,
        });
    }

    return (
        <>
            <ConfirmDialog group={group} />
            <Dropdown
                value={flow.vehicle}
                options={options.map((o) => ({ label: vehicleLabel(o, options), value: o }))}
                onChange={(e) => pick(e.value)}
                placeholder={catalog ? 'Select vehicle' : 'Loading vehicles…'}
                className="p-inputtext-sm"
                style={{ width: 240 }}
            />
        </>
    );
}

export default VehiclePicker;
