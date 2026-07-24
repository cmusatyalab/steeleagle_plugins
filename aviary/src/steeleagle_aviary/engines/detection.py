import time
# Panda3D imports
from panda3d.core import TransformState
# Base import
from steeleagle_aviary.engines.base import Engine

"""Mock object detection engine."""

class ObjectEngine(Engine):
    def __init__(self, **kwargs):
        self.range = kwargs.get('range', 20.0)

    def get_name(self):
        return 'object-engine'

    def inference(self, vehicle, actors):
        # Create a projection matrix for the camera view
        proj_mat = vehicle.camera.camera.node().getLens().getProjectionMatInv()
        vehicle.camera.proj.setTransform(TransformState.makeMat(proj_mat))
        # Iterate through actors and find bounding boxes
        result = []
        for _, act in actors.items():
            if (act.current_position() - vehicle.current_position()).length() <= self.range:
                relative_location = vehicle.camera.camera.getRelativePoint(vehicle.camera.camera.getParent(), act.current_position())
                # Cull detection if it is not in the camera's viewport
                if not vehicle.camera.camera.node().isInView(relative_location):
                    continue
                mins, maxes = act.object.getTightBounds(vehicle.camera.proj)
                lr_bound = [(lambda x: min(1.0, max(0.5 + (x / 2), 0.0)))(x) for x in [mins.x, maxes.x]]
                td_bound = [(lambda x: min(1.0, max(0.5 - (x / 2), 0.0)))(x) for x in [mins.y, maxes.y]]
                result.append({'className': act.tag, 'score': 100, 'bbox': {'xMin': lr_bound[0], 'yMin': td_bound[1], 'xMax': lr_bound[1], 'yMax': td_bound[0]}})
        return result
