from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import Affine
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window

from heavywater_preview.copernicus import fetch_cdse_access_token, post_cdse_process_request, projected_dimensions
from heavywater_preview.config import (
    DEFAULT_TERRAIN_QUERY_STEP,
    TERRAIN_DEM_INSTANCE,
)


@dataclass
class TerrainResult:
    dem_raster_path: Path
    hillshade_raster_path: Path
    summary_path: Path
    summary: dict
    query_data: dict


def fetch_terrain_for_aoi(
    bbox_wgs84: tuple[float, float, float, float],
    dem_output_path: Path,
    hillshade_output_path: Path,
    summary_output_path: Path,
    resolution_m: float,
) -> TerrainResult:
    width, height = projected_dimensions(bbox_wgs84, resolution_m)
    token = fetch_cdse_access_token()
    payload = _terrain_request_payload(bbox_wgs84, width, height)
    dem_bytes = post_cdse_process_request(payload, token)
    dem_output_path.parent.mkdir(parents=True, exist_ok=True)
    dem_output_path.write_bytes(dem_bytes)

    summary = _write_hillshade_and_summary(dem_output_path, hillshade_output_path, summary_output_path)
    return TerrainResult(
        dem_raster_path=dem_output_path,
        hillshade_raster_path=hillshade_output_path,
        summary_path=summary_output_path,
        summary=summary,
        query_data=_build_query_data(dem_output_path),
    )


def _terrain_request_payload(bbox_wgs84: tuple[float, float, float, float], width: int, height: int) -> dict:
    min_lon, min_lat, max_lon, max_lat = bbox_wgs84
    return {
        "input": {
            "bounds": {
                "properties": {"crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"},
                "bbox": [min_lon, min_lat, max_lon, max_lat],
            },
            "data": [
                {
                    "type": "dem",
                    "dataFilter": {"demInstance": TERRAIN_DEM_INSTANCE},
                    "processing": {
                        "upsampling": "BILINEAR",
                        "downsampling": "BILINEAR",
                    },
                }
            ],
        },
        "output": {"width": width, "height": height, "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}]},
        "evalscript": """
//VERSION=3
function setup() {
  return {
    input: ["DEM"],
    output: {
      id: "default",
      bands: 1,
      sampleType: SampleType.FLOAT32,
    },
  };
}

function evaluatePixel(sample) {
  return [sample.DEM];
}
""".strip(),
    }


def _write_hillshade_and_summary(dem_path: Path, hillshade_path: Path, summary_path: Path) -> dict:
    with rasterio.open(dem_path) as src:
        with WarpedVRT(src, crs="EPSG:3035") as vrt:
            dem = vrt.read(1, masked=True).astype("float32").filled(np.nan)
            profile = vrt.profile.copy()
            transform = vrt.transform

    hillshade = _compute_hillshade(dem, transform)
    profile.update(driver="GTiff", dtype="uint8", count=1, nodata=0, compress="deflate")
    with rasterio.open(hillshade_path, "w", **profile) as dst:
        dst.write(hillshade, 1)

    finite = np.isfinite(dem)
    if finite.any():
        slope = _compute_slope_degrees(dem, transform)
        slope_finite = np.isfinite(slope)
        summary = {
            "elevation_min_m": float(np.nanmin(dem)),
            "elevation_max_m": float(np.nanmax(dem)),
            "elevation_mean_m": float(np.nanmean(dem)),
            "slope_mean_deg": float(np.nanmean(slope)) if slope_finite.any() else None,
            "slope_max_deg": float(np.nanmax(slope)) if slope_finite.any() else None,
        }
    else:
        summary = {
            "elevation_min_m": None,
            "elevation_max_m": None,
            "elevation_mean_m": None,
            "slope_mean_deg": None,
            "slope_max_deg": None,
        }

    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def _compute_hillshade(dem: np.ndarray, transform: Affine, azimuth_deg: float = 315.0, altitude_deg: float = 45.0) -> np.ndarray:
    slope_rad, aspect_rad = _slope_aspect(dem, transform)
    azimuth_rad = np.deg2rad(360.0 - azimuth_deg + 90.0)
    altitude_rad = np.deg2rad(altitude_deg)

    shaded = (
        np.sin(altitude_rad) * np.cos(slope_rad)
        + np.cos(altitude_rad) * np.sin(slope_rad) * np.cos(azimuth_rad - aspect_rad)
    )
    shaded = np.clip(shaded, 0.0, 1.0)
    result = np.round(shaded * 255.0).astype("uint8")
    result[~np.isfinite(dem)] = 0
    return result


def _compute_slope_degrees(dem: np.ndarray, transform: Affine) -> np.ndarray:
    slope_rad, _ = _slope_aspect(dem, transform)
    slope_deg = np.rad2deg(slope_rad)
    slope_deg[~np.isfinite(dem)] = np.nan
    return slope_deg


def _slope_aspect(dem: np.ndarray, transform: Affine) -> tuple[np.ndarray, np.ndarray]:
    xres = abs(transform.a) if transform.a else 1.0
    yres = abs(transform.e) if transform.e else 1.0
    filled = np.where(np.isfinite(dem), dem, np.nanmedian(dem[np.isfinite(dem)]) if np.isfinite(dem).any() else 0.0)
    grad_y, grad_x = np.gradient(filled, yres, xres)
    slope_rad = np.arctan(np.hypot(grad_x, grad_y))
    aspect_rad = np.arctan2(-grad_x, grad_y)
    return slope_rad, aspect_rad
def _build_query_data(dem_path: Path, step: int = DEFAULT_TERRAIN_QUERY_STEP) -> dict:
    with rasterio.open(dem_path) as src:
        width = max(1, src.width // step)
        height = max(1, src.height // step)
        with WarpedVRT(
            src,
            crs="EPSG:4326",
            width=width,
            height=height,
            transform=rasterio.transform.from_bounds(*src.bounds, width, height),
        ) as vrt:
            dem = vrt.read(1, masked=True).astype("float32").filled(np.nan)
            west, south, east, north = vrt.bounds
            transform = vrt.transform

    slope = _compute_slope_degrees_geographic(dem, transform, mean_lat=(south + north) / 2.0)
    dem_out = np.where(np.isfinite(dem), np.round(dem, 1), np.nan)
    slope_out = np.where(np.isfinite(slope), np.round(slope, 1), np.nan)
    return {
        "bounds": [west, south, east, north],
        "width": width,
        "height": height,
        "elevation": dem_out.tolist(),
        "slope": slope_out.tolist(),
    }


def _compute_slope_degrees_geographic(dem: np.ndarray, transform: Affine, mean_lat: float) -> np.ndarray:
    meters_per_deg_lat = 111320.0
    meters_per_deg_lon = max(111320.0 * np.cos(np.deg2rad(mean_lat)), 1.0)
    xres = max(abs(transform.a) * meters_per_deg_lon, 1.0)
    yres = max(abs(transform.e) * meters_per_deg_lat, 1.0)
    filled = np.where(np.isfinite(dem), dem, np.nanmedian(dem[np.isfinite(dem)]) if np.isfinite(dem).any() else 0.0)
    grad_y, grad_x = np.gradient(filled, yres, xres)
    slope = np.rad2deg(np.arctan(np.hypot(grad_x, grad_y)))
    slope[~np.isfinite(dem)] = np.nan
    return slope


def sample_terrain_point(dem_path: Path, lat: float, lon: float) -> dict:
    with rasterio.open(dem_path) as src:
        with WarpedVRT(src, crs="EPSG:4326") as vrt:
            west, south, east, north = vrt.bounds
            if lon < west or lon > east or lat < south or lat > north:
                raise ValueError("Terrain data unavailable outside the fetched AOI.")

            row, col = vrt.index(lon, lat)
            if row < 0 or col < 0 or row >= vrt.height or col >= vrt.width:
                raise ValueError("Terrain sample is outside the raster grid.")

            window = Window(max(0, col - 1), max(0, row - 1), min(3, vrt.width - max(0, col - 1)), min(3, vrt.height - max(0, row - 1)))
            dem = vrt.read(1, window=window, masked=True).astype("float32").filled(np.nan)
            transform = vrt.window_transform(window)
            slope = _compute_slope_degrees_geographic(dem, transform, mean_lat=lat)

            center_row = min(1, dem.shape[0] - 1)
            center_col = min(1, dem.shape[1] - 1)
            elevation = float(dem[center_row, center_col]) if np.isfinite(dem[center_row, center_col]) else None
            slope_deg = float(slope[center_row, center_col]) if np.isfinite(slope[center_row, center_col]) else None

    return {
        "elevation_m": elevation,
        "slope_deg": slope_deg,
    }
