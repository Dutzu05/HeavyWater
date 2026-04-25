from __future__ import annotations

import heapq
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from pyproj import Transformer
from rasterio.transform import rowcol, xy
from rasterio.vrt import WarpedVRT
from shapely.geometry import LineString, Point

from heavywater_preview.aoi import reproject_bounds_to_euhydro
from heavywater_preview.config import EUHYDRO_CRS, WGS84_CRS
from heavywater_preview.soil import classify_seepage_risk, query_soilgrids_textures
from heavywater_preview.stability import classify_stability, load_egms_ortho_vertical_points


@dataclass
class DecisionOutputs:
    canals: gpd.GeoDataFrame
    sites: gpd.GeoDataFrame
    summary_rows: list[dict]


def evaluate_water_infrastructure(
    *,
    high_risk_points: gpd.GeoDataFrame,
    water_sources: gpd.GeoDataFrame,
    terrain_dem_raster: Path,
    bbox_wgs84: tuple[float, float, float, float],
    egms_ortho_vertical: str | Path | None,
    stability_buffer_m: float,
    differential_motion_threshold_mm_per_year: float,
) -> DecisionOutputs:
    if high_risk_points.empty:
        return DecisionOutputs(
            canals=_empty_canals(),
            sites=_empty_sites(),
            summary_rows=[],
        )

    aoi_polygon = reproject_bounds_to_euhydro(bbox_wgs84)
    egms_points = _load_egms_points(egms_ortho_vertical)
    canal_features: list[dict] = []
    site_features: list[dict] = []
    summary_rows: list[dict] = []
    soil_context = {"enabled": True, "cache": {}}

    with rasterio.open(terrain_dem_raster) as src:
        with WarpedVRT(src, crs=EUHYDRO_CRS) as vrt:
            dem = vrt.read(1, masked=True).astype("float32").filled(np.nan)
            transform = vrt.transform
            slope = _compute_slope_degrees(dem, transform)
            basin_candidates = _find_basin_candidates(dem, transform, aoi_polygon)

            for _, demand in high_risk_points.iterrows():
                if pd.isna(demand.get("supply_sample_x")) or pd.isna(demand.get("supply_sample_y")):
                    continue
                supply_point = Point(float(demand["supply_sample_x"]), float(demand["supply_sample_y"]))
                demand_point = _analysis_point(demand.geometry)

                canal_option = _evaluate_canal_option(
                    demand=demand,
                    demand_point=demand_point,
                    supply_point=supply_point,
                    dem=dem,
                    transform=transform,
                    egms_points=egms_points,
                    soil_context=soil_context,
                    stability_buffer_m=stability_buffer_m,
                )
                reservoir_option = _evaluate_reservoir_option(
                    demand=demand,
                    demand_point=demand_point,
                    supply_point=supply_point,
                    basin_candidates=basin_candidates,
                    dem=dem,
                    slope=slope,
                    transform=transform,
                    aoi_polygon=aoi_polygon,
                    egms_points=egms_points,
                    soil_context=soil_context,
                    stability_buffer_m=stability_buffer_m,
                    differential_motion_threshold_mm_per_year=differential_motion_threshold_mm_per_year,
                )
                decision_name, decision_reason = _choose_decision(canal_option, reservoir_option)

                if decision_name in {"BUILD CANAL", "BUILD RESERVOIR + FEED CANAL"} and canal_option["geometry"] is not None:
                    canal_features.append(
                        {
                            "demand_id": demand.get("demand_id"),
                            "decision": decision_name,
                            "decision_reason": decision_reason,
                            "option_score": canal_option["score"],
                            "risk_status": demand.get("water_risk"),
                            "distance_to_source_m": demand.get("distance_to_source_m"),
                            "canal_length_m": canal_option["length_m"],
                            "gravity_feasibility_pct": canal_option["gravity_feasibility_pct"],
                            "elevation_drop_m": canal_option["elevation_drop_m"],
                            "mean_route_slope_deg": canal_option["mean_route_slope_deg"],
                            "max_route_slope_deg": canal_option["max_route_slope_deg"],
                            "terrain_behavior": canal_option["terrain_behavior"],
                            "route_ksat_mm_per_hour": canal_option["route_ksat_mm_per_hour"],
                            "route_seepage_class": canal_option["route_seepage_class"],
                            "route_soil_behavior": canal_option["route_soil_behavior"],
                            "canal_stability_status": canal_option["stability_status"],
                            "canal_v_mean_mm_per_year": canal_option["stability_velocity_mm_per_year"],
                            "geometry": canal_option["geometry"],
                        }
                    )

                if decision_name in {"BUILD RESERVOIR", "BUILD RESERVOIR + FEED CANAL"} and reservoir_option["geometry"] is not None:
                    site_features.append(
                        {
                            "demand_id": demand.get("demand_id"),
                            "site_type": "reservoir_site",
                            "decision": decision_name,
                            "decision_reason": decision_reason,
                            "option_score": reservoir_option["score"],
                            "risk_status": demand.get("water_risk"),
                            "distance_to_source_m": reservoir_option["distance_to_source_m"],
                            "distance_to_demand_m": reservoir_option["distance_to_demand_m"],
                            "gravity_feasibility_pct": reservoir_option["feed_gravity_feasibility_pct"],
                            "feed_canal_length_m": reservoir_option["feed_canal_length_m"],
                            "stability_status": reservoir_option["stability_status"],
                            "stability_score": reservoir_option["stability_score"],
                            "stability_velocity_mm_per_year": reservoir_option["stability_velocity_mm_per_year"],
                            "ksat_mm_per_hour": reservoir_option["ksat_mm_per_hour"],
                            "seepage_class": reservoir_option["seepage_class"],
                            "engineering_note": reservoir_option["engineering_note"],
                            "basin_depth_m": reservoir_option["basin_depth_m"],
                            "local_slope_deg": reservoir_option["local_slope_deg"],
                            "geometry": reservoir_option["geometry"],
                        }
                    )

                summary_rows.append(
                    {
                        "demand_id": demand.get("demand_id"),
                        "decision": decision_name,
                        "decision_reason": decision_reason,
                        "canal_score": canal_option["score"],
                        "reservoir_score": reservoir_option["score"],
                        "canal_length_m": canal_option["length_m"],
                        "canal_gravity_feasibility_pct": canal_option["gravity_feasibility_pct"],
                        "canal_elevation_drop_m": canal_option["elevation_drop_m"],
                        "canal_mean_route_slope_deg": canal_option["mean_route_slope_deg"],
                        "canal_route_ksat_mm_per_hour": canal_option["route_ksat_mm_per_hour"],
                        "canal_route_seepage_class": canal_option["route_seepage_class"],
                        "reservoir_basin_depth_m": reservoir_option["basin_depth_m"],
                        "reservoir_distance_to_demand_m": reservoir_option["distance_to_demand_m"],
                        "reservoir_distance_to_source_m": reservoir_option["distance_to_source_m"],
                        "reservoir_ksat_mm_per_hour": reservoir_option["ksat_mm_per_hour"],
                        "reservoir_stability_status": reservoir_option["stability_status"],
                    }
                )

    canals = gpd.GeoDataFrame(canal_features, geometry="geometry", crs=EUHYDRO_CRS) if canal_features else _empty_canals()
    sites = gpd.GeoDataFrame(site_features, geometry="geometry", crs=EUHYDRO_CRS) if site_features else _empty_sites()
    return DecisionOutputs(canals=canals, sites=sites, summary_rows=summary_rows)


def _evaluate_canal_option(
    *,
    demand,
    demand_point: Point,
    supply_point: Point,
    dem: np.ndarray,
    transform,
    egms_points: gpd.GeoDataFrame | None,
    soil_context: dict,
    stability_buffer_m: float,
) -> dict:
    canal_line, gravity_pct = _least_cost_canal_path(dem, transform, supply_point, demand_point)
    if canal_line is None:
        return {
            "geometry": None,
            "score": 0.0,
            "length_m": None,
            "gravity_feasibility_pct": None,
            "stability_status": None,
            "stability_velocity_mm_per_year": None,
        }

    length_m = float(canal_line.length)
    stability_velocity, stability_status, _ = _line_stability(canal_line, egms_points, stability_buffer_m)
    elevation_start_m = _sample_raster_value(dem, transform, supply_point)
    elevation_end_m = _sample_raster_value(dem, transform, demand_point)
    elevation_drop_m = None
    if elevation_start_m is not None and elevation_end_m is not None:
        elevation_drop_m = float(elevation_start_m - elevation_end_m)
    mean_route_slope_deg, max_route_slope_deg = _line_slope_stats(canal_line, dem, transform)
    terrain_behavior = _describe_canal_terrain(
        gravity_pct=gravity_pct,
        elevation_drop_m=elevation_drop_m,
        mean_route_slope_deg=mean_route_slope_deg,
    )
    route_ksat_mm_per_hour, route_seepage_class, route_soil_behavior = _line_soil_summary(canal_line, soil_context)
    gravity_score = _linear_score(gravity_pct or 0.0, low=30.0, high=95.0)
    length_score = _inverse_score(length_m, low=1200.0, high=6000.0)
    stability_score = _stability_numeric_score(stability_status)
    score = round(0.5 * gravity_score + 0.25 * length_score + 0.25 * stability_score, 1)
    return {
        "geometry": canal_line,
        "score": score,
        "length_m": length_m,
        "gravity_feasibility_pct": gravity_pct,
        "elevation_drop_m": elevation_drop_m,
        "mean_route_slope_deg": mean_route_slope_deg,
        "max_route_slope_deg": max_route_slope_deg,
        "terrain_behavior": terrain_behavior,
        "route_ksat_mm_per_hour": route_ksat_mm_per_hour,
        "route_seepage_class": route_seepage_class,
        "route_soil_behavior": route_soil_behavior,
        "stability_status": stability_status,
        "stability_velocity_mm_per_year": stability_velocity,
    }


def _evaluate_reservoir_option(
    *,
    demand,
    demand_point: Point,
    supply_point: Point,
    basin_candidates: list[Point],
    dem: np.ndarray,
    slope: np.ndarray,
    transform,
    aoi_polygon,
    egms_points: gpd.GeoDataFrame | None,
    soil_context: dict,
    stability_buffer_m: float,
    differential_motion_threshold_mm_per_year: float,
) -> dict:
    ordered = sorted(
        basin_candidates,
        key=lambda point: demand_point.distance(point) + 0.6 * supply_point.distance(point),
    )[:8]
    best = _empty_reservoir_option()
    transformer = Transformer.from_crs(EUHYDRO_CRS, WGS84_CRS, always_xy=True)

    for candidate in ordered:
        feed_line, feed_gravity = _least_cost_canal_path(dem, transform, supply_point, candidate)
        if feed_line is None:
            continue
        basin_depth_m = _local_basin_depth(dem, transform, candidate)
        local_slope_deg = _local_mean_value(slope, transform, candidate, radius_m=220.0)
        distance_to_demand_m = float(candidate.distance(demand_point))
        distance_to_source_m = float(candidate.distance(supply_point))
        lon, lat = transformer.transform(candidate.x, candidate.y)

        ksat_mm_per_hour, seepage_class, engineering_note = _soil_snapshot(lat, lon, soil_context)

        stability_velocity, stability_status, stability_score = _point_stability(candidate, egms_points, stability_buffer_m)
        if stability_velocity is not None and abs(stability_velocity) > differential_motion_threshold_mm_per_year and stability_status == "STATUS: MONITORING REQUIRED":
            stability_status = "STATUS: HIGH RISK"
            stability_score = 0

        depth_score = _linear_score(basin_depth_m, low=2.0, high=18.0)
        slope_score = _inverse_score(local_slope_deg, low=4.0, high=18.0)
        soil_score = _soil_numeric_score(ksat_mm_per_hour)
        feed_score = _linear_score(feed_gravity or 0.0, low=35.0, high=95.0)
        source_distance_score = _inverse_score(distance_to_source_m, low=400.0, high=3500.0)
        demand_distance_score = _inverse_score(distance_to_demand_m, low=300.0, high=3000.0)
        stability_numeric = float(stability_score or 0)
        score = round(
            0.25 * depth_score
            + 0.15 * slope_score
            + 0.20 * soil_score
            + 0.15 * feed_score
            + 0.10 * source_distance_score
            + 0.05 * demand_distance_score
            + 0.10 * stability_numeric,
            1,
        )

        footprint_radius_m = float(np.clip(160.0 + max(basin_depth_m, 0.0) * 12.0, 150.0, 420.0))
        footprint = candidate.buffer(footprint_radius_m).intersection(aoi_polygon)
        if footprint.is_empty:
            continue

        option = {
            "geometry": footprint,
            "point_geometry": candidate,
            "score": score,
            "distance_to_source_m": distance_to_source_m,
            "distance_to_demand_m": distance_to_demand_m,
            "feed_gravity_feasibility_pct": feed_gravity,
            "feed_canal_length_m": float(feed_line.length),
            "stability_status": stability_status,
            "stability_score": stability_score,
            "stability_velocity_mm_per_year": stability_velocity,
            "ksat_mm_per_hour": ksat_mm_per_hour,
            "seepage_class": seepage_class,
            "engineering_note": engineering_note,
            "basin_depth_m": basin_depth_m,
            "local_slope_deg": local_slope_deg,
        }
        if option["score"] > best["score"]:
            best = option

    return best


def _choose_decision(canal_option: dict, reservoir_option: dict) -> tuple[str, str]:
    canal_score = float(canal_option.get("score") or 0.0)
    reservoir_score = float(reservoir_option.get("score") or 0.0)
    basin_depth = float(reservoir_option.get("basin_depth_m") or 0.0)
    soil_ok = reservoir_option.get("ksat_mm_per_hour") is not None and float(reservoir_option["ksat_mm_per_hour"]) <= 20.0
    stability_ok = reservoir_option.get("stability_status") not in {"STATUS: HIGH RISK"}
    feed_ok = (reservoir_option.get("feed_gravity_feasibility_pct") or 0.0) >= 55.0

    if reservoir_score >= 68.0 and basin_depth >= 6.0 and stability_ok and soil_ok and feed_ok:
        return "BUILD RESERVOIR + FEED CANAL", "Valley-like basin, acceptable soil permeability, stable ground, and a gravity-fed supply path."
    if canal_score >= 62.0 and canal_score >= reservoir_score + 8.0:
        return "BUILD CANAL", "Direct conveyance scores better than reservoir storage for this target."
    if reservoir_score >= 62.0 and basin_depth >= 5.0 and stability_ok:
        return "BUILD RESERVOIR", "Terrain forms a usable basin and geotechnical screening is acceptable."
    if canal_score >= 52.0 and reservoir_score >= 55.0 and feed_ok and stability_ok:
        return "BUILD RESERVOIR + FEED CANAL", "Both conveyance and storage are feasible, so storing transferred water is the stronger option."
    if canal_score >= 50.0:
        return "BUILD CANAL", "Canal geometry remains workable even though the reservoir option is weaker."
    if reservoir_score >= 50.0 and stability_ok:
        return "BUILD RESERVOIR", "A contained basin is the least-bad option inside the study area."
    return "NO CLEAR OPTION", "Terrain, soil, or stability constraints are too weak for a confident canal or reservoir recommendation."


def _find_basin_candidates(dem: np.ndarray, transform, aoi_polygon, max_candidates: int = 80) -> list[Point]:
    finite = np.isfinite(dem)
    if not finite.any():
        return []
    from scipy.ndimage import minimum_filter

    working = np.where(finite, dem, np.nanmax(dem[finite]) + 1000.0)
    local_min = working <= minimum_filter(working, size=9, mode="nearest")
    rows, cols = np.where(local_min & finite)
    if rows.size == 0:
        return []

    order = np.argsort(working[rows, cols])
    candidates: list[Point] = []
    for idx in order:
        x, y = xy(transform, int(rows[idx]), int(cols[idx]))
        point = Point(float(x), float(y))
        if not aoi_polygon.contains(point):
            continue
        candidates.append(point)
        if len(candidates) >= max_candidates:
            break
    return candidates


def _local_basin_depth(dem: np.ndarray, transform, point: Point, inner_radius_m: float = 120.0, outer_radius_m: float = 420.0) -> float:
    center_value = _sample_raster_value(dem, transform, point)
    ring_mean = _local_mean_value(dem, transform, point, radius_m=outer_radius_m, min_radius_m=inner_radius_m)
    if center_value is None or ring_mean is None:
        return 0.0
    return max(float(ring_mean - center_value), 0.0)


def _local_mean_value(
    values: np.ndarray,
    transform,
    point: Point,
    *,
    radius_m: float,
    min_radius_m: float = 0.0,
) -> float | None:
    try:
        row, col = rowcol(transform, point.x, point.y)
    except Exception:
        return None
    yres = max(abs(transform.e), 1.0)
    xres = max(abs(transform.a), 1.0)
    row_radius = max(int(np.ceil(radius_m / yres)), 1)
    col_radius = max(int(np.ceil(radius_m / xres)), 1)

    row_min = max(0, row - row_radius)
    row_max = min(values.shape[0], row + row_radius + 1)
    col_min = max(0, col - col_radius)
    col_max = min(values.shape[1], col + col_radius + 1)
    window = values[row_min:row_max, col_min:col_max]
    if window.size == 0:
        return None

    rows = np.arange(row_min, row_max) - row
    cols = np.arange(col_min, col_max) - col
    rr, cc = np.meshgrid(rows, cols, indexing="ij")
    distances = np.sqrt((rr * yres) ** 2 + (cc * xres) ** 2)
    mask = distances <= radius_m
    if min_radius_m > 0.0:
        mask &= distances >= min_radius_m
    masked_values = window[mask]
    finite_values = masked_values[np.isfinite(masked_values)]
    if finite_values.size == 0:
        return None
    return float(np.mean(finite_values))


def _sample_raster_value(values: np.ndarray, transform, point: Point) -> float | None:
    try:
        row, col = rowcol(transform, point.x, point.y)
    except Exception:
        return None
    if row < 0 or col < 0 or row >= values.shape[0] or col >= values.shape[1]:
        return None
    value = values[row, col]
    if not np.isfinite(value):
        return None
    return float(value)


def _compute_slope_degrees(dem: np.ndarray, transform) -> np.ndarray:
    xres = max(abs(transform.a), 1.0)
    yres = max(abs(transform.e), 1.0)
    finite = np.isfinite(dem)
    if not finite.any():
        return np.full_like(dem, np.nan, dtype="float32")
    filled = np.where(finite, dem, np.nanmedian(dem[finite]))
    grad_y, grad_x = np.gradient(filled, yres, xres)
    slope = np.rad2deg(np.arctan(np.hypot(grad_x, grad_y)))
    slope[~finite] = np.nan
    return slope.astype("float32")


def _analysis_point(geometry) -> Point:
    if geometry.geom_type == "Point":
        return geometry
    point = geometry.representative_point()
    return Point(float(point.x), float(point.y))


def _least_cost_canal_path(dem: np.ndarray, transform, source_point: Point, target_point: Point) -> tuple[LineString | None, float | None]:
    finite = np.isfinite(dem)
    if not finite.any():
        return None, None
    try:
        source_row, source_col = rowcol(transform, source_point.x, source_point.y)
        target_row, target_col = rowcol(transform, target_point.x, target_point.y)
    except Exception:
        return None, None

    nrows, ncols = dem.shape
    if not (0 <= source_row < nrows and 0 <= source_col < ncols and 0 <= target_row < nrows and 0 <= target_col < ncols):
        return None, None

    distances = np.full((nrows, ncols), np.inf, dtype="float64")
    previous: dict[tuple[int, int], tuple[int, int] | None] = {(source_row, source_col): None}
    heap: list[tuple[float, int, int]] = [(0.0, source_row, source_col)]
    distances[source_row, source_col] = 0.0
    neighbors = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]

    while heap:
        cost, row, col = heapq.heappop(heap)
        if cost > distances[row, col]:
            continue
        if (row, col) == (target_row, target_col):
            break
        current_elev = dem[row, col]
        if not np.isfinite(current_elev):
            continue
        for drow, dcol in neighbors:
            nr, nc = row + drow, col + dcol
            if nr < 0 or nc < 0 or nr >= nrows or nc >= ncols:
                continue
            next_elev = dem[nr, nc]
            if not np.isfinite(next_elev):
                continue
            step_dist = float(np.hypot(drow, dcol))
            uphill = max(0.0, next_elev - current_elev)
            step_cost = step_dist * (1.0 + uphill * 0.25)
            new_cost = cost + step_cost
            if new_cost < distances[nr, nc]:
                distances[nr, nc] = new_cost
                previous[(nr, nc)] = (row, col)
                heapq.heappush(heap, (new_cost, nr, nc))

    if not np.isfinite(distances[target_row, target_col]):
        return None, None

    path_cells = []
    cursor = (target_row, target_col)
    while cursor is not None:
        path_cells.append(cursor)
        cursor = previous.get(cursor)
    path_cells.reverse()
    if len(path_cells) < 2:
        return None, 100.0
    coords = [xy(transform, row, col) for row, col in path_cells]
    line = LineString([(float(x), float(y)) for x, y in coords])
    uphill_steps = 0
    for (row_a, col_a), (row_b, col_b) in zip(path_cells[:-1], path_cells[1:]):
        elev_a = dem[row_a, col_a]
        elev_b = dem[row_b, col_b]
        if np.isfinite(elev_a) and np.isfinite(elev_b) and elev_b > elev_a:
            uphill_steps += 1
    gravity_feasibility = max(0.0, 100.0 * (1.0 - uphill_steps / max(len(path_cells) - 1, 1)))
    return line, round(gravity_feasibility, 1)


def _load_egms_points(egms_ortho_vertical: str | Path | None) -> gpd.GeoDataFrame | None:
    if not egms_ortho_vertical:
        return None
    try:
        points = load_egms_ortho_vertical_points(egms_ortho_vertical)
        if points.empty:
            return None
        return points.to_crs(EUHYDRO_CRS)
    except Exception:
        return None


def _point_stability(point: Point, egms_points: gpd.GeoDataFrame | None, buffer_m: float) -> tuple[float | None, str | None, int | None]:
    if egms_points is None or egms_points.empty:
        return None, None, None
    nearby = egms_points[egms_points.geometry.intersects(point.buffer(buffer_m))]
    if nearby.empty:
        return None, None, None
    velocity = float(nearby["mean_velocity_mm_per_year"].mean())
    status, score = classify_stability(velocity)
    return velocity, status, score


def _line_stability(line: LineString, egms_points: gpd.GeoDataFrame | None, buffer_m: float) -> tuple[float | None, str | None, int | None]:
    if egms_points is None or egms_points.empty:
        return None, None, None
    nearby = egms_points[egms_points.geometry.intersects(line.buffer(buffer_m, cap_style=2, join_style=2))]
    if nearby.empty:
        return None, None, None
    velocity = float(nearby["mean_velocity_mm_per_year"].mean())
    status, score = classify_stability(velocity)
    return velocity, status, score


def _line_slope_stats(line: LineString, dem: np.ndarray, transform) -> tuple[float | None, float | None]:
    points = _sample_line_points(line, target_count=9)
    slopes = []
    for point in points:
        slope = _local_slope_at_point(point, dem, transform)
        if slope is not None and np.isfinite(slope):
            slopes.append(float(slope))
    if not slopes:
        return None, None
    return float(np.mean(slopes)), float(np.max(slopes))


def _line_soil_summary(line: LineString, soil_context: dict) -> tuple[float | None, str, str]:
    transformer = Transformer.from_crs(EUHYDRO_CRS, WGS84_CRS, always_xy=True)
    points = _sample_line_points(line, target_count=5)
    ksat_values: list[float] = []
    seepage_labels: list[str] = []

    for point in points:
        lon, lat = transformer.transform(point.x, point.y)
        ksat_mm_per_hour, seepage_class, _ = _soil_snapshot(lat, lon, soil_context)
        if ksat_mm_per_hour is not None and np.isfinite(ksat_mm_per_hour):
            ksat_values.append(float(ksat_mm_per_hour))
        if seepage_class and seepage_class != "Unavailable":
            seepage_labels.append(str(seepage_class))

    avg_ksat = float(np.mean(ksat_values)) if ksat_values else None
    dominant_seepage = _mode_label(seepage_labels) or "Unavailable"
    behavior = _describe_canal_soil(avg_ksat, dominant_seepage)
    return avg_ksat, dominant_seepage, behavior


def _soil_numeric_score(ksat_mm_per_hour: float | None) -> float:
    if ksat_mm_per_hour is None or not np.isfinite(ksat_mm_per_hour):
        return 35.0
    seepage_class, _ = classify_seepage_risk(float(ksat_mm_per_hour))
    if seepage_class == "Low Seepage":
        return 100.0
    if seepage_class == "Medium Seepage":
        return 60.0
    return 10.0


def _soil_snapshot(lat: float, lon: float, soil_context: dict) -> tuple[float | None, str, str]:
    cache_key = (round(lat, 5), round(lon, 5))
    cache = soil_context["cache"]
    if cache_key in cache:
        return cache[cache_key]
    if not soil_context.get("enabled", True):
        return (None, "Unavailable", "Soil permeability estimate unavailable for this point.")

    try:
        soil = query_soilgrids_textures(lat, lon)
        result = (soil.ksat_mm_per_hour, soil.seepage_class, soil.engineering_note)
        cache[cache_key] = result
        return result
    except Exception:
        soil_context["enabled"] = False
        return (None, "Unavailable", "Soil permeability estimate unavailable for this point.")


def _sample_line_points(line: LineString, target_count: int) -> list[Point]:
    if target_count <= 1 or line.length <= 0:
        return [Point(line.coords[0])]
    distances = np.linspace(0.0, line.length, num=target_count)
    return [line.interpolate(float(distance)) for distance in distances]


def _local_slope_at_point(point: Point, dem: np.ndarray, transform) -> float | None:
    return _local_mean_value(_compute_slope_degrees(dem, transform), transform, point, radius_m=120.0)


def _describe_canal_terrain(*, gravity_pct: float | None, elevation_drop_m: float | None, mean_route_slope_deg: float | None) -> str:
    if gravity_pct is None:
        return "Terrain analysis unavailable along this route."
    if gravity_pct >= 80.0 and (elevation_drop_m is None or elevation_drop_m >= 0.0):
        return "Mostly gravity-fed route with favorable downhill terrain."
    if gravity_pct >= 60.0:
        return "Terrain is workable, but some route sections fight the slope."
    if mean_route_slope_deg is not None and mean_route_slope_deg >= 12.0:
        return "Steep terrain raises excavation and control complexity."
    return "Terrain support is mixed and would need closer engineering review."


def _describe_canal_soil(avg_ksat_mm_per_hour: float | None, seepage_class: str) -> str:
    if avg_ksat_mm_per_hour is None:
        return "Soil permeability along the route is unavailable."
    if seepage_class == "Low Seepage":
        return "Route crosses tighter soils with lower seepage risk."
    if seepage_class == "Medium Seepage":
        return "Route crosses moderately permeable soils; lining or compaction may be needed in sections."
    if seepage_class == "High Seepage":
        return "Route crosses permeable soils, so canal lining would likely be required."
    return "Soil behavior along the route is mixed."


def _mode_label(values: list[str]) -> str | None:
    if not values:
        return None
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return max(counts, key=counts.get)


def _stability_numeric_score(status: str | None) -> float:
    if status == "STATUS: STABLE":
        return 100.0
    if status == "STATUS: MONITORING REQUIRED":
        return 60.0
    if status == "STATUS: HIGH RISK":
        return 0.0
    return 40.0


def _linear_score(value: float | None, *, low: float, high: float) -> float:
    if value is None or not np.isfinite(value):
        return 0.0
    if value <= low:
        return 0.0
    if value >= high:
        return 100.0
    return float((value - low) / (high - low) * 100.0)


def _inverse_score(value: float | None, *, low: float, high: float) -> float:
    if value is None or not np.isfinite(value):
        return 0.0
    if value <= low:
        return 100.0
    if value >= high:
        return 0.0
    return float((high - value) / (high - low) * 100.0)


def _empty_canals() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        columns=[
            "demand_id",
            "decision",
            "decision_reason",
            "option_score",
            "risk_status",
            "distance_to_source_m",
            "canal_length_m",
            "gravity_feasibility_pct",
            "elevation_drop_m",
            "mean_route_slope_deg",
            "max_route_slope_deg",
            "terrain_behavior",
            "route_ksat_mm_per_hour",
            "route_seepage_class",
            "route_soil_behavior",
            "canal_stability_status",
            "canal_v_mean_mm_per_year",
            "geometry",
        ],
        geometry="geometry",
        crs=EUHYDRO_CRS,
    )


def _empty_sites() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        columns=[
            "demand_id",
            "site_type",
            "decision",
            "decision_reason",
            "option_score",
            "risk_status",
            "distance_to_source_m",
            "distance_to_demand_m",
            "gravity_feasibility_pct",
            "feed_canal_length_m",
            "stability_status",
            "stability_score",
            "stability_velocity_mm_per_year",
            "ksat_mm_per_hour",
            "seepage_class",
            "engineering_note",
            "basin_depth_m",
            "local_slope_deg",
            "geometry",
        ],
        geometry="geometry",
        crs=EUHYDRO_CRS,
    )


def _empty_reservoir_option() -> dict:
    return {
        "geometry": None,
        "point_geometry": None,
        "score": 0.0,
        "distance_to_source_m": None,
        "distance_to_demand_m": None,
        "feed_gravity_feasibility_pct": None,
        "feed_canal_length_m": None,
        "stability_status": None,
        "stability_score": None,
        "stability_velocity_mm_per_year": None,
        "ksat_mm_per_hour": None,
        "seepage_class": "Unavailable",
        "engineering_note": "Soil permeability estimate unavailable for this point.",
        "basin_depth_m": None,
        "local_slope_deg": None,
    }
