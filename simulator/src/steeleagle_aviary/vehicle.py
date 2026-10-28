import pymap3d as pm
from panda3d.core import LVector3f, LPoint3f, NodePath
from math import sin, cos, radians, isclose
from collections import deque
# Utility imports
from steeleagle_aviary.util import convert_angle_heading, calculate_bearing
from steeleagle_aviary.datatypes import GeodeticPoint, Mode, PoseMode
from steeleagle_aviary.datatypes import CameraHolder, GroundHolder

"""Defines a vehicle that moves through the simulated world.

Defines a vehicle object that can set its velocity or position
within the simulated world, and provides methods to convert
geodetic points into simulated space and vice-versa. In order
for the vehicle to be controlled, it must be attached to an
interface.
"""

# Default transit speed
DEFAULT_SPEED = 3.0
# Default angular speed for pose adjustment
DEFAULT_ANGULAR_SPEED = 90.0

class Vehicle:
    def __init__(self,
                 name: str,
                 origin: GeodeticPoint,
                 anchor: GeodeticPoint,
                 camera: CameraHolder,
                 ground: GroundHolder
                 ):
        self.name = name
        self.camera = camera
        self.ground = ground
        self.result_map = {}

        # Origins
        self.origin = origin
        self.anchor = anchor
        self.sim_origin = self.get_sim_origin(origin)

        # Move the camera and ground plane
        self.camera.camera.setPos(*self.sim_origin)
        self.camera.camera.setHpr(0, 0, 0)
        self.ground.ground.setPos(self.sim_origin.x, self.sim_origin.y, self.sim_origin.z - 0.1)

        # Target positions
        self.mode = Mode.POSITION
        self.position_target = self.sim_origin
        self.velocity_target = LPoint3f(0.0, 0.0, 0.0) # x_vel, y_vel, z_vel
        # The vehicle's heading and the gimbal's pitch and roll are targeted
        # separately, so that e.g. the vehicle can yaw at a rate while the
        # gimbal pitches at another. Both are in raw simulator angles.
        self.heading_mode = PoseMode.ANGLE
        self.heading_target = 0.0
        self.gimbal_mode = PoseMode.ANGLE
        self.gimbal_target = LVector3f(0.0, 0.0, 0.0) # unused, pitch, roll
        self.speed_target = DEFAULT_SPEED
        self.waypoints = deque()

    def get_sim_origin(self, origin: GeodeticPoint) -> LPoint3f:
        """Gets the simulation origin from starting position.

        Based on a starting geodetic point (origin) and a simulation
        anchor position, calculate the simulation starting position
        for the vehicle.

        Args:
            origin (GeodeticPoint): origin geodetic point

        Returns:
            LPoint3f: converted simulation world point
        """
        x, y, z = pm.geodetic2enu(
                origin.latitude,
                origin.longitude,
                origin.altitude,
                self.anchor.latitude,
                self.anchor.longitude,
                self.anchor.altitude,
                deg=True
                )
        return LPoint3f(x, y, z)

    def convert_to_geodetic(self, point: LPoint3f) -> GeodeticPoint:
        """Convert a simulator point to a geodetic point.

        Takes a simulator point and converts it into geodetic
        space. Altitude in the simulation is tracked independently
        of coordinate conversion, since altitude will throw off
        position estimation.

        Args:
            point (LPoint3f): input simulation world point

        Returns:
            GeodeticPoint: converted geodetic space point
        """
        lat, lng, _ = pm.enu2geodetic(
                point.x,
                point.y,
                self.sim_origin.z,
                self.anchor.latitude,
                self.anchor.longitude,
                self.anchor.altitude,
                deg=True
                )
        return GeodeticPoint(lat.item(), lng.item(), point.z)

    def convert_to_sim(self, geod: GeodeticPoint) -> LPoint3f:
        """Convert a geodetic point to a simulator point.

        Takes a geodetic point and converts it into simulator (ENU)
        space. Altitude in the simulation is tracked independently
        of coordinate conversion, since altitude will throw off
        position estimation.

        Args:
            geod (GeodeticPoint): input geodetic point

        Returns:
            LPoint3f: converted simulation world point
        """
        x, y, _ = pm.geodetic2enu(
                geod.latitude,
                geod.longitude,
                geod.altitude,
                self.anchor.latitude,
                self.anchor.longitude,
                self.anchor.altitude,
                deg=True
                )
        return LPoint3f(x, y, geod.altitude)

    def current_position(self) -> LPoint3f:
        """Get current simulator position.

        Gets the vehicle's current simulator position in ENU coordinate
        space (east, north, up).

        Returns:
            LPoint3f: current simulation world position
        """
        return self.camera.camera.getPos()

    def current_geodetic_position(self) -> GeodeticPoint:
        """Get current geodetic position.

        Gets the vehicle's current position in geodetic coordinate space
        (latitude, longitude, altitude).

        Returns:
            GeodeticPoint: current geodetic position
        """
        return self.convert_to_geodetic(self.current_position())

    def position_reached(self) -> bool:
        """Check if target position is equal to current position.

        Returns:
            bool: true if target is reached, false otherwise
        """
        return self.current_position() == self.position_target

    def current_pose(self) -> LVector3f:
        """Get angle-to-heading corrected camera pose.

        Returns:
            LVector3f: angle-to-heading corrected camera pose
                (heading, pitch, roll) in degrees
        """
        rot = self.current_rotation()
        return LVector3f(convert_angle_heading(rot.x), rot.y, rot.z)

    def current_pose_body(self) -> LVector3f:
        """Get camera body-relative pose.

        Returns:
            LVector3f: camera body-relative pose
                (heading, pitch, roll) in degrees
        """
        rot = self.current_rotation()
        return LVector3f(0.0, rot.y, rot.z)

    def current_rotation(self) -> LVector3f:
        """Raw camera pose.

        Returns the raw simulator camera pose without angle to heading
        correction. These differ since heading rotates clockwise but
        angles rotate counter-clockwise.

        Returns:
            LVector3f: raw camera pose (heading, pitch, roll) in degrees
        """
        return self.camera.camera.getHpr()

    def pose_reached(self) -> bool:
        """Check if target pose is equal to current pose.

        Checks element-by-element through the pose vector to
        verify equality.

        Returns:
            bool: true if pose reached, false otherwise
        """
        rotation = self.camera.camera.getHpr()
        return self.heading_reached() and \
                self.angle_reached(rotation.y, self.gimbal_target.y) and \
                self.angle_reached(rotation.z, self.gimbal_target.z)

    def heading_reached(self) -> bool:
        """Checks if heading component of target pose is equal to current pose heading.

        Returns:
            bool: true if heading reached, false otherwise
        """
        return self.angle_reached(self.camera.camera.getH(), self.heading_target)

    def angle_reached(self, a1: float, a2: float) -> bool:
        """Checks if two angles are equal.

        Checks if two angles are equal irrespective of sign. This is
        necessary because negative angles are not considered equal by
        default to positive angles.

        Returns:
            bool: true if angles are logically equivalent, false otherwise
        """
        return isclose(a1, a2, abs_tol=1e-3) or \
                isclose(a1, -(360.0 - a2), abs_tol=1e-3)

    def set_waypoint_target(self, waypoints: list[LPoint3f], speed=DEFAULT_SPEED):
        """Set a waypoint target.

        Set a list of waypoints (in sim coordinates) for the vehicle to move through.
        The vehicle will visit each waypoint in order and stop once all waypoints are
        visited.

        Args:
            waypoints (list[LPoint3f]): list of waypoints in sim coordinates
        """
        self.waypoints.clear()
        if not len(waypoints):
            return
        for wp in waypoints:
            self.waypoints.append(wp)
        self.speed_target = speed
        # set_position_target() sets self.mode = Mode.POSITION, so WAYPOINT
        # must be (re-)applied after calling it.
        self.set_position_target(self.waypoints[0], speed=speed)
        self.mode = Mode.WAYPOINT

    def set_geodetic_waypoint_target(self, waypoints: list[GeodeticPoint], speed=DEFAULT_SPEED):
        """Set a geodetic waypoint target.

        Set a list of waypoints (in geodetic coordinates) for the vehicle to move through.
        The vehicle will visit each waypoint in order and stop once all waypoints are
        visited.

        Args:
            waypoints (list[LPoint3f]): list of waypoints in geodetic coordinates
        """
        self.waypoints.clear()
        if not len(waypoints):
            return
        for wp in waypoints:
            self.waypoints.append(self.convert_to_sim(wp))
        self.speed_target = speed
        # set_position_target() sets self.mode = Mode.POSITION, so WAYPOINT
        # must be (re-)applied after calling it.
        self.set_position_target(self.waypoints[0], speed=speed)
        self.mode = Mode.WAYPOINT

    def set_position_target(self, point: LPoint3f, speed=DEFAULT_SPEED):
        """Set a position target.

        Set the position target for the vehicle to move towards.

        Args:
            point (LPoint3f): target simulation world point
        """
        self.speed_target = speed
        self.position_target = point
        self.mode = Mode.POSITION

    def set_relative_position_target(self, vector: LVector3f, speed=DEFAULT_SPEED, body_aligned=False):
        """Set a relative position target.

        Set an offset position target relative to the current position
        for the vehicle to move towards.

        Args:
            vector (LVector3f): offset vector
            body_algined (bool): whether or not to align the offset to
                the current pose, default to `False`
        """
        if not body_aligned:
            self.set_position_target(self.current_position() + vector, speed=speed)
        else:
            theta = radians(self.current_rotation().x)
            forward = LVector3f(-sin(theta), cos(theta), 0)
            forward.normalize()
            forward *= vector.x
            right = LVector3f(cos(theta), sin(theta), 0)
            right.normalize()
            right *= vector.y
            up = LVector3f(0, 0, vector.z)
            self.set_position_target(self.current_position() + forward + right + up, speed=speed)

    def set_velocity_target(self, vector: LVector3f, body_aligned=False):
        """Set a velocity target.

        Set the velocity target for the vehicle to move at, either with
        a body or global frame of reference.

        Args:
            vector (LVector3f): target velocity
            body_algined (bool): whether or not to align the velocity to
                the current pose, default to `False`
        """
        if not body_aligned:
            self.velocity_target = vector
        else:
            theta = radians(self.current_rotation().x)
            forward = LVector3f(-sin(theta), cos(theta), 0)
            forward.normalize()
            forward *= vector.x
            right = LVector3f(cos(theta), sin(theta), 0)
            right.normalize()
            right *= vector.y
            up = LVector3f(0, 0, 1) * vector.z
            self.velocity_target = forward + right + up
        self.mode = Mode.VELOCITY

    def set_heading_target(self, heading: float, mode: PoseMode):
        """Set a heading target for the vehicle.

        Args:
            heading (float): target heading, heading offset, or yaw rate
                in degrees (per second)
            mode (PoseMode): pose mode
        """
        if mode == PoseMode.ANGLE:
            self.heading_target = convert_angle_heading(heading)
        elif mode == PoseMode.OFFSET:
            self.heading_target = convert_angle_heading(self.current_pose().x + heading)
        else:
            self.heading_target = -heading
        self.heading_mode = mode

    def set_gimbal_target(self, pitch: float, roll: float, mode: PoseMode):
        """Set a pitch and roll target for the gimbal.

        Args:
            pitch (float): target pitch, pitch offset, or pitch rate in
                degrees (per second)
            roll (float): target roll, roll offset, or roll rate in
                degrees (per second)
            mode (PoseMode): pose mode
        """
        target = LVector3f(0, pitch, roll)
        if mode == PoseMode.OFFSET:
            rot = self.current_rotation()
            target += LVector3f(0, rot.y, rot.z)
        self.gimbal_target = target
        self.gimbal_mode = mode

    def heading_setpoint(self) -> float:
        """Get the heading the vehicle is turning to.

        Returns:
            float: target heading in degrees, or the current heading
                if the vehicle is turning at a rate
        """
        if self.heading_mode == PoseMode.VELOCITY:
            return self.current_pose().x
        return convert_angle_heading(self.heading_target)

    def get_velocity(self, dt: float) -> LVector3f:
        """Get current velocity.

        Gets the current velocity vector scaled by the time delta.

        Args:
            dt (float): delta time in seconds since last tick

        Returns:
            LVector3f: scaled velocity vector
        """
        move = LVector3f(0, 0, 0)
        if self.mode == Mode.POSITION:
            current = self.current_position()
            diff = self.position_target - current
            move += diff
            if move.length() > 0:
                move.normalize()
                move *= self.speed_target * dt
            if move.length() > diff.length():
                move *= (diff.length() / move.length())
        elif self.mode == Mode.VELOCITY:
            move = self.velocity_target * dt
        else: # WAYPOINT mode
            current = self.current_position()
            diff = self.position_target - current
            if diff.length() == 0:
                if not self.waypoints:
                    # The final waypoint has been reached; nothing left to do.
                    return move
                # set_position_target() sets self.mode = Mode.POSITION, so
                # WAYPOINT must be (re-)applied after calling it, otherwise
                # any waypoints after this one are never visited.
                self.set_position_target(self.waypoints.popleft(), speed=self.speed_target)
                self.mode = Mode.WAYPOINT
            else:
                move += diff
                if move.length() > 0:
                    move.normalize()
                    move *= self.speed_target * dt
                if move.length() > diff.length():
                    move *= (diff.length() / move.length())
        return move

    def get_velocity_body(self, dt: float) -> LVector3f:
        """Get current body velocity.

        Gets the current velocity vector scaled by the time delta relative
        to the body pose of the vehicle.

        Args:
            dt (float): delta time in seconds since last tick

        Returns:
            LVector3f: scaled velocity vector
        """
        return self.to_body(self.get_velocity(dt))

    def to_body(self, velocity: LVector3f) -> LVector3f:
        """Convert a simulator (ENU) velocity to body velocity.

        Args:
            velocity (LVector3f): velocity in simulator space

        Returns:
            LVector3f: velocity as (forward, right, up)
        """
        theta = radians(self.current_rotation().x)
        vecf = LVector3f(-sin(theta), cos(theta), 0)
        vecf.normalize()
        forward = velocity.dot(vecf)
        rvec = LVector3f(cos(theta), sin(theta), 0)
        rvec.normalize()
        right = velocity.dot(rvec)
        up = velocity.z
        return LVector3f(forward, right, up)

    def get_angular_velocity(self, dt: float) -> LVector3f:
        """Get current angular velocity.

        Gets the current angular velocity vector scaled by the time delta.

        Args:
            dt (float): delta time in seconds since last tick

        Returns:
            LVector3f: scaled angular velocity vector
        """
        current = self.current_rotation()
        # OFFSET stores an absolute target (current + offset, computed once
        # when the target is set), so it converges the same way ANGLE does
        # rather than being treated as a rate like VELOCITY mode.
        if self.heading_mode == PoseMode.VELOCITY:
            heading = self.heading_target * dt
        else:
            diff = self.heading_target - current.x
            if diff >= 180.0:
                diff -= 360.0 # Rotate in the opposite direction if it's faster
            if diff <= -180.0:
                diff += 360.0
            step = DEFAULT_ANGULAR_SPEED * dt
            heading = max(-step, min(step, diff))

        if self.gimbal_mode == PoseMode.VELOCITY:
            gimbal = self.gimbal_target * dt
        else:
            diff = LVector3f(0, self.gimbal_target.y - current.y, self.gimbal_target.z - current.z)
            gimbal = LVector3f(diff)
            if gimbal.length() > 0:
                gimbal.normalize()
                gimbal *= DEFAULT_ANGULAR_SPEED * dt
            if gimbal.length() > diff.length():
                gimbal = diff
        return LVector3f(heading, gimbal.y, gimbal.z)

    def move(self, dt):
        """Move based on current velocity.

        Moves the vehicle based on a time delta scaled velocity.

        Args:
            dt (float): delta time in seconds since last tick
        """
        move = self.get_velocity(dt)
        pos = self.current_position()
        gimb = self.current_rotation()
        self.camera.camera.setPos(pos + move)

        # To simulate a moving ground underneath the vehicle,
        # the ground plane is moved with it and the UV of the
        # grass texture is offset by the vehicle position.
        self.ground.ground.setTexOffset(self.ground.texture_stage, (pos.x / 10), (pos.y / 10))
        self.ground.ground.setPos(pos.x + move.x, pos.y + move.y, self.sim_origin.z - 0.1)

        rot = self.get_angular_velocity(dt)
        self.camera.camera.setHpr(gimb + rot)
