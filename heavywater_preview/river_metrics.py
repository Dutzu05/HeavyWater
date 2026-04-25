from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import shapes
from rasterio.io import MemoryFile
from scipy import ndimage
from shapely.geometry import shape
from shapely.ops import unary_union

from heavywater_preview.copernicus import fetch_cdse_access_token, first_env_value, post_cdse_process_request, projected_dimensions
from heavywater_preview.config import EUHYDRO_CRS, EWDS_API_KEY_ENV_VARS, EWDS_API_URL_ENV_VARS


@dataclass
class RiverMetricsResult:
    river_lines: gpd.GeoDataFrame
    observed_water: gpd.GeoDataFrame
    sentinel1_mask_path: Path | None
    sentinel2_mask_path: Path | None
    combined_mask_path: Path | None
    discharge_cache_path: Path | None
    discharge_date: str | None


def enrich_rivers_with_metrics(
    water_lines: gpd.GeoDataFrame,
    bbox_wgs84: tuple[float, float, float, float],
    output_dir: Path,
    metric_resolution_m: float,
    lookback_days: int,
    efas_days_back: int,
    include_discharge: bool,
) -> RiverMetricsResult:
    if water_lines.empty:
        return RiverMetricsResult(
            river_lines=water_lines.copy(),
            observed_water=gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs=EUHYDRO_CRS),
            sentinel1_mask_path=None,
            sentinel2_mask_path=None,
            combined_mask_path=None,
            discharge_cache_path=None,
            discharge_date=None,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    width_px, height_px = projected_dimensions(bbox_wgs84, metric_resolution_m)
    token = fetch_cdse_access_token()
    time_to = date.today()
    time_from = time_to - timedelta(days=lookback_days)

    sentinel1_mask_path = output_dir / "sentinel1_water_mask.tif"
    sentinel2_mask_path = output_dir / "sentinel2_water_mask.tif"
    combined_mask_path = output_dir / "observed_water_mask.tif"
    discharge_cache_path = output_dir / "efas_discharge_latest.nc"

    s1_data, s1_profile = _fetch_mask(
        _sentinel1_mask_payload(bbox_wgs84, width_px, height_px, time_from.isoformat(), time_to.isoformat()),
        token,
        sentinel1_mask_path,
    )
    s2_data, s2_profile = _fetch_mask(
        _sentinel2_mask_payload(bbox_wgs84, width_px, height_px, time_from.isoformat(), time_to.isoformat()),
        token,
        sentinel2_mask_path,
    )
    combined_mask, combined_profile = _combine_masks(s1_data, s1_profile, s2_data, s2_profile)
    if combined_profile is not None:
        _write_mask_raster(combined_mask_path, combined_mask, combined_profile)

    observed_water = _vectorize_water_mask(combined_mask, combined_profile)
    width_source = _width_source_label(s1_profile is not None, s2_profile is not None)
    enriched = _attach_width_metrics(water_lines, observed_water, width_source)
    discharge_date = None
    if include_discharge:
        enriched, discharge_date = _attach_discharge_metrics(enriched, discharge_cache_path, efas_days_back=efas_days_back)
    else:
        enriched["discharge_source"] = "not_requested"
        enriched["discharge_date"] = None
    enriched = _attach_quantity_score(enriched)

    return RiverMetricsResult(
        river_lines=enriched,
        observed_water=observed_water,
        sentinel1_mask_path=sentinel1_mask_path if s1_profile is not None else None,
        sentinel2_mask_path=sentinel2_mask_path if s2_profile is not None else None,
        combined_mask_path=combined_mask_path if combined_profile is not None else None,
        discharge_cache_path=discharge_cache_path if discharge_cache_path.exists() else None,
        discharge_date=discharge_date,
    )


def _fetch_mask(payload: dict, token: str, output_path: Path) -> tuple[np.ndarray | None, dict | None]:
    try:
        raster_bytes = post_cdse_process_request(payload, token)
    except Exception:
        return None, None

    with MemoryFile(raster_bytes) as memfile:
        with memfile.open() as src:
            data = src.read(1)
            profile = src.profile.copy()
    _write_mask_raster(output_path, data, profile)
    return data, profile


def _sentinel1_mask_payload(
    bbox_wgs84: tuple[float, float, float, float],
    width: int,
    height: int,
    time_from: str,
    time_to: str,
) -> dict:
    min_lon, min_lat, max_lon, max_lat = bbox_wgs84
    return {
        "input": {
            "bounds": {
                "properties": {"crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"},
                "bbox": [min_lon, min_lat, max_lon, max_lat],
            },
            "data": [
                {
                    "type": "sentinel-1-grd",
                    "dataFilter": {
                        "timeRange": {"from": f"{time_from}T00:00:00Z", "to": f"{time_to}T23:59:59Z"},
                        "acquisitionMode": "IW",
                        "polarization": "DV",
                        "resolution": "HIGH",
                    },
                    "processing": {
                        "orthorectify": True,
                        "backCoeff": "GAMMA0_TERRAIN",
                        "demInstance": "COPERNICUS_30",
                        "speckleFilter": {"type": "LEE", "windowSizeX": 3, "windowSizeY": 3},
                    },
                }
            ],
        },
        "output": {"width": width, "height": height, "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}]},
        "evalscript": """
//VERSION=3
function setup() {
  return {
    input: ["VV", "VH", "dataMask"],
    output: { bands: 1, sampleType: SampleType.UINT8 }
  };
}

function toDb(value) {
  return 10.0 * Math.log(value) / Math.LN10;
}

function evaluatePixel(sample) {
  if (sample.dataMask === 0 || sample.VV <= 0 || sample.VH <= 0) {
    return [0];
  }
  const vv = toDb(sample.VV);
  const vh = toDb(sample.VH);
  const water = vv < -17.0 && vh < -24.0;
  return [water ? 1 : 0];
}
""".strip(),
    }


def _sentinel2_mask_payload(
    bbox_wgs84: tuple[float, float, float, float],
    width: int,
    height: int,
    time_from: str,
    time_to: str,
) -> dict:
    min_lon, min_lat, max_lon, max_lat = bbox_wgs84
    return {
        "input": {
            "bounds": {
                "properties": {"crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"},
                "bbox": [min_lon, min_lat, max_lon, max_lat],
            },
            "data": [
                {
                    "type": "sentinel-2-l2a",
                    "dataFilter": {
                        "timeRange": {"from": f"{time_from}T00:00:00Z", "to": f"{time_to}T23:59:59Z"},
                        "mosaickingOrder": "mostRecent",
                        "maxCloudCoverage": 35,
                    },
                }
            ],
        },
        "output": {"width": width, "height": height, "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}]},
        "evalscript": """
//VERSION=3
function setup() {
  return {
    input: ["B03", "B08", "SCL", "dataMask"],
    output: { bands: 1, sampleType: SampleType.UINT8 }
  };
}

function evaluatePixel(sample) {
  if (sample.dataMask === 0) {
    return [0];
  }
  const blocked = [3, 8, 9, 10, 11];
  if (blocked.indexOf(sample.SCL) >= 0) {
    return [0];
  }
  const denom = sample.B03 + sample.B08;
  if (denom <= 0) {
    return [0];
  }
  const ndwi = (sample.B03 - sample.B08) / denom;
  const water = ndwi > 0.12 && sample.B08 < 0.18;
  return [water ? 1 : 0];
}
""".strip(),
    }


def _combine_masks(
    s1_data: np.ndarray | None,
    s1_profile: dict | None,
    s2_data: np.ndarray | None,
    s2_profile: dict | None,
) -> tuple[np.ndarray, dict | None]:
    profile = s1_profile or s2_profile
    if profile is None:
        return np.zeros((1, 1), dtype="uint8"), None

    masks = []
    if s1_data is not None:
        masks.append(s1_data > 0)
    if s2_data is not None:
        masks.append(s2_data > 0)
    if not masks:
        return np.zeros((profile["height"], profile["width"]), dtype="uint8"), profile

    combined = np.logical_or.reduce(masks)
    combined = ndimage.binary_closing(combined, structure=np.ones((3, 3), dtype="uint8"))
    combined = ndimage.binary_opening(combined, structure=np.ones((3, 3), dtype="uint8"))
    return combined.astype("uint8"), profile


def _write_mask_raster(output_path: Path, data: np.ndarray, profile: dict) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    raster_profile = profile.copy()
    raster_profile.update(driver="GTiff", dtype="uint8", count=1, compress="deflate", nodata=0)
    with rasterio.open(output_path, "w", **raster_profile) as dst:
        dst.write(data.astype("uint8"), 1)


def _vectorize_water_mask(mask: np.ndarray, profile: dict | None) -> gpd.GeoDataFrame:
    if profile is None or mask.size == 0 or not np.any(mask):
        return gpd.GeoDataFrame(columns=["area_m2", "geometry"], geometry="geometry", crs=EUHYDRO_CRS)

    features = []
    for geom, value in shapes(mask.astype("uint8"), mask=mask.astype(bool), transform=profile["transform"]):
        if not value:
            continue
        features.append({"geometry": shape(geom)})

    if not features:
        return gpd.GeoDataFrame(columns=["area_m2", "geometry"], geometry="geometry", crs=EUHYDRO_CRS)

    water = gpd.GeoDataFrame(features, geometry="geometry", crs=profile["crs"]).to_crs(EUHYDRO_CRS)
    water = water[water.geometry.notna() & ~water.geometry.is_empty].copy()
    if water.empty:
        return gpd.GeoDataFrame(columns=["area_m2", "geometry"], geometry="geometry", crs=EUHYDRO_CRS)
    water["area_m2"] = water.geometry.area.astype(float)
    water = water[water["area_m2"] >= 200.0].copy()
    return water


def _attach_width_metrics(water_lines: gpd.GeoDataFrame, observed_water: gpd.GeoDataFrame, width_source: str) -> gpd.GeoDataFrame:
    enriched = water_lines.to_crs(EUHYDRO_CRS).copy()
    for column in (
        "observed_width_m",
        "observed_water_area_m2",
        "river_length_m",
        "width_source",
        "discharge_m3s",
        "discharge_source",
        "discharge_date",
        "daily_flow_volume_m3",
        "quantity_score",
        "score_label",
    ):
        enriched[column] = np.nan if column not in {"width_source", "discharge_source", "discharge_date", "score_label"} else None

    if observed_water.empty:
        enriched["river_length_m"] = enriched.geometry.length.astype(float)
        enriched["width_source"] = "unavailable"
        enriched["score_label"] = "Width score (0-1, relative in this map)"
        return enriched

    water_union = unary_union(list(observed_water.geometry))
    widths: list[float] = []
    areas: list[float] = []
    lengths: list[float] = []
    sources: list[str] = []

    for geom in enriched.geometry:
        line_length = float(geom.length)
        lengths.append(line_length)
        if line_length <= 0.0:
            widths.append(np.nan)
            areas.append(0.0)
            sources.append("unavailable")
            continue

        corridor = geom.buffer(120.0, cap_style=2, join_style=2)
        water_in_corridor = water_union.intersection(corridor)
        if water_in_corridor.is_empty:
            widths.append(np.nan)
            areas.append(0.0)
            sources.append("unavailable")
            continue

        water_area = float(water_in_corridor.area)
        average_width = water_area / line_length if line_length > 0 else np.nan
        widths.append(average_width if average_width > 0 else np.nan)
        areas.append(water_area)
        sources.append(width_source)

    enriched["observed_width_m"] = widths
    enriched["observed_water_area_m2"] = areas
    enriched["river_length_m"] = lengths
    enriched["width_source"] = sources
    enriched["score_label"] = "Width score (0-1, relative in this map)"
    return enriched


def _attach_discharge_metrics(
    river_lines: gpd.GeoDataFrame,
    discharge_cache_path: Path,
    efas_days_back: int,
) -> tuple[gpd.GeoDataFrame, str | None]:
    enriched = river_lines.copy()
    try:
        discharge_grid, lats, lons, discharge_date = _fetch_efas_discharge_grid(discharge_cache_path, efas_days_back)
    except Exception:
        enriched["discharge_source"] = "unavailable"
        return enriched, None

    centroids = enriched.geometry.centroid
    centroids = gpd.GeoSeries(centroids, crs=enriched.crs).to_crs("EPSG:4326")
    discharges = []
    for point in centroids:
        row = int(np.argmin(np.abs(lats - point.y)))
        col = int(np.argmin(np.abs(lons - point.x)))
        value = float(discharge_grid[row, col])
        discharges.append(np.nan if not np.isfinite(value) else value)

    enriched["discharge_m3s"] = discharges
    enriched["discharge_source"] = "efas-historical"
    enriched["discharge_date"] = discharge_date
    enriched["daily_flow_volume_m3"] = enriched["discharge_m3s"] * 86400.0
    enriched["score_label"] = "Water quantity score (0-1, relative in this map)"
    return enriched, discharge_date


def _fetch_efas_discharge_grid(discharge_cache_path: Path, efas_days_back: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, str]:
    try:
        import cdsapi
    except ImportError as exc:
        raise RuntimeError("EFAS discharge requires the cdsapi package.") from exc

    try:
        from netCDF4 import Dataset
    except ImportError as exc:
        raise RuntimeError("EFAS discharge requires the netCDF4 package.") from exc

    target_date = date.today() - timedelta(days=max(efas_days_back, 7))
    request = {
        "system_version": ["version_5_0"],
        "variable": ["river_discharge_in_the_last_6_hours"],
        "model_levels": "surface_level",
        "hyear": [target_date.strftime("%Y")],
        "hmonth": [target_date.strftime("%m")],
        "hday": [target_date.strftime("%d")],
        "time": ["00:00"],
        "data_format": "netcdf",
        "download_format": "unarchived",
    }

    discharge_cache_path.parent.mkdir(parents=True, exist_ok=True)
    client = _build_ewds_client(cdsapi)
    client.retrieve("efas-historical", request).download(str(discharge_cache_path))

    with Dataset(discharge_cache_path) as ds:
        lat_name = _first_present(ds.variables, ("latitude", "lat"))
        lon_name = _first_present(ds.variables, ("longitude", "lon"))
        var_name = _infer_discharge_var_name(ds.variables.keys())
        lats = np.asarray(ds.variables[lat_name][:], dtype="float64")
        lons = np.asarray(ds.variables[lon_name][:], dtype="float64")
        discharge = np.asarray(ds.variables[var_name][:], dtype="float64").squeeze()
        if discharge.ndim != 2:
            raise RuntimeError(f"Unexpected EFAS discharge array shape: {discharge.shape}")
        fill_value = getattr(ds.variables[var_name], "_FillValue", None)
        if fill_value is not None:
            discharge = np.where(discharge == fill_value, np.nan, discharge)

    return discharge, lats, lons, target_date.isoformat()


def _first_present(variables, names: tuple[str, ...]) -> str:
    for name in names:
        if name in variables:
            return name
    raise RuntimeError(f"Could not find any of {names} in EFAS variables.")


def _infer_discharge_var_name(variable_names) -> str:
    preferred = ("dis06", "dis24", "river_discharge_in_the_last_6_hours", "river_discharge_in_the_last_24_hours")
    names = list(variable_names)
    for name in preferred:
        if name in names:
            return name
    for name in names:
        lowered = name.lower()
        if "dis" in lowered and lowered not in {"longitude", "latitude", "lon", "lat"}:
            return name
    raise RuntimeError(f"Could not infer EFAS discharge variable from {names}.")


def _attach_quantity_score(river_lines: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    enriched = river_lines.copy()
    width_values = np.log1p(np.clip(enriched["observed_width_m"].astype(float), a_min=0.0, a_max=None))
    discharge_values = np.log1p(np.clip(enriched["discharge_m3s"].astype(float), a_min=0.0, a_max=None))
    width_score = _normalize_series(width_values)
    discharge_score = _normalize_series(discharge_values)
    has_discharge = np.isfinite(discharge_score)
    has_width = np.isfinite(width_score)
    enriched["quantity_score"] = np.where(
        has_discharge | has_width,
        np.where(
            has_discharge,
            np.nan_to_num(width_score, nan=0.0) * 0.4 + np.nan_to_num(discharge_score, nan=0.0) * 0.6,
            width_score,
        ),
        np.nan,
    )
    enriched["score_label"] = np.where(
        has_discharge,
        "Water quantity score (0-1, relative in this map)",
        "Width score (0-1, relative in this map)",
    )
    return enriched


def _normalize_series(values: np.ndarray) -> np.ndarray:
    finite = np.isfinite(values)
    result = np.full(values.shape, np.nan, dtype="float64")
    if not finite.any():
        return result
    min_value = float(np.nanmin(values[finite]))
    max_value = float(np.nanmax(values[finite]))
    if max_value <= min_value:
        result[finite] = 1.0
        return result
    result[finite] = (values[finite] - min_value) / (max_value - min_value)
    return result


def _width_source_label(has_s1: bool, has_s2: bool) -> str:
    if has_s1 and has_s2:
        return "sentinel1+2"
    if has_s1:
        return "sentinel1"
    if has_s2:
        return "sentinel2"
    return "unavailable"


def _build_ewds_client(cdsapi_module):
    url = first_env_value(EWDS_API_URL_ENV_VARS)
    key = first_env_value(EWDS_API_KEY_ENV_VARS)
    kwargs = {"quiet": True, "progress": False}
    if url and key:
        kwargs.update({"url": url, "key": key})
    return cdsapi_module.Client(**kwargs)
