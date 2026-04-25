from __future__ import annotations

import argparse
import os
from pathlib import Path

from heavywater_preview.config import (
    DEFAULT_BBOX_SIZE_KM,
    DEFAULT_COMMUNITY_THRESHOLD,
    DEFAULT_EFAS_DAYS_BACK,
    DEFAULT_MIN_COMMUNITY_AREA_M2,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_RIVER_METRIC_LOOKBACK_DAYS,
    DEFAULT_RIVER_METRIC_RESOLUTION_M,
    DEFAULT_TERRAIN_RESOLUTION_M,
    PROJECT_ROOT,
)
from heavywater_preview.pipeline import run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract rivers and Copernicus imperviousness-derived communities for an AOI.")
    parser.add_argument("lat", type=float, help="Latitude in WGS84.")
    parser.add_argument("lon", type=float, help="Longitude in WGS84.")
    parser.add_argument("--size-km", type=float, default=DEFAULT_BBOX_SIZE_KM, help="AOI size in kilometers.")
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
    return parser


def main() -> None:
    _load_dotenv()
    args = build_parser().parse_args()
    outputs = run_pipeline(
        lat=args.lat,
        lon=args.lon,
        size_km=args.size_km,
        output_dir=args.output_dir,
        communities_raster=args.communities_raster,
        community_threshold=args.community_threshold,
        min_community_area_m2=args.min_community_area_m2,
        include_terrain=args.terrain,
        terrain_resolution_m=args.terrain_resolution_m,
        include_river_metrics=args.river_metrics,
        include_river_discharge=args.river_discharge,
        river_metric_resolution_m=args.river_metric_resolution_m,
        river_metric_lookback_days=args.river_lookback_days,
        efas_days_back=args.efas_days_back,
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
