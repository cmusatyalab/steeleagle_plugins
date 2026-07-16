import logging
import argparse
# Toml parser
import toml
# Simulator imports
from steeleagle_aviary.datatypes import GeodeticPoint
from steeleagle_aviary.simulator import Simulator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("Aviary/main")

def main():
    """Runs the simulator from either a config file or from JSON.
    """
    parser = argparse.ArgumentParser(
        description="Simulates digital twin SteelEagle drones in a configurable 3D world."
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config.toml",
        help="override config file path (default: config.toml)"
    )
    parser.add_argument(
        "--jsonb64",
        type=str,
        help="base 64 encoded JSON string"
    )
    args = parser.parse_args()

    config = None
    if args.jsonb64:
        logger.info(f'Attempting to read from json byte string {args.jsonb64}')
        try:
            decoded = base64.b64decode(args.jsonb64)
            config = json.loads(decoded)
        except (base64.binascii.Error, json.JSONDecodeError) as e:
            logger.error(f'Failed to read JSON byte string')
            quit()
    else:
        logger.info(f'Attempting to read from config file {args.config}')
        try:
            with open(args.config) as file:
                config = toml.load(file)
        except Exception as e:
            logger.error(f'Failed to load config file {args.config}, reason: {e}')
            quit()

    app = Simulator()

    try:
        for actor in config['actors']:
            name = actor['name']
            tag = actor['tag']
            origin = GeodeticPoint(actor['lat'], actor['lon'], actor['alt'])
            waypoints = actor.get('waypoints', [])
            kwargs = actor.get('kwargs', {})
            app.add_actor(name, tag, origin, waypoints, **kwargs)

        for vehicle in config['vehicles']:
            name = vehicle['name']
            interface = vehicle['interface']
            origin = GeodeticPoint(vehicle['lat'], vehicle['lon'], vehicle['lon'])
            ifargs = vehicle.get('ifargs', {})
            kwargs = vehicle.get('kwargs', {})
            app.add_vehicle(name, interface, ifargs, origin, **kwargs)

        # TODO: Add engines

    except Exception as e:
        logger.error(f'Failed to add objects, reason: {e}')
        quit()

    logger.info('Simulation started! Connect with a vehicle to get started.')
    app.run()
