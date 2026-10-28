import os
import sys
import json
import logging
# Simulator imports
from steeleagle_aviary.datatypes import GeodeticPoint
from steeleagle_aviary.simulator import Simulator

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logger = logging.getLogger('aviary/main')

def parse_point(point) -> dict:
    """Parses a geodetic point from the config.

    Points are given as [lat, lon, alt] arrays, as in the daemon's simulator
    args, or as {lat, lon, alt} tables, which may carry extra keys (such as a
    waypoint's speed or delay). Altitude defaults to 0.

    Args:
        point (list | dict): point from the config

    Returns:
        dict: point with lat, lon and alt keys, and any extra keys
    """
    if isinstance(point, (list, tuple)):
        if len(point) not in (2, 3):
            raise ValueError(f'Point {point} must be [lat, lon] or [lat, lon, alt]')
        return {'lat': point[0], 'lon': point[1], 'alt': point[2] if len(point) == 3 else 0}
    point = dict(point)
    point.setdefault('alt', 0)
    return point

def parse_origin(entry: dict) -> GeodeticPoint:
    """Parses the origin of a config entry, from its origin key or from its
    own lat, lon and alt keys."""
    point = parse_point(entry['origin'] if 'origin' in entry else entry)
    return GeodeticPoint(point['lat'], point['lon'], point['alt'])

def main():
    """Runs the simulator from the JSON args the daemon passes it.
    """
    # Read and load the config
    config = os.getenv('STEELEAGLE_PLUGIN_ARGS')
    if not config:
        logger.error('No config provided')
        sys.exit(1)
    try:
        config = json.loads(config)
    except json.JSONDecodeError as e:
        logger.error(f'Config failed to load, reason: {e}')
        sys.exit(1)

    app = Simulator()

    # Add actors, vehicles, and engines; engines go last since they look at
    # the vehicles and actors
    try:
        for i, actor in enumerate(config.get('actors', [])):
            tag = actor['tag']
            name = actor.get('name', f'{tag}-{i}')
            origin = parse_origin(actor)
            waypoints = [parse_point(w) for w in actor.get('waypoints', [])]
            args = actor.get('args', {})
            res = app.add_actor(name, tag, origin, waypoints, args)
            if not res:
                logger.error(f'Failed to add actor {name}')
                sys.exit(1)

        for vehicle in config.get('vehicles', []):
            name = vehicle['name']
            interface = vehicle.get('interface', 'steeleagle')
            origin = parse_origin(vehicle)
            args = vehicle.get('args', {})
            res = app.add_vehicle(name, interface, origin, args)
            if not res:
                logger.error(f'Failed to add vehicle {name}')
                sys.exit(1)

        for engine in config.get('engines', []):
            name = engine['name']
            interface = engine.get('interface', 'detection')
            args = engine.get('args', {})
            res = app.add_engine(name, interface, args)
            if not res:
                logger.error(f'Failed to add engine {name}')
                sys.exit(1)

    except Exception as e:
        logger.error(f'Failed to add objects, reason: {e}')
        sys.exit(1)

    logger.info('Simulation started! Connect with a vehicle to get started.')
    app.run()
