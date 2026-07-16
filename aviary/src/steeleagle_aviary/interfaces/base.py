from abc import ABC, abstractmethod
from steeleagle_aviary.vehicle import Vehicle

class Interface(ABC):
    """Interface to bridge a drone OS like ROS or SteelEagle with Aviary.
    """
    def __init__(self, vehicle: Vehicle, **kwargs):
        self.vehicle = vehicle
        self.kwargs = kwargs

    @abstractmethod
    def start(self):
        """Run the interface.
        """
        pass
