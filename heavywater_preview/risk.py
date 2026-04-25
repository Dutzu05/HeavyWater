from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from netCDF4 import Dataset
from pyproj import Transformer
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
    if not water_sources.empty:
        try:
            discharge_grid = _fetch_glofas_discharge_grid(
                output_dir / "glofas_discharge_latest.nc",
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
    if terrain_dem_raster is not None and (risk_points["water_risk"] == "HIGH RISK").any():
        decision_outputs = evaluate_water_infrastructure(
            high_risk_points=risk_points[risk_points["water_risk"] == "HIGH RISK"].copy(),
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
        frames.append(lines[["source_kind", "geometry"]])
    if not water_polygons.empty:
        polygons = water_polygons.to_crs(EUHYDRO_CRS).copy()
        polygons["source_kind"] = "Blue Polygon"
        frames.append(polygons[["source_kind", "geometry"]])
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
    return result


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
            result.at[idx, "water_risk"] = "UNKNOWN"
            result.at[idx, "risk_reason"] = "Supply unavailable from GloFAS."
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
