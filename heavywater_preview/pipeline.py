from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from heavywater_preview.aoi import bbox_polygon_wgs84, build_bbox, reproject_bounds_to_euhydro
from heavywater_preview.config import (
    COMMUNITY_GPKG_NAME,
    DEFAULT_BBOX_SIZE_KM,
    DEFAULT_COMMUNITY_PIXEL_AREA_M2,
    DEFAULT_COMMUNITY_THRESHOLD,
    DEFAULT_FARM_DEMAND_M3_DAY,
    DEFAULT_MIN_COMMUNITY_AREA_M2,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_DIFFERENTIAL_MOTION_THRESHOLD,
    DEFAULT_EFAS_DAYS_BACK,
    DEFAULT_GLOFAS_DAYS_BACK,
    DEFAULT_PEOPLE_PER_CLUSTER_PIXEL,
    DEFAULT_RIVER_METRIC_LOOKBACK_DAYS,
    DEFAULT_RIVER_METRIC_RESOLUTION_M,
    DEFAULT_STABILITY_BUFFER_M,
    DEFAULT_TERRAIN_RESOLUTION_M,
    DEFAULT_WATER_SOURCE,
    EUHYDRO_DATA_DIR,
    INDEX_HTML_NAME,
    MAP_HTML_NAME,
    QGS_NAME,
    REPORT_INPUTS_NAME,
    STABILITY_POINTS_NAME,
    STABILITY_SUMMARY_NAME,
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
from heavywater_preview.report import build_report_inputs, write_report_inputs
from heavywater_preview.risk import WaterRiskResult, run_water_risk_analysis
from heavywater_preview.river_metrics import enrich_rivers_with_metrics
from heavywater_preview.stability import StabilityResult, evaluate_structural_stability
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
    stability_points_path: Path | None
    stability_summary_path: Path | None
    water_risk_points_path: Path | None
    water_risk_canals_path: Path | None
    water_risk_sites_path: Path | None
    water_risk_summary_path: Path | None
    report_inputs_path: Path
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
    include_river_metrics: bool = False,
    include_river_discharge: bool = False,
    river_metric_resolution_m: float = DEFAULT_RIVER_METRIC_RESOLUTION_M,
    river_metric_lookback_days: int = DEFAULT_RIVER_METRIC_LOOKBACK_DAYS,
    efas_days_back: int = DEFAULT_EFAS_DAYS_BACK,
    include_stability: bool = False,
    egms_ortho_vertical: str | Path | None = None,
    stability_buffer_m: float = DEFAULT_STABILITY_BUFFER_M,
    differential_motion_threshold_mm_per_year: float = DEFAULT_DIFFERENTIAL_MOTION_THRESHOLD,
    reservoir_site_wgs84: tuple[float, float] | None = None,
    canal_route_source: str | Path | None = None,
    include_water_risk: bool = False,
    water_risk_mode: str = "community",
    farm_demand_m3_day: float = DEFAULT_FARM_DEMAND_M3_DAY,
    cluster_pixel_area_m2: float = DEFAULT_COMMUNITY_PIXEL_AREA_M2,
    people_per_cluster_pixel: float = DEFAULT_PEOPLE_PER_CLUSTER_PIXEL,
    glofas_days_back: int = DEFAULT_GLOFAS_DAYS_BACK,
) -> PipelineOutputs:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    bbox_wgs84 = build_bbox(lat, lon, size_km=size_km)
    aoi_wgs84 = bbox_polygon_wgs84(bbox_wgs84)

    if water_source == WATER_SOURCE_EUHYDRO:
        if not EUHYDRO_DATA_DIR.exists():
            raise FileNotFoundError(
                f"EuHydro data directory not found: {EUHYDRO_DATA_DIR}. "
                "Put the .gpkg files under data\\euhydro, update the configured path, or choose OpenStreetMap Overpass."
            )
        aoi_euhydro = reproject_bounds_to_euhydro(bbox_wgs84)
        water_lines, water_polygons, river_basins = collect_water_layers(EUHYDRO_DATA_DIR, aoi_euhydro)
    elif water_source == WATER_SOURCE_OVERPASS:
        water_lines, water_polygons, river_basins = fetch_water_layers_from_overpass(bbox_wgs84)
    else:
        raise ValueError(f"Unsupported water source: {water_source}")
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

    stability_result: StabilityResult | None = None
    if include_stability:
        stability_result = evaluate_structural_stability(
            bbox_wgs84=bbox_wgs84,
            output_points_path=output_dir / STABILITY_POINTS_NAME,
            output_summary_path=output_dir / STABILITY_SUMMARY_NAME,
            egms_source=egms_ortho_vertical,
            buffer_m=stability_buffer_m,
            differential_motion_threshold_mm_per_year=differential_motion_threshold_mm_per_year,
            reservoir_site_wgs84=reservoir_site_wgs84,
            canal_route_source=canal_route_source,
            fallback_river_lines=water_lines,
        )

    water_risk_result: WaterRiskResult | None = None
    if include_water_risk:
        water_risk_result = run_water_risk_analysis(
            mode=water_risk_mode,
            bbox_wgs84=bbox_wgs84,
            output_dir=output_dir,
            water_lines=water_lines,
            water_polygons=water_polygons,
            communities=communities,
            terrain_dem_raster=terrain_result.dem_raster_path if terrain_result else None,
            demand_center_wgs84=(lat, lon) if water_risk_mode == "farm" else None,
            farm_demand_m3_day=farm_demand_m3_day,
            cluster_pixel_area_m2=cluster_pixel_area_m2,
            people_per_cluster_pixel=people_per_cluster_pixel,
            glofas_days_back=glofas_days_back,
            egms_ortho_vertical=egms_ortho_vertical,
            stability_buffer_m=stability_buffer_m,
            differential_motion_threshold_mm_per_year=differential_motion_threshold_mm_per_year,
        )

    report_inputs_path = output_dir / REPORT_INPUTS_NAME
    report_inputs = build_report_inputs(
        lat=lat,
        lon=lon,
        size_km=size_km,
        terrain_summary=terrain_result.summary if terrain_result else None,
        stability_summary=stability_result.summary if stability_result else None,
        water_risk_summary=water_risk_result.summary if water_risk_result else None,
    )
    write_report_inputs(report_inputs_path, report_inputs)

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
        water_risk_points=water_risk_result.risk_points if water_risk_result else None,
        canal_paths=water_risk_result.canals if water_risk_result else None,
        feasibility_sites=water_risk_result.sites if water_risk_result else None,
    )

    return PipelineOutputs(
        output_dir=output_dir,
        water_gpkg=water_gpkg,
        community_gpkg=community_gpkg,
        terrain_dem_raster=terrain_result.dem_raster_path if terrain_result else None,
        terrain_hillshade_raster=terrain_result.hillshade_raster_path if terrain_result else None,
        terrain_summary_path=terrain_result.summary_path if terrain_result else None,
        stability_points_path=stability_result.points_path if stability_result else None,
        stability_summary_path=stability_result.summary_path if stability_result else None,
        water_risk_points_path=water_risk_result.risk_points_path if water_risk_result else None,
        water_risk_canals_path=water_risk_result.canals_path if water_risk_result else None,
        water_risk_sites_path=water_risk_result.sites_path if water_risk_result else None,
        water_risk_summary_path=water_risk_result.summary_path if water_risk_result else None,
        report_inputs_path=report_inputs_path,
        qgs_path=qgs_path,
        map_html_path=map_html_path,
        index_html_path=index_html_path,
    )
