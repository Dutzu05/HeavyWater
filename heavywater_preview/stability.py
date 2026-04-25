from __future__ import annotations

import io
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import geopandas as gpd
import numpy as np
import pandas as pd
from pyproj import Transformer
from shapely import wkt
from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import unary_union

from heavywater_preview.config import EUHYDRO_CRS, EGMS_SOURCE_ENV_VARS, EGMS_TOKEN_ENV_VARS, WGS84_CRS
from heavywater_preview.copernicus import first_env_value


@dataclass
class StabilityResult:
    points_path: Path
    summary_path: Path
    summary: dict


def evaluate_structural_stability(
    *,
    bbox_wgs84: tuple[float, float, float, float],
    output_points_path: Path,
    output_summary_path: Path,
    egms_source: str | Path | None,
    buffer_m: float,
    differential_motion_threshold_mm_per_year: float,
    reservoir_site_wgs84: tuple[float, float] | None,
    canal_route_source: str | Path | None,
    fallback_river_lines: gpd.GeoDataFrame,
) -> StabilityResult:
    source = _resolve_egms_source(egms_source)
    points = load_egms_ortho_vertical_points(source)
    aoi = _bbox_to_euhydro_polygon(bbox_wgs84)
    clipped = points.clip(aoi)

    output_points_path.parent.mkdir(parents=True, exist_ok=True)
    if clipped.empty:
        clipped = gpd.GeoDataFrame(columns=["mean_velocity_mm_per_year", "geometry"], geometry="geometry", crs=EUHYDRO_CRS)
    clipped.to_file(output_points_path, driver="GeoJSON")

    reservoir_site = _build_reservoir_site(reservoir_site_wgs84, bbox_wgs84)
    canal_route, canal_source = _build_canal_route(canal_route_source, fallback_river_lines)
    reservoir_buffer = gpd.GeoSeries([reservoir_site.buffer(buffer_m)], crs=EUHYDRO_CRS)
    canal_buffer = gpd.GeoSeries([canal_route.buffer(buffer_m, cap_style=2, join_style=2)], crs=EUHYDRO_CRS) if canal_route is not None else None

    reservoir_points = _clip_to_geometry(clipped, reservoir_buffer.iloc[0])
    canal_points = _clip_to_geometry(clipped, canal_buffer.iloc[0]) if canal_buffer is not None else clipped.iloc[0:0].copy()
    combined_points = _combine_measurement_sets(reservoir_points, canal_points)

    reservoir_mean = _mean_velocity(reservoir_points)
    canal_mean = _mean_velocity(canal_points)
    combined_mean = _mean_velocity(combined_points)
    status, score = classify_stability(combined_mean)
    start_velocity, end_velocity, differential_motion = _endpoint_motion_stats(
        clipped,
        canal_route,
        buffer_m,
    )
    maintenance_note = None
    if differential_motion is not None and differential_motion > differential_motion_threshold_mm_per_year:
        maintenance_note = "High risk of joint separation and leaking due to differential ground motion."

    summary = {
        "dataset": "EGMS L3 Ortho Vertical",
        "source": str(source),
        "measurement_points_in_aoi": int(len(clipped)),
        "buffer_m": float(buffer_m),
        "reservoir_site_defaulted_to_aoi_center": reservoir_site_wgs84 is None,
        "canal_route_source": canal_source,
        "reservoir_measurement_points": int(len(reservoir_points)),
        "canal_measurement_points": int(len(canal_points)),
        "combined_measurement_points": int(len(combined_points)),
        "reservoir_v_mean_mm_per_year": reservoir_mean,
        "canal_v_mean_mm_per_year": canal_mean,
        "v_mean_mm_per_year": combined_mean,
        "stability_status": status,
        "stability_score": score,
        "canal_start_velocity_mm_per_year": start_velocity,
        "canal_end_velocity_mm_per_year": end_velocity,
        "differential_motion_mm_per_year": differential_motion,
        "differential_motion_threshold_mm_per_year": float(differential_motion_threshold_mm_per_year),
        "maintenance_note": maintenance_note,
    }
    output_summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return StabilityResult(points_path=output_points_path, summary_path=output_summary_path, summary=summary)


def load_egms_ortho_vertical_points(source: str | Path) -> gpd.GeoDataFrame:
    source_str = str(source)
    if _is_url(source_str):
        payload = _fetch_remote_bytes(source_str)
        suffix = Path(urlparse(source_str).path).suffix.lower()
        if suffix in {".csv", ".txt"}:
            return _load_egms_csv(io.BytesIO(payload))
        return _load_vector_bytes(payload, suffix)

    path = Path(source)
    suffix = path.suffix.lower()
    if suffix in {".csv", ".txt"}:
        with path.open("rb") as handle:
            return _load_egms_csv(handle)
    return _load_vector_file(path)


def classify_stability(v_mean_mm_per_year: float | None) -> tuple[str, int | None]:
    if v_mean_mm_per_year is None or not np.isfinite(v_mean_mm_per_year):
        return "STATUS: DATA UNAVAILABLE", None
    magnitude = abs(v_mean_mm_per_year)
    if magnitude < 2.0:
        return "STATUS: STABLE", 100
    if magnitude <= 5.0:
        return "STATUS: MONITORING REQUIRED", 60
    return "STATUS: HIGH RISK", 0


def _resolve_egms_source(egms_source: str | Path | None) -> str | Path:
    if egms_source:
        return egms_source
    env_source = first_env_value(EGMS_SOURCE_ENV_VARS)
    if env_source:
        return env_source
    raise RuntimeError(
        "Structural stability requires an EGMS Ortho Vertical CSV or GeoJSON source. "
        "Pass --egms-ortho-vertical or set EGMS_ORTHO_VERTICAL_SOURCE in .env."
    )


def _load_egms_csv(handle) -> gpd.GeoDataFrame:
    table = pd.read_csv(handle)
    table.columns = [str(column).strip() for column in table.columns]
    velocity_column = _find_column(table.columns, ("mean_velocity", "velocity", "vel", "avg_velocity"))
    if velocity_column is None:
        raise RuntimeError(f"Could not find EGMS velocity column in CSV headers: {list(table.columns)}")

    geometry = None
    easting_col = _find_column(table.columns, ("easting", "x"))
    northing_col = _find_column(table.columns, ("northing", "y"))
    lon_col = _find_column(table.columns, ("lon", "longitude"))
    lat_col = _find_column(table.columns, ("lat", "latitude"))
    wkt_col = _find_column(table.columns, ("geometry", "wkt"))

    if easting_col and northing_col:
        geometry = gpd.points_from_xy(table[easting_col], table[northing_col], crs=EUHYDRO_CRS)
    elif lon_col and lat_col:
        geometry = gpd.points_from_xy(table[lon_col], table[lat_col], crs=WGS84_CRS).to_crs(EUHYDRO_CRS)
    elif wkt_col:
        geometry = gpd.GeoSeries(table[wkt_col].map(wkt.loads), crs=EUHYDRO_CRS)
    else:
        raise RuntimeError("Could not infer EGMS CSV geometry columns. Expected easting/northing, lon/lat, or WKT geometry.")

    gdf = gpd.GeoDataFrame(table.copy(), geometry=geometry, crs=EUHYDRO_CRS)
    return _normalize_points_frame(gdf, velocity_column)


def _load_vector_file(path: Path) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(path)
    velocity_column = _find_column(gdf.columns, ("mean_velocity", "velocity", "vel", "avg_velocity"))
    if velocity_column is None:
        raise RuntimeError(f"Could not find EGMS velocity column in {path.name}.")
    if gdf.crs is None:
        gdf = gdf.set_crs(EUHYDRO_CRS)
    return _normalize_points_frame(gdf, velocity_column)


def _load_vector_bytes(payload: bytes, suffix: str) -> gpd.GeoDataFrame:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir) / f"egms_source{suffix or '.geojson'}"
        temp_path.write_bytes(payload)
        return _load_vector_file(temp_path)


def _normalize_points_frame(gdf: gpd.GeoDataFrame, velocity_column: str) -> gpd.GeoDataFrame:
    frame = gdf.to_crs(EUHYDRO_CRS).copy()
    frame = frame[frame.geometry.notna() & ~frame.geometry.is_empty].copy()
    frame["mean_velocity_mm_per_year"] = pd.to_numeric(frame[velocity_column], errors="coerce")
    frame = frame[np.isfinite(frame["mean_velocity_mm_per_year"])].copy()
    return frame[["mean_velocity_mm_per_year", "geometry"]]


def _find_column(columns, candidates: tuple[str, ...]) -> str | None:
    normalized = {str(column).strip().lower(): str(column) for column in columns}
    for candidate in candidates:
        if candidate in normalized:
            return normalized[candidate]
    for column in columns:
        lowered = str(column).strip().lower()
        for candidate in candidates:
            if candidate in lowered:
                return str(column)
    return None


def _fetch_remote_bytes(url: str) -> bytes:
    headers = {"Accept": "application/json,text/csv,*/*"}
    token = first_env_value(EGMS_TOKEN_ENV_VARS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    with urlopen(request, timeout=120) as response:
        return response.read()


def _is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _bbox_to_euhydro_polygon(bbox_wgs84: tuple[float, float, float, float]):
    from heavywater_preview.aoi import reproject_bounds_to_euhydro

    return reproject_bounds_to_euhydro(bbox_wgs84)


def _build_reservoir_site(
    reservoir_site_wgs84: tuple[float, float] | None,
    bbox_wgs84: tuple[float, float, float, float],
) -> Point:
    if reservoir_site_wgs84 is None:
        min_lon, min_lat, max_lon, max_lat = bbox_wgs84
        lon = (min_lon + max_lon) / 2.0
        lat = (min_lat + max_lat) / 2.0
    else:
        lat, lon = reservoir_site_wgs84
    transformer = Transformer.from_crs(WGS84_CRS, EUHYDRO_CRS, always_xy=True)
    x, y = transformer.transform(lon, lat)
    return Point(x, y)


def _build_canal_route(
    canal_route_source: str | Path | None,
    fallback_river_lines: gpd.GeoDataFrame,
) -> tuple[LineString | MultiLineString | None, str]:
    if canal_route_source:
        route = gpd.read_file(canal_route_source)
        if route.crs is None:
            route = route.set_crs(EUHYDRO_CRS)
        route = route.to_crs(EUHYDRO_CRS)
        route = route[route.geometry.notna() & ~route.geometry.is_empty].copy()
        line_geometries = [geom for geom in route.geometry if geom.geom_type in {"LineString", "MultiLineString"}]
        if not line_geometries:
            raise RuntimeError("The provided canal route file does not contain line geometry.")
        return unary_union(line_geometries), str(canal_route_source)

    if fallback_river_lines.empty:
        return None, "unavailable"

    candidate = fallback_river_lines.to_crs(EUHYDRO_CRS).copy()
    candidate["length_m"] = candidate.geometry.length
    longest = candidate.sort_values("length_m", ascending=False).geometry.iloc[0]
    return longest, "fallback_longest_water_line"


def _clip_to_geometry(points: gpd.GeoDataFrame, geometry) -> gpd.GeoDataFrame:
    if points.empty:
        return points.copy()
    mask = points.geometry.intersects(geometry)
    return points.loc[mask].copy()


def _combine_measurement_sets(left: gpd.GeoDataFrame, right: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if left.empty and right.empty:
        return left.iloc[0:0].copy()
    combined = pd.concat([left, right], ignore_index=True)
    result = gpd.GeoDataFrame(combined, geometry="geometry", crs=EUHYDRO_CRS)
    result["_geometry_wkb"] = result.geometry.to_wkb()
    result = result.drop_duplicates(subset=["_geometry_wkb", "mean_velocity_mm_per_year"]).drop(columns="_geometry_wkb")
    return result


def _mean_velocity(points: gpd.GeoDataFrame) -> float | None:
    if points.empty:
        return None
    value = float(points["mean_velocity_mm_per_year"].mean())
    return value if np.isfinite(value) else None


def _endpoint_motion_stats(
    points: gpd.GeoDataFrame,
    canal_route: LineString | MultiLineString | None,
    buffer_m: float,
) -> tuple[float | None, float | None, float | None]:
    if canal_route is None or points.empty:
        return None, None, None

    start_point = Point(canal_route.geoms[0].coords[0]) if isinstance(canal_route, MultiLineString) else Point(canal_route.coords[0])
    end_line = canal_route.geoms[-1] if isinstance(canal_route, MultiLineString) else canal_route
    end_point = Point(end_line.coords[-1])

    start_points = _clip_to_geometry(points, start_point.buffer(buffer_m))
    end_points = _clip_to_geometry(points, end_point.buffer(buffer_m))
    start_velocity = _mean_velocity(start_points)
    end_velocity = _mean_velocity(end_points)
    if start_velocity is None or end_velocity is None:
        return start_velocity, end_velocity, None
    return start_velocity, end_velocity, abs(start_velocity - end_velocity)
