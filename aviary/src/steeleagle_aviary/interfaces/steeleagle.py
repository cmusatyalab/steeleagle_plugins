import grpc
from concurrent import futures
# Simulator imports
from steeleagle_aviary.vehicle import Vehicle
# Panda3d imports
from panda3d.core import LVector3f, LPoint3f
# Utility imports
from steeleagle_aviary.util import calculate_bearing
from steeleagle_aviary.datatypes import GeodeticPoint, Mode, PoseMode
# Base import
from steeleagle_aviary.interfaces.base import Interface
# Protocol import
import steeleagle_protocol.v1.services.driver.control_pb2 as control_proto
from steeleagle_protocol.v1.services.driver.control_pb2_grpc import ControlServiceServicer, add_ControlServiceServicer_to_server

class SteelEagle(Interface):
    """Control interface wrapper.
    """
    def start(self):
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
        add_ControlServicer_to_server(Control(self.vehicle), self.server)
        self.server.add_insecure_port(address)
        self.server.start()

class Control(ControlServiceServicer):
    """gRPC control interface.
    """
    def __init__(self, vehicle: Vehicle):
        self.vehicle = vehicle

    def TakeOff(self, request, context):
        self.vehicle.set_position_target(self.vehicle.current_position() + LVector3f(0, 0, request.take_off_altitude))
        return control_proto.TakeOffResponse()

    def Land(self, request, context):
        current = self.vehicle.current_position()
        self.vehicle.set_position_target(LPoint3f(current.x, current.y, self.vehicle.sim_origin.z))
        return control_proto.LandResponse()

    def Hold(self, request, context):
        self.vehicle.set_joystick_target(LVector3f(0, 0, 0))
        self.vehicle.set_pose_target(LVector3f(0, 0, 0), PoseMode.VELOCITY)
        return control_proto.HoldResponse()

    def SetHome(self, request, context):
        self.vehicle.origin.latitude = request.location.latitude
        self.vehicle.origin.longitude = request.location.longitude
        return control_proto.SetHomeResponse()

    def ReturnToHome(self, request, context):
        self.vehicle.set_position_target(self.vehicle.sim_origin)
        return control_proto.ReturnToHomeResponse()

    def GoToRelativePosition(self, request, context):
        offset = LVector3f(request.x, request.y, request.z)
        pose = LPoint3f(request.angle, 0, 0)
        body_aligned = request.frame <= 1
        self.vehicle.set_relative_position_target(new_position, body_aligned=body_aligned)
        self.vehicle.set_pose_target(pose, mode=PoseMode.OFFSET)
        return control_proto.GoToRelativePositionResponse()

    def GoToGlobalPosition(self, request, context):
        # If absolute altitude is specified, add the target altitude
        # to the current altitude
        if request.altitude_mode <= 1:
            altitude = request.location.altitude - self.vehicle.sim_origin.z
        else:
            position = self.vehicle.current_position()
            altitude = position.z + request.location.altitude

        self.vehicle.set_position_target(self.vehicle.convert_to_sim(
                GeodeticPoint(
                    request.location.latitude,
                    request.location.longitude,
                    altitude
                    )
                ))

        # If heading mode is TO_TARGET, set the pose to look at the
        # position target
        if request.heading_mode <= 1:
            position = self.vehicle.current_geodetic_position()
            bearing = calculate_bearing(
                    position.latitude,
                    position.longitude,
                    request.location.latitude,
                    request.location.longitude
                    )
            self.vehicle.set_pose_target(LVector3f(bearing, 0, 0), PoseMode.ANGLE)

        return control_proto.GoToGlobalPositionResponse()

    def SetVelocity(self, request, context):
        self.vehicle.set_joystick_target(LVector3f(request.velocity.x_vel, request.velocity.y_vel, request.velocity.z_vel))
        self.vehicle.set_pose_target(LVector3f(request.velocity.angular_vel, 0, 0), PoseMode.VELOCITY)
        return control_proto.SetVelocityResponse()

    def SetGimbalPose(self, request, context):
        if request.pose_mode == 0:
            self.vehicle.set_pose_target(LVector3f(
                    request.pose.yaw,
                    max(-90, min(90, request.pose.pitch)),
                    request.pose.roll
                    ), PoseMode.ANGLE
                    )
        elif request.pose_mode == 1:
            self.vehicle.set_pose_target(LVector3f(
                    request.pose.yaw,
                    request.pose.pitch,
                    request.pose.roll
                    ),
                    PoseMode.OFFSET
                    )
        else:
            self.vehicle.set_pose_target(LVector3f(
                    request.pose.yaw,
                    request.pose.pitch,
                    request.pose.roll
                    ), PoseMode.VELOCITY
                    )

        return control_proto.SetGimbalPoseResponse()
