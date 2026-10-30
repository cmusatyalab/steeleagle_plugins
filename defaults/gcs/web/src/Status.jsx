import React from "react";
import { Card } from 'primereact/card';
import { Badge } from 'primereact/badge';
import { ProgressBar } from 'primereact/progressbar';
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome';
import { faSatellite } from '@fortawesome/free-solid-svg-icons';
import { vehicleSpeed, hasTelemetry, isVehicleDisconnected, isVehicleSelectable, vehicleStatus, vehicleStatusColor, bearingToCardinal } from './mapUtils.js';
import { displayName } from './vehicleNames.js';

const cardPassthrough = {
    body: { style: { padding: '0.4rem 0.75rem' } },
    content: { style: { padding: 0 } },
};

// One link to the vehicle: green while it is up, grey and struck through
// while it is down. `title` says what it is and how it is.
function LinkIndicator({ icon, up, title }) {
    return (
        <span title={title} aria-label={title} style={{ position: 'relative', display: 'inline-flex', flexShrink: 0 }}>
            <i className={`pi ${icon}`} style={{ fontSize: '0.8rem', color: up ? 'var(--green-500)' : 'var(--gray-500)' }} />
            {!up && (
                <span style={{
                    position: 'absolute', left: '-2px', right: '-2px', top: '50%', height: '1.5px',
                    backgroundColor: 'var(--gray-400)', transform: 'rotate(-35deg)',
                }} />
            )}
        </span>
    );
}

// How long ago something was last seen, for a tooltip
function ago(seconds) {
    if (!Number.isFinite(seconds) || seconds >= 1e5) return 'never seen';
    return `last seen ${Math.round(seconds)} s ago`;
}

// A vehicle's card. The card's colour says whether it can be controlled;
// the indicators say whether its telemetry and video are reaching the GCS,
// which they may not be even while it can (e.g. after a bad dataflow).
// `video` is the vehicle's entry from useVideoFeeds, if any.
function Status({ vehicle, video, selectable, selected, onToggle }) {
    if (!vehicle) {
        return (<>No vehicles connected.</>);
    }

    const disconnected = isVehicleDisconnected(vehicle);
    const telemetry = hasTelemetry(vehicle);
    const videoLive = !!video?.live;
    const showCheck = !!(selectable && selected);
    const status = vehicleStatus(disconnected, showCheck);
    const statusColor = vehicleStatusColor(status);
    // A vehicle with no data can still be selected, to hold it
    const clickable = selectable && isVehicleSelectable(vehicle);

    let battery_severity = "info";

    //consult protocol/telemetry.proto for enum mappings
    if (vehicle.battery <= 25) {
        battery_severity = "red-500";
    } else if (vehicle.battery <= 50) {
        battery_severity = "orange-500";
    } else {
        battery_severity = "green-500";
    }

    const speed = vehicleSpeed(vehicle.velocity);

    return (
        <Card
            pt={cardPassthrough}
            style={{
                position: 'relative',
                backgroundColor: `color-mix(in srgb, ${statusColor} 12%, var(--surface-0))`,
                width: '100%',
                cursor: clickable ? 'pointer' : (selectable ? 'not-allowed' : 'default'),
                border: `2px solid ${statusColor}`,
                opacity: disconnected ? 0.6 : 1,
            }}
            onClick={clickable ? onToggle : undefined}
        >
            {showCheck && (
                <Badge
                    value={<i className="pi pi-check" style={{ fontSize: '0.6rem' }} />}
                    style={{ position: 'absolute', top: '-6px', right: '-6px', backgroundColor: statusColor, color: '#ffffff' }}
                />
            )}
            <div className="flex flex-column gap-1">
                <div className="flex align-items-center gap-2" style={{ minWidth: 0 }}>
                    <span
                        className="font-semibold text-sm white-space-nowrap overflow-hidden text-overflow-ellipsis"
                        style={{ minWidth: 0 }}
                        title={vehicle.name}
                    >
                        {displayName(vehicle.name)}
                    </span>
                    <span className="text-color-secondary text-xs white-space-nowrap" style={{ flexShrink: 0 }}>{vehicle.model}</span>
                    <span className="flex-1" />
                    <LinkIndicator icon="pi-chart-line" up={telemetry}
                        title={`Telemetry: ${telemetry ? 'flowing' : 'not flowing'} (${ago(vehicle.last_updated)})`} />
                    <LinkIndicator icon="pi-video" up={videoLive}
                        title={`Video: ${videoLive ? 'flowing' : 'not flowing'} (${video ? ago(video.ageMs / 1000) : 'never seen'})`} />
                    <LinkIndicator icon="pi-sliders-h" up={!disconnected}
                        title={disconnected ? `Control: unreachable${vehicle.gcs_flow ? ` (${vehicle.gcs_flow})` : ''}` : 'Control: reachable (service running)'} />
                </div>
                {/* Dimmed while telemetry isn't flowing, so stale values don't read as current */}
                <div className="flex align-items-center gap-2" style={{ opacity: telemetry ? 1 : 0.4 }}>
                    <ProgressBar color={`var(--${battery_severity})`} style={{ width: '3rem', height: '4px' }} value={vehicle.battery} showValue={false} />
                    <span className="text-xs font-semibold" style={{ minWidth: '2rem', textAlign: 'right' }}>{Math.round(vehicle.battery)}%</span>
                    <span className="flex-1" />
                    <span className="text-xs text-color-secondary flex align-items-center gap-1">
                        <i className="pi pi-arrow-up" style={{ display: 'inline-block', transform: `rotate(${vehicle.bearing}deg)`, fontSize: '0.65rem' }} />
                        {Math.round(vehicle.bearing)}° {bearingToCardinal(vehicle.bearing)}
                    </span>
                    <span className="text-xs text-color-secondary">{speed.toFixed(1)} m/s</span>
                    <span className="text-xs text-color-secondary flex align-items-center gap-1">
                        <FontAwesomeIcon icon={faSatellite} style={{ fontSize: '0.65rem' }} />
                        {vehicle.sats}
                    </span>
                </div>
            </div>
        </Card>
    );
}

export default Status;
