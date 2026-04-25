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
    EUHYDRO_DATA_DIR,
    INDEX_HTML_NAME,
    MAP_HTML_NAME,
    QGS_NAME,
    WATER_GPKG_NAME,
)
from heavywater_preview.impervious import communities_from_impervious_raster, write_community_layers
from heavywater_preview.leaflet import write_preview_map
from heavywater_preview.qgis_project import write_qgs_project
from heavywater_preview.water import collect_water_layers, write_water_layers


@dataclass
class PipelineOutputs:
    output_dir: Path
    water_gpkg: Path
    community_gpkg: Path
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

    qgs_path = output_dir / QGS_NAME
    write_qgs_project(
        qgs_path=qgs_path,
        water_gpkg=water_gpkg,
        community_gpkg=community_gpkg,
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
    )

    return PipelineOutputs(
        output_dir=output_dir,
        water_gpkg=water_gpkg,
        community_gpkg=community_gpkg,
        qgs_path=qgs_path,
        map_html_path=map_html_path,
        index_html_path=index_html_path,
    )
