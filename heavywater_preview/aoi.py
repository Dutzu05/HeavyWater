from __future__ import annotations

from pyproj import Geod, Transformer
from shapely.geometry import Polygon, box

from heavywater_preview.config import EUHYDRO_CRS, WGS84_CRS


def build_bbox(lat: float, lon: float, size_km: float) -> tuple[float, float, float, float]:
    geod = Geod(ellps="WGS84")
    half_size_m = (size_km * 1000.0) / 2.0
    east_lon, _, _ = geod.fwd(lon, lat, 90, half_size_m)
    west_lon, _, _ = geod.fwd(lon, lat, 270, half_size_m)
    _, north_lat, _ = geod.fwd(lon, lat, 0, half_size_m)
    _, south_lat, _ = geod.fwd(lon, lat, 180, half_size_m)
    return (west_lon, south_lat, east_lon, north_lat)


def bbox_polygon_wgs84(bbox_wgs84: tuple[float, float, float, float]) -> Polygon:
    return box(*bbox_wgs84)


def reproject_bounds_to_euhydro(bbox_wgs84: tuple[float, float, float, float]) -> Polygon:
    transformer = Transformer.from_crs(WGS84_CRS, EUHYDRO_CRS, always_xy=True)
    minx, miny = transformer.transform(bbox_wgs84[0], bbox_wgs84[1])
    maxx, maxy = transformer.transform(bbox_wgs84[2], bbox_wgs84[3])
    return box(min(minx, maxx), min(miny, maxy), max(minx, maxx), max(miny, maxy))
