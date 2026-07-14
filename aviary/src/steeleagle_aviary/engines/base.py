from abc import ABC, abstractmethod
from vehicle import Vehicle
from actor import Actor

class Engine(ABC):

    @abstractmethod
    def get_name(self):
        """Returns the name of the engine."""
        pass

    @abstractmethod
    def get_result(self, vehicle: Vehicle, actors: list[Actor]) -> dict:
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

class EngineHolder:
    def __init__(self, vehicle: Vehicle, actors: list[Actor], sinks: list[Engine]):
        self.vehicle = vehicle
        self.actors = actors
        self.sinks = sinks

    def get_results(self) -> dict:
        """Builds a dictionary of results.

        Synthesizes results from all attached engines into one queryable
        dictionary that can be parsed and shipped by an interface.

        Returns:
            dict: synthesized result dictionaries
        """
        results = {}
        for sink in self.sinks:
            results[sink.get_name()] = sink.get_result(self.vehicle, self.actors)
        return results

def get_engine_from_name(name: str, **kwargs) -> Engine:
    """Get the associated engine from a name.

    Builds a dynamic engine object from a given name. The name is matched
    to a file in the engine folder, and an import of an object of the same 
    name as the file is attempted.

    Args:
        name (str): name of the engine

    Returns:
        Engine: engine object

    Raises:
        ValueError: if name cannot be found or the import fails
    """
    import importlib
    
    try:
        # Capitalizes letters and removed dashes and underscores
        name = name.title().replace('-', '').replace('_', '')
        module = importlib.import_module(f'engines.{name}')
        return getattr(module, name)(**kwargs)
    except:
        raise ValueError(f'Could not find engine with name {name}!')
