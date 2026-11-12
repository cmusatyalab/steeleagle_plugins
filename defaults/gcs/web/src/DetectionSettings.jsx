import { useEffect, useRef, useState } from 'react';
import { Knob } from 'primereact/knob';
import { Chip } from 'primereact/chip';
import { Button } from 'primereact/button';
import { fetchPins, putPinSettings, clearPins } from './detectionPins.js';

// How often the shared settings and pin count are re-read while shown.
const POLL_MS = 2000;
// How long after the last knob change the settings are sent.
const SEND_DELAY_MS = 300;

// The knobs, each showing a setting in its own unit: expiry is kept in
// seconds but shown and set in whole minutes.
const KNOBS = [
  { key: 'radius_m', label: 'Grouping Radius', icon: 'pi pi-circle', min: 1, max: 20, template: '{value}m', toKnob: (v) => v, fromKnob: (v) => v },
  { key: 'expiry_s', label: 'Pin Expiry', icon: 'pi pi-clock', min: 1, max: 60, template: '{value}min', toKnob: (v) => Math.round(v / 60), fromKnob: (v) => v * 60 },
  { key: 'min_sightings', label: 'Min Sightings', icon: 'pi pi-eye', min: 1, max: 5, template: '{value}', toKnob: (v) => v, fromKnob: (v) => v },
];

// The Detections tab of the settings pop-up: the detection pins' settings,
// which the GCS shares with everyone viewing it, and clearing every pin.
// It reads them itself, as the pop-up unmounts it when closed. toast, if
// given, shows failures.
function DetectionSettings({ toast }) {
  const [server, setServer] = useState(null);
  const [pinCount, setPinCount] = useState(0);
  // The settings being changed here, shown until sent, then the server's
  const [pending, setPending] = useState(null);
  const [confirming, setConfirming] = useState(false);
  const sendTimer = useRef(null);

  const fail = (summary, err) => {
    if (toast?.current) toast.current.show({ severity: 'error', summary, detail: err.message });
    else console.error(summary, err);
  };

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const result = await fetchPins();
        if (cancelled) return;
        setServer(result.settings);
        setPinCount(result.pins?.length ?? 0);
      } catch {
        // keep the last settings
      }
    };
    poll();
    const id = setInterval(poll, POLL_MS);
    return () => { cancelled = true; clearInterval(id); clearTimeout(sendTimer.current); };
  }, []);

  const change = (key, value) => {
    const next = { ...(pending ?? server), [key]: value };
    setPending(next);
    clearTimeout(sendTimer.current);
    sendTimer.current = setTimeout(async () => {
      try {
        setServer(await putPinSettings(next));
      } catch (err) {
        fail('Detection settings not saved', err);
      }
      setPending(null);
    }, SEND_DELAY_MS);
  };

  const clear = async () => {
    setConfirming(false);
    try {
      await clearPins();
      setPinCount(0);
    } catch (err) {
      fail('Pins not cleared', err);
    }
  };

  const shown = pending ?? server;
  return (
    <div className="flex flex-column gap-2">
      <span className="text-sm text-color-secondary m-2 mb-0">Shared by everyone viewing this GCS</span>
      {shown ? (
        <div className="flex flex-row gap-2">
          {KNOBS.map((k) => (
            <div key={k.key} className="flex flex-column flex-wrap align-content-center m-2">
              <Knob className="flex align-items-center justify-content-center" value={k.toKnob(shown[k.key])}
                onChange={(e) => change(k.key, k.fromKnob(e.value))} min={k.min} max={k.max} step={1} valueTemplate={k.template} />
              <Chip className="flex align-items-center justify-content-center" label={k.label} icon={k.icon} />
            </div>
          ))}
        </div>
      ) : (
        <span className="m-2">Loading…</span>
      )}
      <div className="flex align-items-center gap-2 m-2">
        {confirming ? (
          <>
            <span>{`Clear all ${pinCount} pins for everyone?`}</span>
            <Button size="small" severity="danger" label="Clear" onClick={clear} />
            <Button size="small" text label="Cancel" onClick={() => setConfirming(false)} />
          </>
        ) : (
          <Button size="small" outlined severity="danger" icon="pi pi-trash" label="Clear all pins" onClick={() => setConfirming(true)} />
        )}
      </div>
    </div>
  );
}

export default DetectionSettings;
