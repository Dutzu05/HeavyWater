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
    DEFAULT_EFAS_DAYS_BACK,
    DEFAULT_RIVER_METRIC_LOOKBACK_DAYS,
    DEFAULT_RIVER_METRIC_RESOLUTION_M,
    DEFAULT_TERRAIN_RESOLUTION_M,
    EUHYDRO_DATA_DIR,
    INDEX_HTML_NAME,
    MAP_HTML_NAME,
    QGS_NAME,
    TERRAIN_DEM_NAME,
    TERRAIN_HILLSHADE_NAME,
    TERRAIN_SUMMARY_NAME,
    WATER_GPKG_NAME,
)
from heavywater_preview.impervious import communities_from_impervious_raster, write_community_layers
from heavywater_preview.leaflet import write_preview_map
from heavywater_preview.qgis_project import write_qgs_project
from heavywater_preview.river_metrics import enrich_rivers_with_metrics
from heavywater_preview.terrain import TerrainResult, fetch_terrain_for_aoi
from heavywater_preview.water import collect_water_layers, write_water_layers


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
    communities_raster: str | Path | None = None,
    community_threshold: float = DEFAULT_COMMUNITY_THRESHOLD,
    min_community_area_m2: float = DEFAULT_MIN_COMMUNITY_AREA_M2,
    include_terrain: bool = False,
    terrain_resolution_m: float = DEFAULT_TERRAIN_RESOLUTION_M,
    include_river_metrics: bool = False,
    include_river_discharge: bool = False,
    river_metric_resolution_m: float = DEFAULT_RIVER_METRIC_RESOLUTION_M,
    river_metric_lookback_days: int = DEFAULT_RIVER_METRIC_LOOKBACK_DAYS,
    efas_days_back: int = DEFAULT_EFAS_DAYS_BACK,
) -> PipelineOutputs:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if not EUHYDRO_DATA_DIR.exists():
        raise FileNotFoundError(
            f"EuHydro data directory not found: {EUHYDRO_DATA_DIR}. "
            "Put the .gpkg files under data\\euhydro or update the configured path."
        )

    bbox_wgs84 = build_bbox(lat, lon, size_km=size_km)
    aoi_wgs84 = bbox_polygon_wgs84(bbox_wgs84)
    aoi_euhydro = reproject_bounds_to_euhydro(bbox_wgs84)

    water_lines, water_polygons, river_basins = collect_water_layers(EUHYDRO_DATA_DIR, aoi_euhydro)
    if include_river_metrics:
        metric_result = enrich_rivers_with_metrics(
            water_lines=water_lines,
            bbox_wgs84=bbox_wgs84,
            output_dir=output_dir,
            metric_resolution_m=river_metric_resolution_m,
            lookback_days=river_metric_lookback_days,
            efas_days_back=efas_days_back,
            include_discharge=include_river_discharge,
        )
        water_lines = metric_result.river_lines
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
