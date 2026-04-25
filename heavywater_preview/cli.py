from __future__ import annotations

import argparse
import os
from pathlib import Path

from heavywater_preview.config import (
    DEFAULT_BBOX_SIZE_KM,
    DEFAULT_COMMUNITY_THRESHOLD,
    DEFAULT_MIN_COMMUNITY_AREA_M2,
    DEFAULT_OUTPUT_DIR,
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
        help="Water data source. Defaults to 'overpass' to fetch rivers from the OpenStreetMap Overpass API.",
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
    return parser


def main() -> None:
    _load_dotenv()
    args = build_parser().parse_args()
    outputs = run_pipeline(
        lat=args.lat,
        lon=args.lon,
        size_km=args.size_km,
        output_dir=args.output_dir,
        water_source=args.water_source,
        communities_raster=args.communities_raster,
        community_threshold=args.community_threshold,
        min_community_area_m2=args.min_community_area_m2,
        include_terrain=args.terrain,
        terrain_resolution_m=args.terrain_resolution_m,
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
