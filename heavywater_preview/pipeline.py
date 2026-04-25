from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from heavywater_preview.aoi import bbox_polygon_wgs84, build_bbox, reproject_bounds_to_euhydro
from heavywater_preview.config import (
    COMMUNITY_GPKG_NAME,
    DEFAULT_BBOX_SIZE_KM,
    DEFAULT_COMMUNITY_THRESHOLD,
    DEFAULT_MIN_COMMUNITY_AREA_M2,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_TERRAIN_RESOLUTION_M,
    DEFAULT_WATER_SOURCE,
    EUHYDRO_DATA_DIR,
    INDEX_HTML_NAME,
    MAP_HTML_NAME,
    QGS_NAME,
    TERRAIN_DEM_NAME,
    TERRAIN_HILLSHADE_NAME,
    TERRAIN_SUMMARY_NAME,
    WATER_GPKG_NAME,
    WATER_SOURCE_EUHYDRO,
    WATER_SOURCE_OVERPASS,
)
from heavywater_preview.impervious import communities_from_impervious_raster, write_community_layers
from heavywater_preview.leaflet import write_preview_map
from heavywater_preview.qgis_project import write_qgs_project
from heavywater_preview.terrain import TerrainResult, fetch_terrain_for_aoi
from heavywater_preview.water import collect_water_layers, fetch_water_layers_from_overpass, write_water_layers


@dataclass
class PipelineOutputs:
    output_dir: Path
    water_gpkg: Path
    community_gpkg: Path
    terrain_dem_raster: Path | None
    terrain_hillshade_raster: Path | None
    terrain_summary_path: Path | None
    qgs_path: Path
    map_html_path: Path
    index_html_path: Path


def run_pipeline(
    lat: float,
    lon: float,
    size_km: float = DEFAULT_BBOX_SIZE_KM,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    water_source: str = DEFAULT_WATER_SOURCE,
    communities_raster: str | Path | None = None,
    community_threshold: float = DEFAULT_COMMUNITY_THRESHOLD,
    min_community_area_m2: float = DEFAULT_MIN_COMMUNITY_AREA_M2,
    include_terrain: bool = False,
    terrain_resolution_m: float = DEFAULT_TERRAIN_RESOLUTION_M,
) -> PipelineOutputs:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    bbox_wgs84 = build_bbox(lat, lon, size_km=size_km)
    aoi_wgs84 = bbox_polygon_wgs84(bbox_wgs84)
    if water_source == WATER_SOURCE_EUHYDRO:
        if not EUHYDRO_DATA_DIR.exists():
            raise FileNotFoundError(
                f"EuHydro data directory not found: {EUHYDRO_DATA_DIR}. "
                "Put the .gpkg files under data\\euhydro, update the configured path, or use the default Overpass API source."
            )
        aoi_euhydro = reproject_bounds_to_euhydro(bbox_wgs84)
        water_lines, water_polygons, river_basins = collect_water_layers(EUHYDRO_DATA_DIR, aoi_euhydro)
    elif water_source == WATER_SOURCE_OVERPASS:
        water_lines, water_polygons, river_basins = fetch_water_layers_from_overpass(bbox_wgs84)
    else:
        raise ValueError(f"Unsupported water source: {water_source}")

    water_gpkg = output_dir / WATER_GPKG_NAME
    write_water_layers(water_lines, water_polygons, river_basins, water_gpkg)

    communities = communities_from_impervious_raster(
        raster_path=communities_raster,
        aoi_wgs84=aoi_wgs84,
        threshold=community_threshold,
        min_area_m2=min_community_area_m2,
    )
    community_gpkg = output_dir / COMMUNITY_GPKG_NAME
    write_community_layers(communities, community_gpkg)

    terrain_result: TerrainResult | None = None
    if include_terrain:
        terrain_result = fetch_terrain_for_aoi(
            bbox_wgs84=bbox_wgs84,
            dem_output_path=output_dir / TERRAIN_DEM_NAME,
            hillshade_output_path=output_dir / TERRAIN_HILLSHADE_NAME,
            summary_output_path=output_dir / TERRAIN_SUMMARY_NAME,
            resolution_m=terrain_resolution_m,
        )

    qgs_path = output_dir / QGS_NAME
    write_qgs_project(
        qgs_path=qgs_path,
        water_gpkg=water_gpkg,
        community_gpkg=community_gpkg,
        terrain_raster=terrain_result.hillshade_raster_path if terrain_result else None,
        bbox_wgs84=bbox_wgs84,
    )

    map_html_path = output_dir / MAP_HTML_NAME
    index_html_path = output_dir / INDEX_HTML_NAME
    write_preview_map(
        html_path=map_html_path,
        index_path=index_html_path,
        lat=lat,
        lon=lon,
        bbox_wgs84=bbox_wgs84,
        water_lines=water_lines,
        communities=communities,
        terrain_dem_raster=terrain_result.dem_raster_path if terrain_result else None,
        terrain_hillshade_raster=terrain_result.hillshade_raster_path if terrain_result else None,
        terrain_query_data=terrain_result.query_data if terrain_result else None,
    )

    return PipelineOutputs(
        output_dir=output_dir,
        water_gpkg=water_gpkg,
        community_gpkg=community_gpkg,
        terrain_dem_raster=terrain_result.dem_raster_path if terrain_result else None,
        terrain_hillshade_raster=terrain_result.hillshade_raster_path if terrain_result else None,
        terrain_summary_path=terrain_result.summary_path if terrain_result else None,
        qgs_path=qgs_path,
        map_html_path=map_html_path,
        index_html_path=index_html_path,
    )
