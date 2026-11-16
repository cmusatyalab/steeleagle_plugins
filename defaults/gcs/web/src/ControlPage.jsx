import { useEffect, useMemo, useState } from 'react';
import { Button } from 'primereact/button';
import { Toolbar } from 'primereact/toolbar';
import { ButtonGroup } from 'primereact/buttongroup';
import { Tooltip } from 'primereact/tooltip';
import { FileUpload } from 'primereact/fileupload';
import { Dropdown } from 'primereact/dropdown';
import { MultiSelect } from 'primereact/multiselect';
import { Dialog } from 'primereact/dialog';
import { InputText } from 'primereact/inputtext';
import React from 'react';
import { getApiUrl } from './urls.js';
import Mapbox from './Mapbox.jsx';
import { toggleVehicleInSquad } from './squadUtils.js';
import { STYLE_OPTIONS } from './mapStyles.js';
import { streamUpload, initialUploadState, applyUploadEvent, isUploadFinished, finishUpload, validMissionName, defaultMissionName } from './missionUpload.js';
import { displayName } from './vehicleNames.js';
import VehicleVideoPanel from './VehicleVideoPanel.jsx';
import { fetchPins, classCounts, visiblePins } from './detectionPins.js';

const vehicleOptionTemplate = (name) => (name ? <span title={name}>{displayName(name)}</span> : null);
const vehicleValueTemplate = (name, props) => (name ? <span title={name}>{displayName(name)}</span> : props.placeholder);

const cancelOptions = { icon: 'pi pi-fw pi-times', iconOnly: true, className: 'custom-cancel-btn p-button-danger' };
const chooseOptions = { label: 'Select...', icon: 'pi pi-fw pi-file', iconOnly: false, className: 'custom-choose-btn p-button-primary' };
const uploadOptions = { icon: 'pi pi-fw pi-cloud-upload', iconOnly: true, className: 'custom-upload-btn p-button-info' };

// Shared between the map and the video panel next to it so they're always
// the same height (passed to Mapbox as mapHeight, overriding its own
// '20rem' default) -- bumped up from that default now that removing the
// Swarm Controls Panel wrapper below frees up the vertical room for it.
const videoPanelHeight = '26rem';

// How often the detection pins are fetched.
const PIN_POLL_MS = 1000;
// Where this browser keeps the classes it hides from the map.
const HIDDEN_CLASSES_KEY = 'se-hidden-pin-classes';

function loadHiddenClasses() {
  try {
    const stored = JSON.parse(localStorage.getItem(HIDDEN_CLASSES_KEY) ?? '[]');
    return new Set(Array.isArray(stored) ? stored.filter((c) => typeof c === 'string') : []);
  } catch {
    return new Set();
  }
}

// The detection pins and their shared settings, fetched every PIN_POLL_MS
// while mounted; a failed fetch keeps the last ones.
function usePins() {
  const [state, setState] = useState({ pins: [], settings: null });
  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const result = await fetchPins();
        if (!cancelled) setState({ pins: result.pins ?? [], settings: result.settings ?? null });
      } catch {
        // keep the last pins
      }
    };
    poll();
    const id = setInterval(poll, PIN_POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);
  return state;
}

function ControlPage({ vehicles, selectedVehicle, setSelectedVehicle, resultsEngine, setResultsEngine, tracking, setTracking,
  toast, onCommand,
  setManualControl, squadList, setSquadList, takeOffAltitude, controlGroups,
  uploadState, setUploadState }) {
  const mapPanelSize = 0;
  const gcsFlow = vehicles?.find?.((v) => v.name === selectedVehicle)?.gcs_flow || '';
  const noVideoLabel = 'No video: the vehicle isn\'t sending video to the GCS (a mission dataflow may be active; Hold restores it)'
    + (gcsFlow.startsWith('no video:') || gcsFlow.startsWith('error:') ? ` (${gcsFlow})` : '');
  const [mapStyle, setMapStyle] = useState('streets');
  const { pins, settings: pinSettings } = usePins();
  // The classes this browser hides; a class not listed, even a new one, shows
  const [hiddenClasses, setHiddenClasses] = useState(loadHiddenClasses);
  const pinClasses = useMemo(() => classCounts(pins), [pins]);
  const shownPins = useMemo(() => visiblePins(pins, hiddenClasses), [pins, hiddenClasses]);
  const onShownClasses = (shown) => {
    const next = new Set(hiddenClasses);
    for (const { cls } of pinClasses) {
      if (shown.includes(cls)) next.delete(cls); else next.add(cls);
    }
    setHiddenClasses(next);
    localStorage.setItem(HIDDEN_CLASSES_KEY, JSON.stringify([...next]));
  };
  // The last mission name used, kept as the next upload's default
  const [missionName, setMissionName] = useState('');
  // The FileUpload event waiting for its mission name, while the dialog is open
  const [pendingUpload, setPendingUpload] = useState(null);
  const missionNameError = validMissionName(missionName);
  const uploadInProgress = uploadState != null && !isUploadFinished(uploadState);

  // Asks for the mission name before uploading, prefilled with the last name
  // or, the first time, the first file's name.
  const uploadHandler = (event) => {
    if (uploadInProgress) {
      toast.current.show({ severity: 'warn', summary: 'Upload already in progress', detail: `Wait for the current mission upload to finish before starting another.` });
      return;
    }
    if (squadList == null || squadList.length == 0) {
      toast.current.show({ severity: 'warn', summary: 'No Vehicles in Squad', detail: `Please select at least one vehicle to control.` });
      return;
    }
    setMissionName((name) => name || defaultMissionName(event.files[0]?.name ?? ''));
    setPendingUpload(event);
  };

  const confirmUpload = async () => {
    const event = pendingUpload;
    setPendingUpload(null);
    if (event == null || missionNameError) return;
    const targets = [...squadList];
    const form = new FormData();
    form.append('name', missionName);
    for (const file of event.files) form.append('files', file);
    for (const vehicle of targets) form.append('vehicles', vehicle);
    setUploadState(initialUploadState(targets));
    try {
      await streamUpload(getApiUrl('/api/upload'), { method: 'POST', body: form },
        (ev) => setUploadState(s => applyUploadEvent(s, ev)));
      setUploadState(s => finishUpload(s));
      event.options.clear();
    } catch (e) {
      setUploadState(s => applyUploadEvent(s, { type: 'error', detail: e.message }));
    }
  };

  const onMissionStart = async () => {
    const body = {};
    body.vehicles = squadList;
    if (squadList == null || squadList.length == 0) {
      toast.current.show({ severity: 'warn', summary: 'No Vehicles in Squad', detail: `Please select at least one vehicle to control.` });
      return;
    } else {
      setManualControl(false);
      const requestOptions = {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      };
      const response = await fetch(getApiUrl('/api/start'), requestOptions);
      if (!response.ok) {
        const result = await response.json();
        toast.current.show({ severity: 'error', summary: 'Mission Error', detail: `HTTP error! status: ${result.detail}` });
      }
      else {
        const result = await response.json();
        toast.current.show({ severity: 'success', summary: 'Mission Success', detail: `${result}` });
      }
    }
  }

  const itemTemplate = (file) => (
    <span className="text-left ml-3">{file.name}</span>
  );

  const missonControls = useMemo(() => (
    <>
      <Tooltip target=".custom-choose-btn" content="Select Mission Files" position="bottom" />
      <Tooltip target=".custom-upload-btn" content="Upload Mission" position="bottom" />
      <Tooltip target=".custom-cancel-btn" content="Clear Selected Files" position="bottom" />
      <FileUpload className="m-2" itemTemplate={itemTemplate} chooseOptions={chooseOptions} uploadOptions={uploadOptions} cancelOptions={cancelOptions} mode="advanced" name="files" multiple maxFileSize={128 * 1024 * 1024} customUpload uploadHandler={uploadHandler} disabled={uploadInProgress} />
      <Button icon="pi pi-play-circle" label="Start Mission" className="m-2 p-button-success" onClick={onMissionStart} />
    </>
  ), [uploadHandler, onMissionStart, uploadInProgress]);

  const controlButtons = useMemo(() => (
    <div className="flex flex-row flex-wrap gap-2">
      <ButtonGroup>
        <Button outlined size="small" icon="pi pi-check-circle" label="Arm" onClick={() => onCommand({ arm: true })} />
        <Button outlined size="small" iconPos="right" icon="pi pi-times-circle" label="Disarm" onClick={() => onCommand({ arm: false })} />
      </ButtonGroup>
      <ButtonGroup>
        <Button outlined size="small" icon="pi pi-arrow-up" label="Takeoff" onClick={() => onCommand({ takeoff: takeOffAltitude })} />
        <Button outlined size="small" iconPos="right" icon="pi pi-arrow-down" label="Land" onClick={() => onCommand({ land: true })} />
      </ButtonGroup>
      <ButtonGroup>
        <Button outlined size="small" icon="pi pi-home" label="RTH" onClick={() => onCommand({ rth: true })} />
        <Button outlined size="small" iconPos="right" icon="pi pi-stop-circle" label="Hold" onClick={() => onCommand({ hold: true })} />
      </ButtonGroup>
    </div>
  ), [onCommand, takeOffAltitude]);

  const vehicleNames = useMemo(() => vehicles.map(v => v.name), [vehicles]);

  const onToggleVehicle = (name) => setSquadList((prev) => toggleVehicleInSquad(prev, name));

  return (
    <div className="p-2">
      <div className="flex flex-column">
        <div className="flex align-items-center gap-2 mb-2 px-2 py-1 border-round" style={{ backgroundColor: 'var(--surface-card)', border: '1px solid var(--surface-border)' }}>
          {/* Left/center/right flex-1 sections roughly align with the map
              and video panels below. */}
          <div className="flex-1 flex justify-content-start gap-2">
            <Dropdown value={mapStyle} options={STYLE_OPTIONS} onChange={(e) => setMapStyle(e.value)}
              className="w-full md:w-9rem" />
            {pinClasses.length > 0 && (
              <MultiSelect value={pinClasses.map((c) => c.cls).filter((c) => !hiddenClasses.has(c))}
                options={pinClasses.map(({ cls, count }) => ({ label: `${cls} (${count})`, value: cls }))}
                onChange={(e) => onShownClasses(e.value)} placeholder="Detections" display="comma" maxSelectedLabels={2}
                selectedItemsLabel="{0} classes" className="w-full md:w-12rem" />
            )}
          </div>
          <div className="flex align-items-center gap-1">
            <Button
              size="small"
              outlined={!tracking}
              severity={tracking ? undefined : 'secondary'}
              icon={tracking ? 'pi pi-bullseye' : 'pi pi-map'}
              label="Vehicle Tracking"
              tooltip={`Tracking ${tracking ? 'On' : 'Off'}: recenters the map on the selected vehicle`}
              tooltipOptions={{ position: 'bottom' }}
              onClick={() => setTracking(!tracking)}
              aria-label="Toggle tracking"
            />
          </div>
          <div className="flex-1 flex justify-content-end">
            <Dropdown value={selectedVehicle} checkmark={true} onChange={(e) => setSelectedVehicle(e.value)} options={vehicleNames} useOptionAsValue optionLabel="name"
              itemTemplate={vehicleOptionTemplate} valueTemplate={vehicleValueTemplate}
              placeholder="Select Video Feed" className="w-full md:w-14rem" />
          </div>
        </div>
        <div className="grid m-0">
          <div className="col-12 lg:col-6 p-2">
            <Mapbox selectedVehicle={selectedVehicle} vehicles={vehicles} mapPanelSize={mapPanelSize} tracking={tracking}
              mapHeight={videoPanelHeight} squadList={squadList} onToggleVehicle={onToggleVehicle} controlGroups={controlGroups} mapStyle={mapStyle}
              pins={shownPins} expiryS={pinSettings?.expiry_s ?? 60} />
          </div>
          <div className="col-12 lg:col-6 p-2">
            <VehicleVideoPanel vehicle={selectedVehicle} engine={resultsEngine} setEngine={setResultsEngine}
              height={videoPanelHeight} noVideoLabel={noVideoLabel} />
          </div>
        </div>
        <div className="my-2" style={{ overflowX: 'auto' }}>
          <Toolbar className="w-full flex-nowrap" start={controlButtons} end={missonControls} />
        </div>
        <Dialog header="Upload mission" visible={pendingUpload != null} style={{ width: '28rem' }}
          onHide={() => setPendingUpload(null)}
          footer={<Button label="Upload" icon="pi pi-cloud-upload" disabled={missionNameError !== null} onClick={confirmUpload} />}>
          <label htmlFor="upload-name" className="block mb-2">Mission name</label>
          <InputText id="upload-name" className="w-full" value={missionName} autoFocus
            onChange={(e) => setMissionName(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && missionNameError === null) confirmUpload(); }}
            invalid={missionName !== '' && missionNameError !== null}
            placeholder="e.g. survey_north" />
          {missionName !== '' && missionNameError && <small className="block mt-1 p-error">{missionNameError}</small>}
          <div className="mt-3">
            Upload {pendingUpload?.files?.map((f) => f.name).join(', ')} to {squadList?.map(displayName).join(', ')}
          </div>
        </Dialog>
      </div>
    </div>
  );
}

export default React.memo(ControlPage);
