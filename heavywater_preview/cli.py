from __future__ import annotations

import argparse
import os
from pathlib import Path

from heavywater_preview.config import (
    DEFAULT_BBOX_SIZE_KM,
    DEFAULT_COMMUNITY_PIXEL_AREA_M2,
    DEFAULT_COMMUNITY_MERGE_DISTANCE_M,
    DEFAULT_COMMUNITY_THRESHOLD,
    DEFAULT_DIFFERENTIAL_MOTION_THRESHOLD,
    DEFAULT_EFAS_DAYS_BACK,
    DEFAULT_FARM_DEMAND_M3_DAY,
    DEFAULT_GLOFAS_DAYS_BACK,
    DEFAULT_MIN_COMMUNITY_AREA_M2,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_PEOPLE_PER_CLUSTER_PIXEL,
    DEFAULT_RIVER_METRIC_LOOKBACK_DAYS,
    DEFAULT_RIVER_METRIC_RESOLUTION_M,
    DEFAULT_STABILITY_BUFFER_M,
    DEFAULT_TERRAIN_RESOLUTION_M,
    DEFAULT_WATER_SOURCE,
    PROJECT_ROOT,
    WATER_SOURCE_EUHYDRO,
    WATER_SOURCE_OVERPASS,
)
from heavywater_preview.pipeline import run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract rivers and Copernicus imperviousness-derived communities for an AOI.")
    parser.add_argument("lat", type=float, help="Latitude in WGS84.")
    parser.add_argument("lon", type=float, help="Longitude in WGS84.")
    parser.add_argument("--size-km", type=float, default=DEFAULT_BBOX_SIZE_KM, help="AOI size in kilometers.")
    parser.add_argument(
        "--water-source",
        choices=(WATER_SOURCE_EUHYDRO, WATER_SOURCE_OVERPASS),
        default=DEFAULT_WATER_SOURCE,
        help="Water vector source used for rivers and water bodies.",
    )
    parser.add_argument(
        "--communities-raster",
        type=Path,
        help="Copernicus imperviousness or built-up GeoTIFF used to build the Communities layer.",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Output directory for HTML, GPKG, QGS, and clipped rasters.")
    parser.add_argument(
        "--community-threshold",
        type=float,
        default=DEFAULT_COMMUNITY_THRESHOLD,
        help="Raster value threshold for communities. Use 1 for built-up rasters, or a higher value for density rasters.",
    )
    parser.add_argument(
        "--min-community-area-m2",
        type=float,
        default=DEFAULT_MIN_COMMUNITY_AREA_M2,
        help="Minimum connected community area to keep.",
    )
    parser.add_argument(
        "--community-merge-distance-m",
        type=float,
        default=DEFAULT_COMMUNITY_MERGE_DISTANCE_M,
        help="Merge built-up community patches separated by this distance or less.",
    )
    parser.add_argument(
        "--terrain",
        action="store_true",
        help="Fetch Copernicus GLO-30 terrain through the Copernicus Data Space Sentinel Hub Process API.",
    )
    parser.add_argument(
        "--terrain-resolution-m",
        type=float,
        default=DEFAULT_TERRAIN_RESOLUTION_M,
        help="Requested terrain raster resolution in meters for the fetched DEM.",
    )
    parser.add_argument(
        "--river-metrics",
        action="store_true",
        help="Fetch Sentinel-1/Sentinel-2 water masks to enrich rivers with observed width metrics.",
    )
    parser.add_argument(
        "--river-discharge",
        action="store_true",
        help="Also fetch EFAS discharge. This can take much longer than width extraction.",
    )
    parser.add_argument(
        "--river-metric-resolution-m",
        type=float,
        default=DEFAULT_RIVER_METRIC_RESOLUTION_M,
        help="Requested Sentinel-1/Sentinel-2 raster resolution in meters for width extraction.",
    )
    parser.add_argument(
        "--river-lookback-days",
        type=int,
        default=DEFAULT_RIVER_METRIC_LOOKBACK_DAYS,
        help="How many recent days of Sentinel-1/Sentinel-2 scenes to search for width extraction.",
    )
    parser.add_argument(
        "--efas-days-back",
        type=int,
        default=DEFAULT_EFAS_DAYS_BACK,
        help="How many days back from today to request EFAS historical discharge.",
    )
    parser.add_argument(
        "--stability",
        action="store_true",
        help="Evaluate structural stability from EGMS L3 Ortho Vertical measurement points.",
    )
    parser.add_argument(
        "--egms-ortho-vertical",
        type=str,
        help="Local path or HTTPS URL to an EGMS L3 Ortho Vertical CSV or GeoJSON export.",
    )
    parser.add_argument(
        "--stability-buffer-m",
        type=float,
        default=DEFAULT_STABILITY_BUFFER_M,
        help="Buffer distance in meters around the reservoir site and canal route.",
    )
    parser.add_argument(
        "--differential-motion-threshold",
        type=float,
        default=DEFAULT_DIFFERENTIAL_MOTION_THRESHOLD,
        help="Threshold in mm/year for flagging canal differential motion.",
    )
    parser.add_argument(
        "--reservoir-site-lat",
        type=float,
        help="Optional reservoir site latitude. Defaults to the AOI center.",
    )
    parser.add_argument(
        "--reservoir-site-lon",
        type=float,
        help="Optional reservoir site longitude. Defaults to the AOI center.",
    )
    parser.add_argument(
        "--canal-route",
        type=Path,
        help="Optional line vector file for the proposed canal route. Falls back to the longest clipped water line.",
    )
    parser.add_argument(
        "--water-risk",
        action="store_true",
        help="Run the water-risk analysis and feasibility suggestions.",
    )
    parser.add_argument(
        "--water-risk-mode",
        choices=("community", "farm"),
        default="community",
        help="Community discovery scans cluster centroids; farm mode treats the input lat/lon as the demand center.",
    )
    parser.add_argument(
        "--farm-demand-m3-day",
        type=float,
        default=DEFAULT_FARM_DEMAND_M3_DAY,
        help="Estimated daily water demand for farm siting mode.",
    )
    parser.add_argument(
        "--cluster-pixel-area-m2",
        type=float,
        default=DEFAULT_COMMUNITY_PIXEL_AREA_M2,
        help="Pixel area used to convert community polygons into cluster pixel counts until GHSL/WorldPop is wired.",
    )
    parser.add_argument(
        "--people-per-cluster-pixel",
        type=float,
        default=DEFAULT_PEOPLE_PER_CLUSTER_PIXEL,
        help="Population proxy per cluster pixel for community discovery mode.",
    )
    parser.add_argument(
        "--glofas-days-back",
        type=int,
        default=DEFAULT_GLOFAS_DAYS_BACK,
        help="How many days back from today to request GloFAS historical discharge.",
    )
    return parser


def main() -> None:
    _load_dotenv()
    args = build_parser().parse_args()
    if (args.reservoir_site_lat is None) != (args.reservoir_site_lon is None):
        raise SystemExit("Provide both --reservoir-site-lat and --reservoir-site-lon together.")
    outputs = run_pipeline(
        lat=args.lat,
        lon=args.lon,
        size_km=args.size_km,
        output_dir=args.output_dir,
        water_source=args.water_source,
        communities_raster=args.communities_raster,
        community_threshold=args.community_threshold,
        min_community_area_m2=args.min_community_area_m2,
        community_merge_distance_m=args.community_merge_distance_m,
        include_terrain=args.terrain,
        terrain_resolution_m=args.terrain_resolution_m,
        include_river_metrics=args.river_metrics,
        include_river_discharge=args.river_discharge,
        river_metric_resolution_m=args.river_metric_resolution_m,
        river_metric_lookback_days=args.river_lookback_days,
        efas_days_back=args.efas_days_back,
        include_stability=args.stability,
        egms_ortho_vertical=args.egms_ortho_vertical,
        stability_buffer_m=args.stability_buffer_m,
        differential_motion_threshold_mm_per_year=args.differential_motion_threshold,
        reservoir_site_wgs84=(args.reservoir_site_lat, args.reservoir_site_lon) if args.reservoir_site_lat is not None else None,
        canal_route_source=args.canal_route,
        include_water_risk=args.water_risk,
        water_risk_mode=args.water_risk_mode,
        farm_demand_m3_day=args.farm_demand_m3_day,
        cluster_pixel_area_m2=args.cluster_pixel_area_m2,
        people_per_cluster_pixel=args.people_per_cluster_pixel,
        glofas_days_back=args.glofas_days_back,
    )
    print(str(outputs.map_html_path))


def _load_dotenv() -> None:
    for dotenv_path in (PROJECT_ROOT / ".env", PROJECT_ROOT / ".env.local"):
        if not dotenv_path.exists():
            continue
        for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = value
