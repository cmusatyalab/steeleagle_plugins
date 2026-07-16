from abc import ABC, abstractmethod
from steeleagle_aviary.vehicle import Vehicle
from steeleagle_aviary.actor import Actor

class Engine(ABC):
    """Synthetic cognitive engine for producing deterministic AI results
    in Aviary.
    """
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    @abstractmethod
    def get_name(self) -> str:
        """Returns the name of the engine."""
        pass

    @abstractmethod
    def inference(self, vehicle: Vehicle, actors: dict[str, Actor]) -> dict:
        """Return dictionary-formatted result.

        Builds and returns a dictionary-formatted result based
        on the vehicle's current telemetry and camera pose.

        Args:
            vehicle (Vehicle): vehicle to generate results from
            actors (list[Actor]): actors within the simulation

        Returns:
            dict: result dictionary
        """
        pass
