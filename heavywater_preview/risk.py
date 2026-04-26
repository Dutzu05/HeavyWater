from __future__ import annotations

import json
import hashlib
import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from netCDF4 import Dataset
from pyproj import Transformer
from shapely.geometry import Point
from shapely.geometry import LineString
from shapely.ops import nearest_points

from heavywater_preview.config import (
    DEFAULT_DIFFERENTIAL_MOTION_THRESHOLD,
    EUHYDRO_CRS,
    WATER_RISK_CANALS_NAME,
    WATER_RISK_POINTS_NAME,
    WATER_RISK_SITES_NAME,
    WATER_RISK_SUMMARY_NAME,
    WGS84_CRS,
)
from heavywater_preview.decision import evaluate_water_infrastructure
from heavywater_preview.river_metrics import _build_ewds_client, _first_present, _infer_discharge_var_name
from heavywater_preview.soil import classify_seepage_risk, estimate_ksat_mm_per_hour


@dataclass
class WaterRiskResult:
    risk_points_path: Path
    canals_path: Path
    sites_path: Path
    summary_path: Path
    summary: dict
    risk_points: gpd.GeoDataFrame
    canals: gpd.GeoDataFrame
    sites: gpd.GeoDataFrame


def run_water_risk_analysis(
    *,
    mode: str,
    bbox_wgs84: tuple[float, float, float, float],
    output_dir: Path,
    water_lines: gpd.GeoDataFrame,
    water_polygons: gpd.GeoDataFrame,
    communities: gpd.GeoDataFrame,
    terrain_dem_raster: Path | None,
    demand_center_wgs84: tuple[float, float] | None,
    farm_demand_m3_day: float,
    cluster_pixel_area_m2: float,
    people_per_cluster_pixel: float,
    glofas_days_back: int,
    egms_ortho_vertical: str | Path | None,
    stability_buffer_m: float,
    differential_motion_threshold_mm_per_year: float = DEFAULT_DIFFERENTIAL_MOTION_THRESHOLD,
) -> WaterRiskResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    water_sources = _prepare_water_sources(water_lines, water_polygons)
    demand_points = _build_demand_points(
        mode=mode,
        communities=communities,
        demand_center_wgs84=demand_center_wgs84,
        farm_demand_m3_day=farm_demand_m3_day,
        cluster_pixel_area_m2=cluster_pixel_area_m2,
        people_per_cluster_pixel=people_per_cluster_pixel,
    )

    risk_points = _attach_supply_metrics(demand_points, water_sources)
    discharge_grid = None
    if _live_glofas_enabled() and not water_sources.empty:
        try:
            discharge_grid = _fetch_glofas_discharge_grid(
                output_dir / f"glofas_discharge_{_bbox_cache_key(bbox_wgs84)}.nc",
                bbox_wgs84=bbox_wgs84,
                days_back=glofas_days_back,
            )
        except Exception as exc:
            risk_points["supply_source"] = "unavailable"
            risk_points["supply_error"] = str(exc)
    if discharge_grid is not None:
        risk_points = _attach_glofas_supply(risk_points, discharge_grid)

    risk_points = _score_water_risk(risk_points)
    canals = gpd.GeoDataFrame(columns=["gravity_feasibility_pct", "risk_status", "geometry"], geometry="geometry", crs=EUHYDRO_CRS)
    sites = gpd.GeoDataFrame(
        columns=[
            "site_type",
            "risk_status",
            "gravity_feasibility_pct",
            "stability_status",
            "stability_score",
            "ksat_mm_per_hour",
            "basin_feasibility",
            "geometry",
        ],
        geometry="geometry",
        crs=EUHYDRO_CRS,
    )
    decision_rows = []
    decision_points = risk_points[risk_points["water_risk"].isin(["HIGH RISK", "MODERATE RISK", "LOW RISK"])].copy()
    if terrain_dem_raster is not None and not decision_points.empty:
        decision_outputs = evaluate_water_infrastructure(
            high_risk_points=decision_points,
            water_sources=water_sources,
            terrain_dem_raster=terrain_dem_raster,
            bbox_wgs84=bbox_wgs84,
            egms_ortho_vertical=egms_ortho_vertical,
            stability_buffer_m=stability_buffer_m,
            differential_motion_threshold_mm_per_year=differential_motion_threshold_mm_per_year,
        )
        canals = decision_outputs.canals
        sites = decision_outputs.sites
        decision_rows = decision_outputs.summary_rows
    elif not decision_points.empty:
        canals, sites, decision_rows = _build_fast_screening_options(decision_points)

    risk_points_path = output_dir / WATER_RISK_POINTS_NAME
    canals_path = output_dir / WATER_RISK_CANALS_NAME
    sites_path = output_dir / WATER_RISK_SITES_NAME
    summary_path = output_dir / WATER_RISK_SUMMARY_NAME

    risk_points.to_file(risk_points_path, driver="GeoJSON")
    canals.to_file(canals_path, driver="GeoJSON")
    sites.to_file(sites_path, driver="GeoJSON")

    summary = {
        "mode": mode,
        "cluster_source": "communities_polygons_proxy" if mode == "community" else "user_selected_point",
        "glofas_requested": discharge_grid is not None,
        "risk_counts": risk_points["water_risk"].value_counts(dropna=False).to_dict(),
        "high_risk_count": int((risk_points["water_risk"] == "HIGH RISK").sum()),
        "moderate_risk_count": int((risk_points["water_risk"] == "MODERATE RISK").sum()),
        "low_risk_count": int((risk_points["water_risk"] == "LOW RISK").sum()),
        "report_rows": json.loads(risk_points.drop(columns="geometry").to_json(orient="records")),
        "infrastructure_recommendations": decision_rows,
        "feasibility_sites": json.loads(sites.drop(columns="geometry").to_json(orient="records")) if not sites.empty else [],
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    return WaterRiskResult(
        risk_points_path=risk_points_path,
        canals_path=canals_path,
        sites_path=sites_path,
        summary_path=summary_path,
        summary=summary,
        risk_points=risk_points,
        canals=canals,
        sites=sites,
    )


def _prepare_water_sources(water_lines: gpd.GeoDataFrame, water_polygons: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    frames: list[gpd.GeoDataFrame] = []
    if not water_lines.empty:
        lines = water_lines.to_crs(EUHYDRO_CRS).copy()
        lines["source_kind"] = "Blue Line"
        keep = [
            column
            for column in (
                "source_kind",
                "discharge_m3s",
                "daily_flow_volume_m3",
                "observed_width_m",
                "river_length_m",
                "geometry",
            )
            if column in lines.columns
        ]
        frames.append(lines[keep])
    if not water_polygons.empty:
        polygons = water_polygons.to_crs(EUHYDRO_CRS).copy()
        polygons["source_kind"] = "Blue Polygon"
        if "surface_area_m2" not in polygons.columns:
            polygons["surface_area_m2"] = polygons.geometry.area
        keep = [column for column in ("source_kind", "surface_area_m2", "geometry") if column in polygons.columns]
        frames.append(polygons[keep])
    if not frames:
        return gpd.GeoDataFrame(columns=["source_kind", "geometry"], geometry="geometry", crs=EUHYDRO_CRS)
    merged = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), geometry="geometry", crs=EUHYDRO_CRS)
    merged["source_id"] = [f"water_{index}" for index in range(len(merged))]
    return merged


def _build_demand_points(
    *,
    mode: str,
    communities: gpd.GeoDataFrame,
    demand_center_wgs84: tuple[float, float] | None,
    farm_demand_m3_day: float,
    cluster_pixel_area_m2: float,
    people_per_cluster_pixel: float,
) -> gpd.GeoDataFrame:
    if mode == "farm":
        if demand_center_wgs84 is None:
            raise RuntimeError("Farm siting mode requires a demand center coordinate.")
        lat, lon = demand_center_wgs84
        transformer = Transformer.from_crs(WGS84_CRS, EUHYDRO_CRS, always_xy=True)
        x, y = transformer.transform(lon, lat)
        return gpd.GeoDataFrame(
            [
                {
                    "demand_id": "farm_1",
                    "mode": "farm",
                    "demand_population_proxy": float(farm_demand_m3_day / 50.0 * 1000.0),
                    "demand_m3_day": float(farm_demand_m3_day),
                    "cluster_pixels": None,
                    "geometry": Point(x, y),
                }
            ],
            geometry="geometry",
            crs=EUHYDRO_CRS,
        )

    community_columns = [
        "demand_id",
        "mode",
        "demand_population_proxy",
        "demand_m3_day",
        "cluster_pixels",
        "area_m2",
        "block_area_m2",
        "member_count",
        "geometry",
    ]
    if communities.empty:
        return gpd.GeoDataFrame(columns=community_columns, geometry="geometry", crs=EUHYDRO_CRS)

    clusters = communities.to_crs(EUHYDRO_CRS).copy()
    clusters["cluster_pixels"] = np.maximum(np.round(clusters["area_m2"].astype(float) / cluster_pixel_area_m2), 1.0)
    clusters["demand_population_proxy"] = clusters["cluster_pixels"] * people_per_cluster_pixel
    clusters["demand_m3_day"] = (clusters["demand_population_proxy"] / 1000.0) * 50.0
    clusters["demand_id"] = [f"community_{index}" for index in range(len(clusters))]
    keep_columns = [column for column in community_columns if column in clusters.columns and column != "geometry"]
    result = clusters[keep_columns].copy()
    result["mode"] = "community"
    return gpd.GeoDataFrame(result, geometry=clusters.geometry, crs=EUHYDRO_CRS)


def _attach_supply_metrics(demand_points: gpd.GeoDataFrame, water_sources: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    result = demand_points.copy()
    for column in (
        "nearest_source_id",
        "nearest_source_type",
        "distance_to_source_m",
        "supply_discharge_m3s",
        "supply_m3_day",
        "supply_source",
        "water_risk",
        "risk_reason",
    ):
        result[column] = np.nan if column in {"distance_to_source_m", "supply_discharge_m3s", "supply_m3_day"} else None

    if water_sources.empty or result.empty:
        return result

    for idx, row in result.iterrows():
        distances = water_sources.geometry.distance(row.geometry)
        nearest_idx = int(distances.idxmin())
        nearest = water_sources.loc[nearest_idx]
        result.at[idx, "nearest_source_id"] = nearest["source_id"]
        result.at[idx, "nearest_source_type"] = nearest["source_kind"]
        result.at[idx, "distance_to_source_m"] = float(distances.loc[nearest_idx])
        _, source_point = nearest_points(row.geometry, nearest.geometry)
        result.at[idx, "supply_sample_x"] = source_point.x
        result.at[idx, "supply_sample_y"] = source_point.y
        discharge = _source_discharge_m3s(nearest)
        result.at[idx, "supply_discharge_m3s"] = discharge
        result.at[idx, "supply_m3_day"] = discharge * 86400.0
        result.at[idx, "supply_source"] = _source_flow_label(nearest)
    return result


def _build_fast_screening_options(risk_points: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, list[dict]]:
    canal_features: list[dict] = []
    site_features: list[dict] = []
    summary_rows: list[dict] = []
    transformer = Transformer.from_crs(EUHYDRO_CRS, WGS84_CRS, always_xy=True)

    for _, demand in risk_points.iterrows():
        if pd.isna(demand.get("supply_sample_x")) or pd.isna(demand.get("supply_sample_y")):
            continue
        demand_point = demand.geometry if demand.geometry.geom_type == "Point" else demand.geometry.representative_point()
        source_point = Point(float(demand["supply_sample_x"]), float(demand["supply_sample_y"]))
        canal_line = LineString([source_point, demand_point])
        midpoint = canal_line.interpolate(0.58, normalized=True)
        lake_polygon = midpoint.buffer(260.0).intersection(midpoint.buffer(260.0))
        distance = float(demand.get("distance_to_source_m") or canal_line.length)
        supply = _finite_float(demand.get("supply_m3_day"), 0.0) or 0.0
        demand_m3_day = _finite_float(demand.get("demand_m3_day"), 0.0) or 0.0
        gravity_pct = float(np.clip(78.0 - distance / 180.0, 35.0, 82.0))
        canal_score = round(float(np.clip(88.0 - distance / 95.0 + min(supply / max(demand_m3_day, 1.0), 2.0) * 8.0, 45.0, 86.0)), 1)
        lake_score = round(float(np.clip(58.0 + distance / 220.0 - min(supply / max(demand_m3_day, 1.0), 2.0) * 4.0, 46.0, 84.0)), 1)
        lon, lat = transformer.transform(demand_point.x, demand_point.y)
        soil = _estimated_soil(lat, lon)
        if canal_score >= lake_score + 6.0:
            decision = "BUILD CANAL"
            reason = "Fast screening favors direct conveyance because the source is close enough and storage is less valuable."
        elif lake_score >= canal_score + 6.0:
            decision = "BUILD LAKE / RESERVOIR"
            reason = "Fast screening favors local storage because distance and demand pressure make direct conveyance less robust."
        else:
            decision = "BUILD RESERVOIR + FEED CANAL"
            reason = "Fast screening keeps both storage and conveyance because their scores are close."

        common = {
            "demand_id": demand.get("demand_id"),
            "decision": decision,
            "decision_reason": reason,
            "risk_status": demand.get("water_risk"),
            "nearest_source_type": demand.get("nearest_source_type"),
            "distance_to_source_m": distance,
            "demand_m3_day": demand_m3_day,
            "supply_discharge_m3s": demand.get("supply_discharge_m3s"),
            "supply_m3_day": supply,
            "supply_source": demand.get("supply_source"),
        }
        canal_features.append(
            {
                **common,
                "option_score": canal_score,
                "canal_length_m": float(canal_line.length),
                "gravity_feasibility_pct": gravity_pct,
                "elevation_drop_m": None,
                "mean_route_slope_deg": None,
                "max_route_slope_deg": None,
                "terrain_behavior": "Fast screening route. Enable Terrain overlay for DEM-based slope and least-cost routing.",
                "route_ksat_mm_per_hour": soil["ksat_mm_per_hour"],
                "route_clay_pct": soil["clay_pct"],
                "route_sand_pct": soil["sand_pct"],
                "route_silt_pct": soil["silt_pct"],
                "route_seepage_class": soil["seepage_class"],
                "route_soil_behavior": soil["engineering_note"],
                "canal_stability_status": "SCREENING ONLY",
                "canal_v_mean_mm_per_year": None,
                "geometry": canal_line,
            }
        )
        site_features.append(
            {
                **common,
                "site_type": "screening_lake_site",
                "option_score": lake_score,
                "distance_to_demand_m": float(midpoint.distance(demand_point)),
                "gravity_feasibility_pct": max(gravity_pct - 8.0, 30.0),
                "feed_canal_length_m": float(source_point.distance(midpoint)),
                "stability_status": "SCREENING ONLY",
                "stability_score": None,
                "stability_velocity_mm_per_year": None,
                "ksat_mm_per_hour": soil["ksat_mm_per_hour"],
                "clay_pct": soil["clay_pct"],
                "sand_pct": soil["sand_pct"],
                "silt_pct": soil["silt_pct"],
                "seepage_class": soil["seepage_class"],
                "engineering_note": soil["engineering_note"],
                "basin_depth_m": None,
                "local_slope_deg": None,
                "geometry": lake_polygon,
            }
        )
        summary_rows.append(
            {
                "demand_id": demand.get("demand_id"),
                "decision": decision,
                "decision_reason": reason,
                "canal_score": canal_score,
                "reservoir_score": lake_score,
                "canal_length_m": float(canal_line.length),
                "canal_gravity_feasibility_pct": gravity_pct,
                "canal_route_ksat_mm_per_hour": soil["ksat_mm_per_hour"],
                "canal_route_clay_pct": soil["clay_pct"],
                "canal_route_sand_pct": soil["sand_pct"],
                "canal_route_silt_pct": soil["silt_pct"],
                "canal_route_seepage_class": soil["seepage_class"],
                "reservoir_distance_to_demand_m": float(midpoint.distance(demand_point)),
                "reservoir_distance_to_source_m": float(source_point.distance(midpoint)),
                "reservoir_ksat_mm_per_hour": soil["ksat_mm_per_hour"],
                "reservoir_stability_status": "SCREENING ONLY",
            }
        )

    canals = gpd.GeoDataFrame(canal_features, geometry="geometry", crs=EUHYDRO_CRS) if canal_features else _empty_canals()
    sites = gpd.GeoDataFrame(site_features, geometry="geometry", crs=EUHYDRO_CRS) if site_features else _empty_sites()
    return canals, sites, summary_rows


def _estimated_soil(lat: float, lon: float) -> dict:
    seed = abs(np.sin(np.radians(lat * 11.0 + lon * 7.0)))
    clay_pct = 24.0 + seed * 22.0
    sand_pct = 18.0 + (1.0 - seed) * 34.0
    silt_pct = max(5.0, 100.0 - clay_pct - sand_pct)
    total = clay_pct + sand_pct + silt_pct
    clay_pct = clay_pct / total * 100.0
    sand_pct = sand_pct / total * 100.0
    silt_pct = silt_pct / total * 100.0
    ksat = estimate_ksat_mm_per_hour(sand_pct=sand_pct, clay_pct=clay_pct, organic_matter_pct=1.5)
    seepage_class, note = classify_seepage_risk(ksat)
    return {
        "clay_pct": round(float(clay_pct), 1),
        "sand_pct": round(float(sand_pct), 1),
        "silt_pct": round(float(silt_pct), 1),
        "ksat_mm_per_hour": ksat,
        "seepage_class": seepage_class,
        "engineering_note": f"{note} Fast screening estimate; enable Terrain/SoilGrids for stronger design evidence.",
    }


def _empty_canals() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs=EUHYDRO_CRS)


def _empty_sites() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs=EUHYDRO_CRS)


def _source_discharge_m3s(source) -> float:
    for column in ("discharge_m3s",):
        value = source.get(column)
        if pd.notna(value):
            try:
                numeric = float(value)
                if np.isfinite(numeric) and numeric >= 0.0:
                    return numeric
            except (TypeError, ValueError):
                pass
    daily_volume = source.get("daily_flow_volume_m3")
    if pd.notna(daily_volume):
        try:
            numeric = float(daily_volume)
            if np.isfinite(numeric) and numeric >= 0.0:
                return numeric / 86400.0
        except (TypeError, ValueError):
            pass

    kind = str(source.get("source_kind") or "")
    if "Polygon" in kind:
        area_m2 = _finite_float(source.get("surface_area_m2"), default=120_000.0)
        return float(np.clip(area_m2 / 1_200_000.0, 0.02, 2.5))
    width_m = _finite_float(source.get("observed_width_m"), default=None)
    if width_m is not None:
        return float(np.clip(width_m * 0.12, 0.02, 4.0))
    length_m = _finite_float(source.geometry.length, default=1200.0)
    return float(np.clip(length_m / 3500.0, 0.03, 3.0))


def _source_flow_label(source) -> str:
    if pd.notna(source.get("discharge_m3s")) or pd.notna(source.get("daily_flow_volume_m3")):
        return "river-metric-derived"
    return "estimated-screening-flow"


def _finite_float(value, default: float | None) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return numeric if np.isfinite(numeric) else default


def _live_glofas_enabled() -> bool:
    return os.getenv("HEAVYWATER_ENABLE_LIVE_GLOFAS", "").strip().lower() in {"1", "true", "yes"}


def _fetch_glofas_discharge_grid(cache_path: Path, bbox_wgs84: tuple[float, float, float, float], days_back: int):
    import cdsapi

    target_date = date.today() - timedelta(days=max(days_back, 2))
    if cache_path.exists():
        return _read_glofas_discharge_grid(cache_path, date_label="cached")

    north = min(90.0, bbox_wgs84[3] + 0.25)
    west = max(-180.0, bbox_wgs84[0] - 0.25)
    south = max(-90.0, bbox_wgs84[1] - 0.25)
    east = min(180.0, bbox_wgs84[2] + 0.25)
    request = {
        "system_version": "operational",
        "product_type": "control_forecast",
        "hydrological_model": "lisflood",
        "variable": "river_discharge_in_the_last_24_hours",
        "year": target_date.strftime("%Y"),
        "month": target_date.strftime("%m"),
        "day": target_date.strftime("%d"),
        "leadtime_hour": ["24"],
        "area": [north, west, south, east],
        "data_format": "netcdf",
        "download_format": "unarchived",
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    client = _build_ewds_client(cdsapi)
    client.retrieve("cems-glofas-forecast", request).download(str(cache_path))
    return _read_glofas_discharge_grid(cache_path, date_label=target_date.isoformat())


def _bbox_cache_key(bbox_wgs84: tuple[float, float, float, float]) -> str:
    rounded = ",".join(f"{value:.3f}" for value in bbox_wgs84)
    return hashlib.sha1(rounded.encode("ascii")).hexdigest()[:10]


def _read_glofas_discharge_grid(cache_path: Path, date_label: str):
    with Dataset(cache_path) as ds:
        lat_name = _first_present(ds.variables, ("latitude", "lat"))
        lon_name = _first_present(ds.variables, ("longitude", "lon"))
        var_name = _infer_discharge_var_name(ds.variables.keys())
        lats = np.asarray(ds.variables[lat_name][:], dtype="float64")
        lons = np.asarray(ds.variables[lon_name][:], dtype="float64")
        discharge = np.asarray(ds.variables[var_name][:], dtype="float64").squeeze()
        fill_value = getattr(ds.variables[var_name], "_FillValue", None)
        if fill_value is not None:
            discharge = np.where(discharge == fill_value, np.nan, discharge)
    return {"lats": lats, "lons": lons, "discharge": discharge, "date": date_label}


def _attach_glofas_supply(risk_points: gpd.GeoDataFrame, discharge_grid: dict) -> gpd.GeoDataFrame:
    result = risk_points.copy()
    lats = discharge_grid["lats"]
    lons = discharge_grid["lons"]
    discharge = discharge_grid["discharge"]
    transformer = Transformer.from_crs(EUHYDRO_CRS, WGS84_CRS, always_xy=True)

    for idx, row in result.iterrows():
        if pd.isna(row.get("supply_sample_x")) or pd.isna(row.get("supply_sample_y")):
            continue
        lon, lat = transformer.transform(float(row["supply_sample_x"]), float(row["supply_sample_y"]))
        row_idx = int(np.argmin(np.abs(lats - lat)))
        col_idx = int(np.argmin(np.abs(lons - lon)))
        value = float(discharge[row_idx, col_idx])
        if not np.isfinite(value):
            result.at[idx, "supply_source"] = "unavailable"
            continue
        result.at[idx, "supply_discharge_m3s"] = value
        result.at[idx, "supply_m3_day"] = value * 86400.0
        result.at[idx, "supply_source"] = f"glofas-forecast:{discharge_grid['date']}"
    return result


def _score_water_risk(risk_points: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    result = risk_points.copy()
    for idx, row in result.iterrows():
        distance = float(row["distance_to_source_m"]) if pd.notna(row["distance_to_source_m"]) else np.nan
        supply = float(row["supply_m3_day"]) if pd.notna(row["supply_m3_day"]) else np.nan
        demand = float(row["demand_m3_day"]) if pd.notna(row["demand_m3_day"]) else np.nan
        if not np.isfinite(distance) or not np.isfinite(demand):
            result.at[idx, "water_risk"] = "UNKNOWN"
            result.at[idx, "risk_reason"] = "Missing distance or demand."
            continue
        if not np.isfinite(supply):
            if distance > 3000.0:
                result.at[idx, "water_risk"] = "HIGH RISK"
                result.at[idx, "risk_reason"] = "Flow rate unavailable; distance-only screening flags the water source as more than 3 km away."
            elif distance > 1500.0:
                result.at[idx, "water_risk"] = "MODERATE RISK"
                result.at[idx, "risk_reason"] = "Flow rate unavailable; distance-only screening flags the water source as 1.5-3 km away."
            else:
                result.at[idx, "water_risk"] = "LOW RISK"
                result.at[idx, "risk_reason"] = "Flow rate unavailable; distance-only screening finds a nearby water source."
            continue
        if distance > 2000.0 and supply < demand:
            result.at[idx, "water_risk"] = "HIGH RISK"
            result.at[idx, "risk_reason"] = "Distance exceeds 2 km and daily supply is below estimated demand."
        elif distance <= 2000.0 and supply < demand:
            result.at[idx, "water_risk"] = "MODERATE RISK"
            result.at[idx, "risk_reason"] = "Source is close, but daily supply is below estimated demand."
        elif distance <= 2000.0 and supply >= demand:
            result.at[idx, "water_risk"] = "LOW RISK"
            result.at[idx, "risk_reason"] = "Source is close and daily supply exceeds estimated demand."
        else:
            result.at[idx, "water_risk"] = "MODERATE RISK"
            result.at[idx, "risk_reason"] = "Source is farther than 2 km but modeled supply exceeds estimated demand."
    return result
