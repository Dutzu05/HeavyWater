from __future__ import annotations

import io
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.errors import WindowError
from rasterio.features import shapes
from rasterio.merge import merge
from rasterio.mask import mask
from rasterio.io import MemoryFile
from shapely.geometry import mapping, shape

from heavywater_preview.config import (
    COMMUNITIES_ARCHIVE_NAME,
    COMMUNITIES_LAYER,
    DEFAULT_COMMUNITY_MERGE_DISTANCE_M,
    EUHYDRO_CRS,
    LOCAL_COMMUNITIES_DATA_DIR,
    WGS84_CRS,
)
from heavywater_preview.geom import project_geometry


def communities_from_impervious_raster(
    raster_path: Path | list[Path] | tuple[Path, ...] | None,
    aoi_wgs84,
    threshold: float,
    min_area_m2: float,
    merge_distance_m: float = DEFAULT_COMMUNITY_MERGE_DISTANCE_M,
) -> gpd.GeoDataFrame:
    if raster_path is None:
        raster_paths = _ensure_overlapping_communities_rasters(aoi_wgs84)
    elif isinstance(raster_path, (list, tuple)):
        raster_paths = [Path(path) for path in raster_path]
    else:
        raster_paths = [Path(raster_path)]

    if not raster_paths:
        return _empty_communities()

    for path in raster_paths:
        if not path.exists():
            raise FileNotFoundError(f"Community raster not found: {path}")

    with _open_community_source(raster_paths) as src:
        try:
            aoi_in_raster_crs = project_geometry(aoi_wgs84, WGS84_CRS, src.crs)
            clipped_data, clipped_transform = mask(src, [mapping(aoi_in_raster_crs)], crop=True, indexes=1, filled=False)
        except (ValueError, WindowError) as exc:
            joined_paths = ", ".join(str(path) for path in raster_paths)
            raise ValueError(f"The community raster does not overlap the requested AOI: {joined_paths}") from exc
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
    communities = merge_nearby_communities(communities, merge_distance_m=merge_distance_m)
    return communities[["class_name", "threshold", "area_m2", "geometry"]]


def write_community_layers(communities: gpd.GeoDataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    communities.to_file(output_path, layer=COMMUNITIES_LAYER, driver="GPKG")


def _empty_communities() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(columns=["class_name", "threshold", "area_m2", "geometry"], geometry="geometry", crs=EUHYDRO_CRS)


def merge_nearby_communities(communities: gpd.GeoDataFrame, merge_distance_m: float) -> gpd.GeoDataFrame:
    if communities.empty or merge_distance_m <= 0:
        return communities.copy()

    working = communities[["class_name", "threshold", "geometry"]].copy()
    working["geometry"] = working.geometry.buffer(merge_distance_m / 2.0)
    dissolved_geometry = working.geometry.union_all()
    if dissolved_geometry.is_empty:
        return _empty_communities()

    merged = gpd.GeoDataFrame(geometry=gpd.GeoSeries([dissolved_geometry], crs=EUHYDRO_CRS).explode(index_parts=False), crs=EUHYDRO_CRS)
    merged["geometry"] = merged.geometry.buffer(-(merge_distance_m / 2.0)).buffer(0)
    merged = merged[merged.geometry.notna() & ~merged.geometry.is_empty].copy()
    if merged.empty:
        return _empty_communities()

    merged["class_name"] = "community"
    merged["threshold"] = float(communities["threshold"].iloc[0]) if "threshold" in communities.columns and not communities.empty else np.nan
    merged["area_m2"] = merged.geometry.area.astype(float)
    return merged[["class_name", "threshold", "area_m2", "geometry"]].reset_index(drop=True)


def _ensure_overlapping_communities_rasters(aoi_wgs84) -> list[Path]:
    tile_keys = _required_tile_keys(aoi_wgs84)
    raster_paths: list[Path] = []
    for tile_key in tile_keys:
        local_path = _find_local_tile(tile_key)
        if local_path is None:
            local_path = _extract_tile_from_archives(tile_key)
        if local_path is not None:
            raster_paths.append(local_path)
    return raster_paths


def _required_tile_keys(aoi_wgs84) -> list[str]:
    projected = project_geometry(aoi_wgs84, WGS84_CRS, EUHYDRO_CRS)
    minx, miny, maxx, maxy = projected.bounds
    xs = range(int(minx // 100000), int(maxx // 100000) + 1)
    ys = range(int(miny // 100000), int(maxy // 100000) + 1)
    return [f"E{x:02d}N{y:02d}" for x in xs for y in ys]


def _find_local_tile(tile_key: str) -> Path | None:
    if not LOCAL_COMMUNITIES_DATA_DIR.exists():
        return None

    for raster_path in sorted(LOCAL_COMMUNITIES_DATA_DIR.glob("*.tif")):
        if tile_key in raster_path.name:
            return raster_path

    return None


def _extract_tile_from_archives(tile_key: str) -> Path | None:
    for archive_path in _community_archives():
        extracted = _extract_tile_from_archive(archive_path, tile_key)
        if extracted is not None:
            return extracted
    return None


def _community_archives() -> list[Path]:
    candidates = [
        LOCAL_COMMUNITIES_DATA_DIR / COMMUNITIES_ARCHIVE_NAME,
        Path.home() / "Downloads" / COMMUNITIES_ARCHIVE_NAME,
    ]
    return [path for path in candidates if path.exists()]


def _extract_tile_from_archive(archive_path: Path, tile_key: str) -> Path | None:
    with zipfile.ZipFile(archive_path) as outer_zip:
        matching_entries = [entry for entry in outer_zip.infolist() if tile_key in entry.filename and entry.filename.endswith(".zip")]
        if not matching_entries:
            return None

        nested_bytes = outer_zip.read(matching_entries[0].filename)

    with zipfile.ZipFile(io.BytesIO(nested_bytes)) as inner_zip:
        tif_name = next((entry.filename for entry in inner_zip.infolist() if entry.filename.endswith(".tif")), None)
        if tif_name is None:
            return None
        LOCAL_COMMUNITIES_DATA_DIR.mkdir(parents=True, exist_ok=True)
        inner_zip.extract(tif_name, path=LOCAL_COMMUNITIES_DATA_DIR)
        return LOCAL_COMMUNITIES_DATA_DIR / tif_name


def _open_community_source(raster_paths: list[Path]):
    if len(raster_paths) == 1:
        return rasterio.open(raster_paths[0])

    datasets = [rasterio.open(path) for path in raster_paths]
    mosaic, transform = merge(datasets)
    profile = datasets[0].profile.copy()
    profile.update(
        driver="GTiff",
        height=mosaic.shape[1],
        width=mosaic.shape[2],
        count=mosaic.shape[0],
        transform=transform,
    )
    memfile = MemoryFile()
    dataset = memfile.open(**profile)
    dataset.write(mosaic)

    class _MergedContext:
        def __enter__(self):
            return dataset

        def __exit__(self, exc_type, exc, tb):
            dataset.close()
            memfile.close()
            for src in datasets:
                src.close()

    return _MergedContext()
