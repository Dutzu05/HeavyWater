from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.errors import WindowError
from rasterio.features import shapes
from rasterio.mask import mask
from shapely.geometry import mapping, shape

from heavywater_preview.config import COMMUNITIES_LAYER, EUHYDRO_CRS, WGS84_CRS
from heavywater_preview.geom import project_geometry


def communities_from_impervious_raster(
    raster_path: Path | None,
    aoi_wgs84,
    threshold: float,
    min_area_m2: float,
) -> gpd.GeoDataFrame:
    if raster_path is None:
        return _empty_communities()

    raster_path = Path(raster_path)
    if not raster_path.exists():
        raise FileNotFoundError(f"Community raster not found: {raster_path}")

    with rasterio.open(raster_path) as src:
        try:
            aoi_in_raster_crs = project_geometry(aoi_wgs84, WGS84_CRS, src.crs)
            clipped_data, clipped_transform = mask(src, [mapping(aoi_in_raster_crs)], crop=True, indexes=1, filled=False)
        except (ValueError, WindowError) as exc:
            raise ValueError(f"The community raster does not overlap the requested AOI: {raster_path}") from exc
        raster_crs = src.crs

    data = np.asarray(clipped_data.filled(0), dtype="float32")
    valid = ~np.ma.getmaskarray(clipped_data)
    if not valid.any():
        return _empty_communities()

    community_mask = valid & (data >= threshold)
    if not community_mask.any():
        return _empty_communities()

    features = []
    for geom, value in shapes(community_mask.astype("uint8"), mask=community_mask, transform=clipped_transform):
        if not value:
            continue
        features.append({"class_name": "community", "threshold": threshold, "geometry": shape(geom)})

    if not features:
        return _empty_communities()

    communities = gpd.GeoDataFrame(features, geometry="geometry", crs=raster_crs).to_crs(EUHYDRO_CRS)
    communities["area_m2"] = communities.geometry.area.astype(float)
    communities = communities[communities["area_m2"] >= min_area_m2].copy()
    if communities.empty:
        return _empty_communities()

    communities["geometry"] = communities.geometry.buffer(0)
    return communities[["class_name", "threshold", "area_m2", "geometry"]]


def write_community_layers(communities: gpd.GeoDataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    communities.to_file(output_path, layer=COMMUNITIES_LAYER, driver="GPKG")


def _empty_communities() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(columns=["class_name", "threshold", "area_m2", "geometry"], geometry="geometry", crs=EUHYDRO_CRS)
