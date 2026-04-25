from __future__ import annotations

import argparse
from pathlib import Path

from heavywater_preview.config import (
    DEFAULT_BBOX_SIZE_KM,
    DEFAULT_COMMUNITY_THRESHOLD,
    DEFAULT_MIN_COMMUNITY_AREA_M2,
    DEFAULT_OUTPUT_DIR,
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
    return parser


def main() -> None:
    args = build_parser().parse_args()
    outputs = run_pipeline(
        lat=args.lat,
        lon=args.lon,
        size_km=args.size_km,
        output_dir=args.output_dir,
        communities_raster=args.communities_raster,
        community_threshold=args.community_threshold,
        min_community_area_m2=args.min_community_area_m2,
    )
    print(str(outputs.map_html_path))
