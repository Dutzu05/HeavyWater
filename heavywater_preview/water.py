from __future__ import annotations

from pathlib import Path
from typing import Iterable

import fiona
import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import LineString, MultiLineString

from heavywater_preview.config import (
    EUHYDRO_CRS,
    BASIN_LAYERS,
    LINE_LAYERS,
    OVERPASS_API_URL,
    POLYGON_LAYERS,
    RIVER_BASINS_LAYER,
    WGS84_CRS,
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


def fetch_water_layers_from_overpass(bbox_wgs84: tuple[float, float, float, float]):
    south = bbox_wgs84[1]
    west = bbox_wgs84[0]
    north = bbox_wgs84[3]
    east = bbox_wgs84[2]
    query = f"""
[out:json][timeout:60];
(
  way["waterway"~"river|stream|canal|ditch|drain"]({south},{west},{north},{east});
  relation["waterway"~"river|stream|canal|ditch|drain"]({south},{west},{north},{east});
);
out geom;
""".strip()
    response = requests.post(
        OVERPASS_API_URL,
        data=query,
        headers={"Content-Type": "text/plain", "User-Agent": "heavywater-preview/1.0"},
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()

    line_records: list[dict] = []
    polygon_records: list[dict] = []
    basin_records: list[dict] = []

    for element in payload.get("elements", []):
        geometry = _geometry_from_overpass_element(element)
        if geometry is None:
            continue
        tags = element.get("tags", {})
        line_records.append(
            {
                "source_file": "overpass",
                "source_layer": tags.get("waterway", element.get("type", "waterway")),
                "name": tags.get("name"),
                "osm_id": str(element.get("id")),
                "geometry": geometry,
            }
        )

    water_lines = _build_wgs84_frame(line_records)
    water_polygons = _build_wgs84_frame(polygon_records)
    river_basins = _build_wgs84_frame(basin_records)
    return water_lines, water_polygons, river_basins


def _concat_frames(frames: list[gpd.GeoDataFrame]) -> gpd.GeoDataFrame:
    usable = [frame for frame in frames if not frame.empty]
    if not usable:
        return gpd.GeoDataFrame(columns=["source_file", "source_layer", "geometry"], geometry="geometry", crs=EUHYDRO_CRS)

    merged = gpd.GeoDataFrame(pd.concat(usable, ignore_index=True), geometry="geometry", crs=usable[0].crs)
    merged = merged[merged.geometry.notna() & ~merged.geometry.is_empty].copy()
    return merged


def _build_wgs84_frame(records: list[dict]) -> gpd.GeoDataFrame:
    if not records:
        return gpd.GeoDataFrame(columns=["source_file", "source_layer", "geometry"], geometry="geometry", crs=WGS84_CRS)

    frame = gpd.GeoDataFrame(records, geometry="geometry", crs=WGS84_CRS)
    frame = frame[frame.geometry.notna() & ~frame.geometry.is_empty].copy()
    return frame


def _geometry_from_overpass_element(element: dict):
    if "geometry" in element:
        coords = [(point["lon"], point["lat"]) for point in element["geometry"]]
        if len(coords) >= 2:
            return LineString(coords)

    members = element.get("members", [])
    member_lines = []
    for member in members:
        member_geometry = member.get("geometry", [])
        coords = [(point["lon"], point["lat"]) for point in member_geometry]
        if len(coords) >= 2:
            member_lines.append(LineString(coords))
    if len(member_lines) == 1:
        return member_lines[0]
    if len(member_lines) > 1:
        return MultiLineString(member_lines)
    return None


def write_water_layers(lines: gpd.GeoDataFrame, polygons: gpd.GeoDataFrame, basins: gpd.GeoDataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    lines.to_file(output_path, layer=WATER_LINES_LAYER, driver="GPKG")
    polygons.to_file(output_path, layer=WATER_POLYGONS_LAYER, driver="GPKG")
    basins.to_file(output_path, layer=RIVER_BASINS_LAYER, driver="GPKG")
