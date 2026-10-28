"""A geofence read from a KML file, such as one exported from Google Maps."""

import xml.etree.ElementTree as ET
from pathlib import Path

from shapely.geometry import Point, Polygon

POLYGON = "Polygon"
OUTER_BOUNDARY = "outerBoundaryIs"
INNER_BOUNDARY = "innerBoundaryIs"
COORDINATES = "coordinates"


class GeofenceError(Exception):
    pass


def _local(element):
    """An element's tag without its XML namespace."""
    return element.tag.rpartition("}")[2]


def _ring(boundary, path):
    """The (lon, lat) points of the boundary's coordinates. KML orders each
    point lon,lat[,alt]."""
    text = next((e.text for e in boundary.iter() if _local(e) == COORDINATES), None)
    if not text:
        raise GeofenceError(f"{path}: a boundary has no coordinates")
    try:
        return [tuple(float(v) for v in point.split(",")[:2]) for point in text.split()]
    except ValueError as err:
        raise GeofenceError(f"{path}: bad coordinates: {err}") from err


class Geofence:
    """The union of the polygons of a KML file."""

    def __init__(self, polygons):
        self.polygons = polygons

    @classmethod
    def from_kml(cls, path):
        path = Path(path)
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError) as err:
            raise GeofenceError(f"cannot read {path}: {err}") from err

        polygons = []
        for element in root.iter():
            if _local(element) != POLYGON:
                continue
            shell = holes = None
            outer = [e for e in element if _local(e) == OUTER_BOUNDARY]
            inner = [e for e in element if _local(e) == INNER_BOUNDARY]
            if outer:
                shell = _ring(outer[0], path)
                holes = [_ring(e, path) for e in inner]
            try:
                polygons.append(Polygon(shell, holes))
            except (TypeError, ValueError) as err:
                raise GeofenceError(f"{path}: invalid polygon: {err}") from err
        if not polygons:
            raise GeofenceError(f"{path} has no polygon")
        return cls(polygons)

    def contains(self, lat, lon):
        """Whether the point is inside any polygon, or on its edge, and not in
        one of its holes."""
        point = Point(lon, lat)
        return any(polygon.covers(point) for polygon in self.polygons)
