from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import shapes
from scipy import ndimage
from shapely.geometry import shape

from heavywater_preview.config import COMMUNITIES_LAYER, EUHYDRO_CRS, IMPACT_ZONE_LAYER


@dataclass
class SarProcessingResult:
    communities: gpd.GeoDataFrame
    impact_zone: gpd.GeoDataFrame
    filtered_db_raster_path: Path
    source_raster_path: Path


def detect_communities(
    sar_raster_path: Path,
    filtered_db_output_path: Path,
    threshold_db: float,
    min_cluster_area_m2: float,
    buffer_distance_m: float,
) -> SarProcessingResult:
    with rasterio.open(sar_raster_path) as src:
        intensity = src.read(1, masked=True).astype("float32").filled(np.nan)
        transform = src.transform
        crs = src.crs
        profile = src.profile.copy()

    backscatter_db = to_decibels(intensity)
    filtered_db = median_filter_db(backscatter_db)
    community_mask = np.isfinite(filtered_db) & (filtered_db > threshold_db)
    cleaned_mask = remove_small_clusters(community_mask, transform, crs, min_cluster_area_m2)

    write_float_raster(filtered_db, profile, filtered_db_output_path)
    communities = vectorize_mask(cleaned_mask, transform, crs, threshold_db)
    communities = communities.to_crs(EUHYDRO_CRS)
    if not communities.empty:
        communities["area_m2"] = communities.geometry.area.astype(float)
        communities = communities[communities["area_m2"] >= min_cluster_area_m2].copy()
    if communities.empty:
        impact_zone = gpd.GeoDataFrame(columns=["class_name", "geometry"], geometry="geometry", crs=EUHYDRO_CRS)
    else:
        communities["class_name"] = "community"
        impact_zone = communities[["class_name", "geometry"]].copy()
        impact_zone["class_name"] = "impact_zone"
        impact_zone["geometry"] = impact_zone.geometry.buffer(buffer_distance_m)

    return SarProcessingResult(
        communities=communities,
        impact_zone=impact_zone,
        filtered_db_raster_path=filtered_db_output_path,
        source_raster_path=sar_raster_path,
    )


def to_decibels(intensity: np.ndarray) -> np.ndarray:
    finite = np.isfinite(intensity)
    if not finite.any():
        return intensity
    if float(np.nanmax(intensity[finite])) <= 5.0:
        return intensity
    result = np.full_like(intensity, np.nan, dtype="float32")
    result[finite] = 10.0 * np.log10(np.clip(intensity[finite], 1e-6, None))
    return result


def median_filter_db(backscatter_db: np.ndarray) -> np.ndarray:
    finite = np.isfinite(backscatter_db)
    if not finite.any():
        return backscatter_db
    fill_value = float(np.nanmin(backscatter_db[finite]))
    filled = np.where(finite, backscatter_db, fill_value)
    filtered = ndimage.median_filter(filled, size=3, mode="nearest")
    filtered = filtered.astype("float32")
    filtered[~finite] = np.nan
    return filtered


def remove_small_clusters(mask_array: np.ndarray, transform, crs, min_cluster_area_m2: float) -> np.ndarray:
    labeled, cluster_count = ndimage.label(mask_array, structure=np.ones((3, 3), dtype="uint8"))
    if cluster_count == 0:
        return mask_array

    if crs is not None and getattr(crs, "is_geographic", False):
        return mask_array

    pixel_area = abs(transform.a * transform.e - transform.b * transform.d)
    min_pixels = max(1, int(np.ceil(min_cluster_area_m2 / pixel_area)))
    counts = np.bincount(labeled.ravel())
    keep = counts >= min_pixels
    keep[0] = False
    return keep[labeled]


def vectorize_mask(mask_array: np.ndarray, transform, crs, threshold_db: float) -> gpd.GeoDataFrame:
    features = []
    for geom, value in shapes(mask_array.astype("uint8"), mask=mask_array, transform=transform):
        if not value:
            continue
        features.append(
            {
                "class_name": "community",
                "threshold_db": threshold_db,
                "geometry": shape(geom),
            }
        )

    if not features:
        return gpd.GeoDataFrame(columns=["class_name", "threshold_db", "geometry"], geometry="geometry", crs=crs)
    return gpd.GeoDataFrame(features, geometry="geometry", crs=crs)


def write_float_raster(data: np.ndarray, profile: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    raster_profile = profile.copy()
    raster_profile.update(
        driver="GTiff",
        dtype="float32",
        count=1,
        compress="deflate",
        nodata=np.nan,
    )
    raster_profile.pop("blockxsize", None)
    raster_profile.pop("blockysize", None)
    raster_profile.pop("tiled", None)
    with rasterio.open(output_path, "w", **raster_profile) as dst:
        dst.write(data.astype("float32"), 1)


def write_community_layers(communities: gpd.GeoDataFrame, impact_zone: gpd.GeoDataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    communities.to_file(output_path, layer=COMMUNITIES_LAYER, driver="GPKG")
    impact_zone.to_file(output_path, layer=IMPACT_ZONE_LAYER, driver="GPKG")
