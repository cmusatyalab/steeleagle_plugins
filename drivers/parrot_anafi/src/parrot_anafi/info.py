import logging
# Protocol imports
import steeleagle_protocol.v1.services.driver.info_pb2 as info_proto
from steeleagle_protocol.v1.services.driver.info_pb2_grpc import InfoServiceServicer
# Olympe imports
from olympe.messages.common.SettingsState import ProductNameChanged

logger = logging.getLogger('parrot-anafi/info')

DEFAULT_MODEL = 'Parrot Anafi'


class Info(InfoServiceServicer):
    """Info Service implementation.
    """
    def __init__(self, drone):
        self.drone = drone

    def GetVehicleInfo(self, request, context):
        model = DEFAULT_MODEL
        try:
            state = self.drone.get_state(ProductNameChanged)
            if state and state.get('name'):
                model = state['name']
        except Exception as e:
            logger.warning(f'could not read product name from drone, using default: {e}')
        return info_proto.GetVehicleInfoResponse(model=model)
