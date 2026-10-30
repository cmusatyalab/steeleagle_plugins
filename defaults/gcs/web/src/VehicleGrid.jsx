import Status from './Status.jsx';
import { useVideoFeeds } from './useVideoFeeds.js';

function VehicleGrid({ vehicles, selectable, squadList, onToggle, cardColumnClass = 'col-12 lg:col-3 p-2' }) {
    // One poll of /api/streams for every card
    const videoFeeds = useVideoFeeds();
    return (
        <>
            {vehicles.map((v) => (
                <div className={cardColumnClass} key={v.name}>
                    <Status
                        vehicle={v}
                        video={videoFeeds[v.name]}
                        selectable={selectable}
                        selected={!!(squadList && squadList.includes(v.name))}
                        onToggle={onToggle ? () => onToggle(v.name) : undefined}
                    />
                </div>
            ))}
        </>
    );
}

export default VehicleGrid;
