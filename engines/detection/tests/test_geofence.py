import pytest

from detection.geofence import Geofence, GeofenceError

KML_HEADER = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>fence</name>"""
KML_FOOTER = "</Document></kml>"


def placemark(outer, inner=None):
    holes = (
        f"<innerBoundaryIs><LinearRing><coordinates>{inner}</coordinates></LinearRing></innerBoundaryIs>"
        if inner
        else ""
    )
    return (
        "<Placemark><Polygon><outerBoundaryIs><LinearRing>"
        f"<coordinates>{outer}</coordinates>"
        f"</LinearRing></outerBoundaryIs>{holes}</Polygon></Placemark>"
    )


# KML orders points lon,lat,alt. This square spans lat 40..41, lon -80..-79.
SQUARE = "-80,40,0 -79,40,0 -79,41,0 -80,41,0 -80,40,0"
HOLE = "-79.6,40.4,0 -79.4,40.4,0 -79.4,40.6,0 -79.6,40.6,0 -79.6,40.4,0"
FAR_SQUARE = "10,10,0 11,10,0 11,11,0 10,11,0 10,10,0"


def write_kml(tmp_path, *placemarks, name="fence.kml"):
    path = tmp_path / name
    path.write_text(KML_HEADER + "".join(placemarks) + KML_FOOTER)
    return path


def test_point_inside_the_polygon_is_contained(tmp_path):
    fence = Geofence.from_kml(write_kml(tmp_path, placemark(SQUARE)))
    assert fence.contains(40.5, -79.5)


def test_point_outside_the_polygon_is_not_contained(tmp_path):
    fence = Geofence.from_kml(write_kml(tmp_path, placemark(SQUARE)))
    assert not fence.contains(42.0, -79.5)


def test_kml_coordinates_are_longitude_first(tmp_path):
    fence = Geofence.from_kml(write_kml(tmp_path, placemark(SQUARE)))
    # Swapped lat and lon would put this point at lat -79.5, far from the square.
    assert fence.contains(lat=40.5, lon=-79.5)
    assert not fence.contains(lat=-79.5, lon=40.5)


def test_point_in_any_of_several_polygons_is_contained(tmp_path):
    fence = Geofence.from_kml(
        write_kml(tmp_path, placemark(SQUARE), placemark(FAR_SQUARE))
    )
    assert fence.contains(10.5, 10.5)
    assert fence.contains(40.5, -79.5)
    assert not fence.contains(25.0, 25.0)


def test_point_in_an_inner_boundary_is_not_contained(tmp_path):
    fence = Geofence.from_kml(write_kml(tmp_path, placemark(SQUARE, inner=HOLE)))
    assert not fence.contains(40.5, -79.5)
    assert fence.contains(40.9, -79.9)


def test_polygons_inside_folders_are_found(tmp_path):
    folder = "<Folder>" + placemark(SQUARE) + "</Folder>"
    fence = Geofence.from_kml(write_kml(tmp_path, folder))
    assert fence.contains(40.5, -79.5)


def test_kml_without_a_namespace_is_read(tmp_path):
    path = tmp_path / "plain.kml"
    path.write_text(f"<kml><Document>{placemark(SQUARE)}</Document></kml>")
    assert Geofence.from_kml(path).contains(40.5, -79.5)


def test_a_missing_file_is_an_error(tmp_path):
    with pytest.raises(GeofenceError, match="missing.kml"):
        Geofence.from_kml(tmp_path / "missing.kml")


def test_malformed_xml_is_an_error(tmp_path):
    path = tmp_path / "broken.kml"
    path.write_text("<kml><Document>")
    with pytest.raises(GeofenceError, match="broken.kml"):
        Geofence.from_kml(path)


def test_kml_without_a_polygon_is_an_error(tmp_path):
    path = write_kml(
        tmp_path,
        "<Placemark><Point><coordinates>-79,40,0</coordinates></Point></Placemark>",
    )
    with pytest.raises(GeofenceError, match="no polygon"):
        Geofence.from_kml(path)
