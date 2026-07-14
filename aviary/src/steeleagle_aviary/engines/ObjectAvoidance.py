from panda3d.core import TransformState
from engines.base import Engine
import math
import numpy as np
import cv2
import time
 
class ObjectAvoidance(Engine):
    def __init__(self, **kwargs):
        self.range = kwargs.get('range', 10.0)
        self.drop_range = 10.0
        # TODO - replace these with parrot defaults
        self.frame_width = kwargs.get('frame_width', 640)
        self.frame_height = kwargs.get('frame_height', 480)
 
    def get_name(self):
        return 'obstacle-engine'
 
    def get_result(self, vehicle, actors):
        frame_width = self.frame_width
        frame_height = self.frame_height
        COLLISION_FACTOR = 0.25

        raw_depth = np.ones((frame_height, frame_width), dtype=np.uint8)
        pose = vehicle.current_pose()
        pose.x = round(pose.x) 
        vehicle.camera.camera.setHpr(pose)
        proj_mat = vehicle.camera.camera.node().getLens().getProjectionMatInv()
        vehicle.camera.proj.setTransform(TransformState.makeMat(proj_mat))
 
        # grab values for horizontal fov for normalizing to [-1, 1]
        hfov_deg = vehicle.camera.camera.node().getLens().getHfov()
        half_hfov_rad = math.radians(hfov_deg / 2.0)
 
        visible = []
        steering_accum = 0.0
        for act in actors:
            # get exact sim distance from actor
            acp = act.current_position()
            vcp = vehicle.current_position()
            acp.z = 0
            vcp.z = 0
            distance = (acp - vcp).length()
            if distance <= COLLISION_FACTOR:
                print("collision")
                with open("failure_marker.txt", "a") as f:
                    f.write(f"fail: {time.time()}\n")
            if distance > self.range or distance > self.drop_range or distance <= 0:
                continue

            # verify that actor is in front of the actual camera viewport
            relative_location = vehicle.camera.camera.getRelativePoint(vehicle.camera.camera.getParent(), act.current_position())
            if not vehicle.camera.camera.node().isInView(relative_location):
                continue
            # get actor values relative to frame norm/pixel position
            mins, maxes = act.object.getTightBounds(vehicle.camera.proj)
            x_min_n = min(1.0, max(0.5 + (mins.x / 2), 0.0))
            x_max_n = min(1.0, max(0.5 + (maxes.x / 2), 0.0))
            y_min_n = min(1.0, max(0.5 - (maxes.y / 2), 0.0))
            y_max_n = min(1.0, max(0.5 - (mins.y / 2), 0.0))
            x_min_px = int(x_min_n * frame_width)
            x_max_px = int(x_max_n * frame_width)
            y_min_px = int(y_min_n * frame_height)
            y_max_px = int(y_max_n * frame_height)
 
            if x_max_px > x_min_px and y_max_px > y_min_px:
                # use inverse depth similarly to midas
                depth_value = int(round(255 * (1.0 - distance / self.range)))
                depth_value = max(2, min(255, depth_value))
                visible.append({
                    'bbox': (x_min_px, y_min_px, x_max_px, y_max_px),
                    'depth_value': depth_value,
                    'distance': distance,
                })
 
            # actor alongside or behind the camera plane
            if relative_location.y <= 0:
                continue
 
            # out of range in the lateral plane
            horizontal_distance = math.sqrt(relative_location.x ** 2 + relative_location.y ** 2)
            if horizontal_distance > self.range:
                continue
 
            # positive if actor on right, negative on left
            lateral_angle = math.atan2(relative_location.x, relative_location.y)
 
            # normalize to [-1, 1] across the horizontal field of view.
            normalized_lateral = lateral_angle / half_hfov_rad
 
            # prox weight is 1 at zero distance, 0 at max range, squared to weight by prox
            proximity = 1.0 - ((horizontal_distance - COLLISION_FACTOR) / self.range)
            weight = proximity * proximity
 
            # actuation vector is opposite the direction of the action (flipped sign)
            steering_accum += -weight * normalized_lateral
 
        # update raw depth map in descending distance order, overwrites simply occlude further objs
        visible.sort(key=lambda a: -a['distance'])
        for actor in visible:
            x1, y1, x2, y2 = actor['bbox']
            region = raw_depth[y1:y2, x1:x2]
            np.maximum(region, actor['depth_value'], out=region)

        full_depth_map = cv2.applyColorMap(raw_depth, cv2.COLORMAP_OCEAN)
        actuation_vector = max(-1.0, min(1.0, steering_accum))
 
        cv2.putText(
            full_depth_map,
            f"steer: {actuation_vector:+.4f}",
            (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (0, 255, 0),
            2,
        )
        return actuation_vector, full_depth_map
