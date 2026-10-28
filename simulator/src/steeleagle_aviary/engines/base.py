from abc import ABC, abstractmethod
from steeleagle_aviary.vehicle import Vehicle
from steeleagle_aviary.actor import Actor

class Engine(ABC):
    """Synthetic cognitive engine for producing deterministic AI results
    in Aviary.
    """
    def __init__(self, name: str, vehicles: dict[str, Vehicle], actors: dict[str, Actor], **kwargs):
        self.name = name
        self.vehicles = vehicles
        self.actors = actors
        self.kwargs = kwargs

    def get_name(self) -> str:
        """Returns the name of the engine."""
        return self.name

    @abstractmethod
    def start(self):
        """Run the interface.
        """
        pass
