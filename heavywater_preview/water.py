from __future__ import annotations

from pathlib import Path
from typing import Iterable

import fiona
import geopandas as gpd
import pandas as pd

from heavywater_preview.config import (
    EUHYDRO_CRS,
    BASIN_LAYERS,
    LINE_LAYERS,
    POLYGON_LAYERS,
    RIVER_BASINS_LAYER,
    WATER_LINES_LAYER,
    WATER_POLYGONS_LAYER,
)


def _bounds_intersect(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> bool:
    return not (
        left[2] < right[0]
        or left[0] > right[2]
        or left[3] < right[1]
        or left[1] > right[3]
    )


def _iter_candidate_layers(gpkg_path: Path, layer_names: Iterable[str], bbox_bounds):
    for layer_name in layer_names:
        try:
            with fiona.open(gpkg_path, layer=layer_name) as src:
                layer_bounds = src.bounds
        except Exception:
            continue
        if _bounds_intersect(layer_bounds, bbox_bounds):
            yield layer_name


def collect_water_layers(data_dir: Path, bbox_geom):
    line_frames: list[gpd.GeoDataFrame] = []
    polygon_frames: list[gpd.GeoDataFrame] = []
    basin_frames: list[gpd.GeoDataFrame] = []
    bbox_bounds = bbox_geom.bounds

    for gpkg_path in sorted(data_dir.glob("*.gpkg")):
        for layer_name in _iter_candidate_layers(gpkg_path, LINE_LAYERS, bbox_bounds):
            gdf = gpd.read_file(gpkg_path, layer=layer_name, engine="fiona", bbox=bbox_bounds)
            if gdf.empty:
                continue
            clipped = gdf.clip(bbox_geom)
            clipped["source_file"] = gpkg_path.name
            clipped["source_layer"] = layer_name
            line_frames.append(clipped[["source_file", "source_layer", "geometry"]])

        for layer_name in _iter_candidate_layers(gpkg_path, POLYGON_LAYERS, bbox_bounds):
            gdf = gpd.read_file(gpkg_path, layer=layer_name, engine="fiona", bbox=bbox_bounds)
            if gdf.empty:
                continue
            clipped = gdf.clip(bbox_geom)
            clipped["source_file"] = gpkg_path.name
            clipped["source_layer"] = layer_name
            polygon_frames.append(clipped[["source_file", "source_layer", "geometry"]])

        for layer_name in _iter_candidate_layers(gpkg_path, BASIN_LAYERS, bbox_bounds):
            gdf = gpd.read_file(gpkg_path, layer=layer_name, engine="fiona", bbox=bbox_bounds)
            if gdf.empty:
                continue
            clipped = gdf.clip(bbox_geom)
            clipped["source_file"] = gpkg_path.name
            clipped["source_layer"] = layer_name
            basin_frames.append(clipped[["source_file", "source_layer", "geometry"]])

    return _concat_frames(line_frames), _concat_frames(polygon_frames), _concat_frames(basin_frames)


def _concat_frames(frames: list[gpd.GeoDataFrame]) -> gpd.GeoDataFrame:
    usable = [frame for frame in frames if not frame.empty]
    if not usable:
        return gpd.GeoDataFrame(columns=["source_file", "source_layer", "geometry"], geometry="geometry", crs=EUHYDRO_CRS)

    merged = gpd.GeoDataFrame(pd.concat(usable, ignore_index=True), geometry="geometry", crs=usable[0].crs)
    merged = merged[merged.geometry.notna() & ~merged.geometry.is_empty].copy()
    return merged


def write_water_layers(lines: gpd.GeoDataFrame, polygons: gpd.GeoDataFrame, basins: gpd.GeoDataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    lines.to_file(output_path, layer=WATER_LINES_LAYER, driver="GPKG")
    polygons.to_file(output_path, layer=WATER_POLYGONS_LAYER, driver="GPKG")
    basins.to_file(output_path, layer=RIVER_BASINS_LAYER, driver="GPKG")
