from abc import ABC, abstractmethod
from steeleagle_aviary.vehicle import Vehicle

class Driver(ABC):
    """Driver to bridge with SteelEagle.
    """
    def __init__(self, vehicle: Vehicle, **kwargs):
        self.vehicle = vehicle
        self.kwargs = kwargs

    @abstractmethod
    def start(self):
        """Run the interface.
        """
        pass
