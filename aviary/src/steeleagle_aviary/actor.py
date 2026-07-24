from queue import Queue
import pymap3d as pm
from panda3d.core import LVector3f, LPoint3f, NodePath
# Utility imports
from steeleagle_aviary.datatypes import GeodeticPoint

"""Defines an actor that moves through the simulated world.

Defines an actor object that can set its position within the
simulated world, and provides methods to convert geodetic
points into simulated space and vice-versa. Usually actors
are automatically controlled by passing in a set of waypoints.
"""

# Default speed for waypoint transit
DEFAULT_SPEED = 1.0

class Actor:
    def __init__(self,
                 name: str,
                 tag: str,
                 origin: GeodeticPoint,
                 anchor: GeodeticPoint,
                 obj: NodePath,
                 waypoints: list[dict[str, float]],
                 **kwargs
                 ):
        self.name = name
        self.tag = tag
        self.origin = origin
        self.anchor = anchor
        self.sim_origin = self.get_sim_origin(origin)
        self.object = obj
        self.waypoint_cycling = False

        for k,v in kwargs.items():
            setattr(self, k, v)

        self.waypoints = Queue()
        self.wp_delays = Queue()
        self.active_delay = 0.0
        for w in waypoints:
            if 'speed' not in w:
                w['speed'] = DEFAULT_SPEED
            if 'alt' not in w:
                w['alt'] = 0
            gp = GeodeticPoint(w['lat'], w['lon'], w['alt'])
            if 'delay' in w:
                print(f"delay recognized {w['delay']}")
                self.wp_delays.put(float(w['delay']))
            else:
                self.wp_delays.put(0.0)
            self.waypoints.put((self.convert_to_sim(gp), w['speed']))

        if 'waypoint_cycle' in kwargs:
            self.waypoint_cycling = True

        # Move the object into position
        self.object.setPos(*self.sim_origin)
        self.object.setHpr(0, 0, 0)

        # Position target
        self.position_target = self.sim_origin

    def get_sim_origin(self, origin: GeodeticPoint) -> LPoint3f:
        """Gets the simulation origin from starting position.

        Based on a starting geodetic point (origin) and a simulation
        anchor position, calculate the simulation starting position
        for the actor.

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

        Gets the actor's current simulator position in ENU coordinate
        space (east, north, up).

        Returns:
            LPoint3f: current simulation world position
        """
        return self.object.getPos()

    def position_reached(self) -> bool:
        """Check if target position is equal to current position.

        Returns:
            bool: true if target is reached, false otherwise
        """
        return self.current_position() == self.position_target

    def set_position_target(self, point: LPoint3f, speed: float):
        """Set a position target.

        Set the position target for the actor to move towards.

        Args:
            point (LPoint3f): target simulation world point
            speed (float): target speed at which to move
        """
        self.position_target = point
        self.speed_target = speed

    def get_velocity(self, dt: float) -> LVector3f:
        """Get current velocity.

        Gets the current velocity vector scaled by the time delta.

        Args:
            dt (float): delta time in seconds since last tick

        Returns:
            LVector3f: scaled velocity vector
        """
        move = LVector3f(0, 0, 0)
        current = self.current_position()
        diff = self.position_target - current
        move += diff
        if move.length() > 0:
            move.normalize()
            move *= self.speed_target * dt
        if move.length() > diff.length():
            move *= (diff.length() / move.length())
        return move

    def move(self, dt: float):
        """Move based on current velocity.

        Moves the actor based on a time delta scaled velocity.

        Args:
            dt (float): delta time in seconds since last tick
        """
        move = self.get_velocity(dt)
        pos = self.current_position()
        self.object.setPos(pos + move)

        if self.position_reached():
            if self.active_delay > 0:
                self.active_delay -= dt
                return
            self.cycle_next_waypoint() if self.waypoint_cycling else self.set_next_waypoint()

    def set_next_waypoint(self):
        """Sets the next waypoint for the actor.

        Attempts to set the actor target position to the next waypoint by consuming
        a waypoint from the front of the waypoint queue. For cycled traces intended
        to run indefinitely use cycle_next_waypoint. If no waypoint queue exists for
        the actor, the target position is set to the current position.
        """
        if self.waypoints is not None and not self.waypoints.empty():
            self.set_position_target(*self.waypoints.get())
            self.active_delay = self.wp_delays.get()

    def cycle_next_waypoint(self):
        """Sets the next waypoint for the actor and sets the current to the back of the queue.

        Attempts to set the actor target position to the next waypoint after placing
        the current waypoint back on the actor's waypoint queue. If no waypoint queue
        exists, the target position is set to the current position. For traces intended
        to run the path a single time, use set_next_waypoint instead.
        """
        if self.waypoints is not None and not self.waypoints.empty():
            self.waypoints.put((self.position_target, self.speed_target))
            self.set_position_target(*self.waypoints.get())

    def has_waypoints_remaining(self):
        """Checks if waypoints are remaining.
        """
        return self.waypoints and not self.waypoints.empty()
