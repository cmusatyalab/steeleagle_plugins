import grpc
import logging
# Protocol imports
import steeleagle_protocol.v1.services.driver.calibrate_pb2 as calibrate_proto
from steeleagle_protocol.v1.services.driver.calibrate_pb2_grpc import CalibrateServiceServicer
# Olympe imports
from olympe.messages.gimbal import calibrate, calibration_state, calibration_result
from olympe.messages.common.Calibration import MagnetoCalibration
from olympe.messages.common.CalibrationState import (
    MagnetoCalibrationStateChanged,
    MagnetoCalibrationStartedChanged,
    MagnetoCalibrationAxisToCalibrateChanged,
)
from olympe.enums.gimbal import calibration_state as gimbal_state
from olympe.enums.gimbal import calibration_result as result_state

logger = logging.getLogger('parrot-anafi/calibrate')

class Calibrate(CalibrateServiceServicer):
    """Calibrate Service implementation.
    """
    def __init__(self, drone):
        self.drone = drone

    def magnetometer_calibrated(self):
        """Checks the magnetometer state for calibration.
        """
        state = self.drone.get_state(MagnetoCalibrationStateChanged)
        return all([
            state["xAxisCalibration"],
            state["yAxisCalibration"],
            state["zAxisCalibration"],
        ])

    def Calibrate(self, request, context):
        if request.sensor == 1: # Magnetometer
            self.drone(
                MagnetoCalibration(1)
                >> MagnetoCalibrationStartedChanged(started=1, _policy='wait')
            ).wait().success()
            while not self.magnetometer_calibrated():
                to_calibrate = self.drone.get_state(MagnetoCalibrationAxisToCalibrateChanged)['axis']
                if to_calibrate == 'xAxis':
                    yield calibrate_proto.CalibrateResponse(
                        next_instruction='Rotate the drone around its X (roll) axis!',
                        step=1, total=3,
                    )
                elif to_calibrate == 'yAxis':
                    yield calibrate_proto.CalibrateResponse(
                        next_instruction='Rotate the drone around its Y (pitch) axis!',
                        step=2, total=3,
                    )
                elif to_calibrate == 'zAxis':
                    yield calibrate_proto.CalibrateResponse(
                        next_instruction='Rotate the drone around its Z (yaw) axis!',
                        step=3, total=3,
                    )
                yield calibrate_proto.CalibrateResponse(
                    next_instruction='Magnetic calibration complete!',
                    step=3, total=3,
                    complete=True,
                )
        elif request.sensor == 2: # Gimbal
            self.drone(
                calibrate(request.id)
                >> calibration_state(state=gimbal_state.in_progress, _policy='wait')
                >> calibration_result(result=result_state.success, _policy='wait')
            ).wait().success()
            yield calibrate_proto.CalibrateResponse(step=1, total=1, complete=True)
        else:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                'no supported sensor ID provided',
            )
