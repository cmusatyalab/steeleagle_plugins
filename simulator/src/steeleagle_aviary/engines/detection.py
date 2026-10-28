import os
import logging
import threading
import uriel
# Panda3D imports
from panda3d.core import TransformState
# Simulator imports
from steeleagle_aviary.vehicle import Vehicle
# Base import
from steeleagle_aviary.engines.base import Engine
# Protocol import
import steeleagle_protocol.v1.messages.result.result_pb2 as result_proto

"""Mock object detection engine."""

logger = logging.getLogger('aviary/engines/detection')

# Uriel result type, matching sim_detector.plugin.toml
RESULT_TYPE = 'steeleagle_protocol/v1/messages/result/DetectionResult'
# Maximum size of a serialized detection result
MAX_RESULT_SIZE = 64 << 10
# How long the engine waits for an input before checking if it was stopped
POLL_TIMEOUT = 1.0

class DetectionEngine(Engine):
    def __init__(self, name, vehicles, actors, **kwargs):
        super().__init__(name, vehicles, actors, **kwargs)
        self.range = kwargs.get('range', 20.0)
        self.stopped = threading.Event()
        self.error = None

    def inference(self, vehicle: Vehicle) -> result_proto.DetectionResult:
        """Detects the actors within range of the vehicle's camera view."""
        # Create a projection matrix for the camera view
        proj_mat = vehicle.camera.camera.node().getLens().getProjectionMatInv()
        vehicle.camera.proj.setTransform(TransformState.makeMat(proj_mat))
        # Iterate through actors and find bounding boxes
        result = result_proto.DetectionResult()
        for _, act in self.actors.items():
            if (act.current_position() - vehicle.current_position()).length() <= self.range:
                relative_location = vehicle.camera.camera.getRelativePoint(vehicle.camera.camera.getParent(), act.current_position())
                # Cull detection if it is not in the camera's viewport
                if not vehicle.camera.camera.node().isInView(relative_location):
                    continue
                mins, maxes = act.object.getTightBounds(vehicle.camera.proj)
                lr_bound = [(lambda x: min(1.0, max(0.5 + (x / 2), 0.0)))(x) for x in [mins.x, maxes.x]]
                td_bound = [(lambda x: min(1.0, max(0.5 - (x / 2), 0.0)))(x) for x in [mins.y, maxes.y]]
                result.detections.append(result_proto.Detection(
                    class_name=act.tag,
                    score=1.0,
                    bbox=result_proto.BoundingBox(
                        x_min=lr_bound[0],
                        y_min=td_bound[1],
                        x_max=lr_bound[1],
                        y_max=td_bound[0],
                        ),
                    ))
        return result

    def find_vehicle(self, producer: str) -> Vehicle | None:
        """Finds the vehicle whose camera published on `producer`.

        The vehicle names its producers {vehicle name}.{camera name}, where
        the vehicle name is qualified as {qualifier}.{name}.
        """
        for name, vehicle in self.vehicles.items():
            if producer.startswith(f'{self.qualifier}.{name}.'):
                return vehicle
        # Inputs from anywhere else are attributed to the only vehicle, if
        # there is just one
        if len(self.vehicles) == 1:
            return next(iter(self.vehicles.values()))
        return None

    def serve(self, ready: threading.Event):
        """Serves the engine on Uriel until stopped."""
        try:
            engine = uriel.Engine(self.qualified_name, result_type=RESULT_TYPE, max_output_size=MAX_RESULT_SIZE)
        except Exception as e:
            self.error = e
            return
        finally:
            ready.set()
        logger.info(f'Serving engine {self.qualified_name}')
        try:
            while not self.stopped.is_set():
                inp = engine.next(timeout=POLL_TIMEOUT)
                if inp is None:
                    continue
                # The input itself is ignored: results come from the
                # simulated scene, as seen by the vehicle that sent it
                producer = inp.id.producer
                try:
                    vehicle = self.find_vehicle(producer)
                    if vehicle is None:
                        logger.warning(f'No vehicle for producer {producer}, forwarding input without a result')
                        engine.forward()
                        continue
                    # Forwarded so that engines after this one in the flow
                    # get the frame too
                    engine.forward(result=self.inference(vehicle).SerializeToString())
                except Exception as e:
                    logger.error(f'Inference failed for producer {producer}, reason: {e}')
                    engine.forward()
        finally:
            engine.close()

    def start(self):
        self.qualifier = os.getenv('STEELEAGLE_SIMULATOR_QUALIFIER', '')
        # The daemon serves the engine on Uriel by its qualified name
        self.qualified_name = f'{self.qualifier}.{self.name}'

        # A Uriel engine must be used on the thread that created it. It is
        # created before start returns, since the daemon's compute server adds
        # it once the simulator's services have started
        ready = threading.Event()
        self.thread = threading.Thread(target=self.serve, args=(ready,), name=f'engine [{self.name}]', daemon=True)
        self.thread.start()
        ready.wait()
        if self.error is not None:
            raise RuntimeError(f'Failed to create Uriel engine {self.qualified_name}: {self.error}')

