from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
from pyproj import Transformer
from shapely.geometry import LineString, Point

from heavywater_preview.config import (
    INDEX_HTML_NAME,
    MAP_HTML_NAME,
    REPORT_INPUTS_NAME,
    STABILITY_SUMMARY_NAME,
    TERRAIN_SUMMARY_NAME,
    WATER_RISK_CANALS_NAME,
    WATER_RISK_POINTS_NAME,
    WATER_RISK_SITES_NAME,
)
from heavywater_preview.leaflet import write_preview_map


MOCK_EXAMPLES_PATH = Path(__file__).resolve().parents[1] / "data" / "mock_examples.json"
MATCH_TOLERANCE_DEG = 0.01


def find_mock_example(lat: float, lon: float) -> dict | None:
    if not MOCK_EXAMPLES_PATH.exists():
        return None
    examples = json.loads(MOCK_EXAMPLES_PATH.read_text(encoding="utf-8"))
    for example in examples:
        if abs(float(example["lat"]) - float(lat)) <= MATCH_TOLERANCE_DEG and abs(float(example["lon"]) - float(lon)) <= MATCH_TOLERANCE_DEG:
            return example
    return None


def write_mock_outputs(*, example: dict, output_dir: Path, size_km: float) -> SimpleNamespace:
    output_dir.mkdir(parents=True, exist_ok=True)
    bbox_wgs84 = _bbox(example["lat"], example["lon"], size_km)
    risk_points, canals, sites, water_lines, communities = _build_layers(example)

    risk_points_path = output_dir / WATER_RISK_POINTS_NAME
    canals_path = output_dir / WATER_RISK_CANALS_NAME
    sites_path = output_dir / WATER_RISK_SITES_NAME
    risk_points.to_file(risk_points_path, driver="GeoJSON")
    canals.to_file(canals_path, driver="GeoJSON")
    sites.to_file(sites_path, driver="GeoJSON")

    terrain_summary = {
        "elevation_min_m": example["elevation_min_m"],
        "elevation_max_m": example["elevation_max_m"],
        "elevation_mean_m": example["elevation_mean_m"],
        "slope_mean_deg": example["slope_deg"],
    }
    stability_summary = {
        "stability_status": "SCREENING MOCK - STABLE",
        "v_mean_mm_per_year": 0.4,
        "differential_motion_mm_per_year": 0.7,
        "maintenance_note": "Mock stability screening: use EGMS and field survey before design.",
    }
    water_risk_summary = _water_risk_summary(example, risk_points, canals, sites)
    report_inputs = {
        "location": {"lat": example["lat"], "lon": example["lon"], "size_km": size_km},
        "terrain": terrain_summary,
        "soil": {
            "query_point": {"lat": example["lat"], "lon": example["lon"]},
            "depth": "60-100cm",
            "clay_pct": example["clay_pct"],
            "sand_pct": example["sand_pct"],
            "silt_pct": example["silt_pct"],
            "organic_matter_pct": 1.7,
            "ksat_mm_per_hour": example["ksat_mm_per_hour"],
            "seepage_class": example["seepage_class"],
            "engineering_note": example["engineering_note"],
            "source_note": "Local mock example for instant demonstration.",
        },
        "stability": stability_summary,
        "water_risk": water_risk_summary,
        "mock_example": {"id": example["id"], "title": example["title"]},
    }
    (output_dir / TERRAIN_SUMMARY_NAME).write_text(json.dumps(terrain_summary, indent=2), encoding="utf-8")
    (output_dir / STABILITY_SUMMARY_NAME).write_text(json.dumps(stability_summary, indent=2), encoding="utf-8")
    (output_dir / "water_risk_summary.json").write_text(json.dumps(water_risk_summary, indent=2), encoding="utf-8")
    (output_dir / REPORT_INPUTS_NAME).write_text(json.dumps(report_inputs, indent=2), encoding="utf-8")

    map_html_path = output_dir / MAP_HTML_NAME
    index_html_path = output_dir / INDEX_HTML_NAME
    map_communities = communities if example["mode"] == "community" else gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    map_risk_points = risk_points if example["mode"] == "farm" else None
    write_preview_map(
        html_path=map_html_path,
        index_path=index_html_path,
        lat=float(example["lat"]),
        lon=float(example["lon"]),
        bbox_wgs84=bbox_wgs84,
        water_lines=water_lines,
        communities=map_communities,
        water_risk_points=map_risk_points,
        canal_paths=canals,
        feasibility_sites=sites,
    )

    return SimpleNamespace(
        output_dir=output_dir,
        map_html_path=map_html_path,
        index_html_path=index_html_path,
    )


def _build_layers(example: dict):
    to_eu = Transformer.from_crs("EPSG:4326", "EPSG:3035", always_xy=True)
    source_x, source_y = to_eu.transform(example["source_lon"], example["source_lat"])
    demand_x, demand_y = to_eu.transform(example["lon"], example["lat"])
    source = Point(source_x, source_y)
    demand = Point(demand_x, demand_y)
    canal = LineString([source, demand])
    lake_center = canal.interpolate(0.58, normalized=True)
    lake = lake_center.buffer(260.0)
    supply_m3_day = float(example["flow_m3s"]) * 86400.0
    common = {
        "demand_id": f"{example['id']}_demand",
        "decision": example["decision"],
        "decision_reason": example["reason"],
        "risk_status": example["risk_status"],
        "nearest_source_type": "Mock watercourse",
        "distance_to_source_m": example["distance_to_source_m"],
        "demand_m3_day": example["demand_m3_day"],
        "supply_discharge_m3s": example["flow_m3s"],
        "supply_m3_day": supply_m3_day,
        "supply_source": "local-mock-example",
    }
    risk = gpd.GeoDataFrame(
        [
            {
                "demand_id": common["demand_id"],
                "mode": example["mode"],
                "demand_population_proxy": example["population_proxy"],
                "demand_m3_day": example["demand_m3_day"],
                "nearest_source_type": common["nearest_source_type"],
                "distance_to_source_m": example["distance_to_source_m"],
                "supply_discharge_m3s": example["flow_m3s"],
                "supply_m3_day": supply_m3_day,
                "supply_source": "local-mock-example",
                "water_risk": example["risk_status"],
                "risk_reason": example["reason"],
                "supply_sample_x": source_x,
                "supply_sample_y": source_y,
                "area_m2": 92000.0 if example["mode"] == "community" else None,
                "block_area_m2": 128000.0 if example["mode"] == "community" else None,
                "member_count": 4 if example["mode"] == "community" else None,
                "geometry": demand if example["mode"] == "farm" else demand.buffer(430.0),
            }
        ],
        geometry="geometry",
        crs="EPSG:3035",
    )
    canals = gpd.GeoDataFrame(
        [
            {
                **common,
                "option_score": example["canal_score"],
                "canal_length_m": example["canal_length_m"],
                "gravity_feasibility_pct": example["gravity_pct"],
                "elevation_drop_m": 18.0 if example["decision"] == "BUILD CANAL" else 7.5,
                "mean_route_slope_deg": example["slope_deg"],
                "max_route_slope_deg": example["slope_deg"] + 4.0,
                "terrain_behavior": "Mock DEM screening: route grade is suitable for concept comparison.",
                "route_ksat_mm_per_hour": example["ksat_mm_per_hour"],
                "route_clay_pct": example["clay_pct"],
                "route_sand_pct": example["sand_pct"],
                "route_silt_pct": example["silt_pct"],
                "route_seepage_class": example["seepage_class"],
                "route_soil_behavior": example["engineering_note"],
                "canal_stability_status": "SCREENING MOCK - STABLE",
                "canal_v_mean_mm_per_year": 0.4,
                "geometry": canal,
            }
        ],
        geometry="geometry",
        crs="EPSG:3035",
    )
    sites = gpd.GeoDataFrame(
        [
            {
                **common,
                "site_type": "mock_lake_reservoir_site",
                "option_score": example["lake_score"],
                "distance_to_demand_m": example["distance_to_demand_m"],
                "gravity_feasibility_pct": max(30.0, example["gravity_pct"] - 8.0),
                "feed_canal_length_m": example["feed_canal_length_m"],
                "stability_status": "SCREENING MOCK - STABLE",
                "stability_score": 85,
                "stability_velocity_mm_per_year": 0.4,
                "ksat_mm_per_hour": example["ksat_mm_per_hour"],
                "clay_pct": example["clay_pct"],
                "sand_pct": example["sand_pct"],
                "silt_pct": example["silt_pct"],
                "seepage_class": example["seepage_class"],
                "engineering_note": example["engineering_note"],
                "basin_depth_m": example["basin_depth_m"],
                "local_slope_deg": example["slope_deg"],
                "geometry": lake,
            }
        ],
        geometry="geometry",
        crs="EPSG:3035",
    )
    water_lines = gpd.GeoDataFrame(
        [
            {
                "discharge_m3s": example["flow_m3s"],
                "daily_flow_volume_m3": supply_m3_day,
                "observed_width_m": max(3.0, example["flow_m3s"] * 18.0),
                "quantity_score": 0.8,
                "score_label": "Mock source score",
                "geometry": LineString([(example["source_lon"] - 0.012, example["source_lat"] - 0.008), (example["source_lon"] + 0.012, example["source_lat"] + 0.008)]),
            }
        ],
        geometry="geometry",
        crs="EPSG:4326",
    )
    communities = risk if example["mode"] == "community" else gpd.GeoDataFrame(geometry=[], crs="EPSG:3035")
    return risk, canals, sites, water_lines, communities


def _water_risk_summary(example: dict, risk_points, canals, sites) -> dict:
    return {
        "mode": example["mode"],
        "cluster_source": "local_mock_example",
        "glofas_requested": False,
        "risk_counts": {example["risk_status"]: 1},
        "high_risk_count": 1 if example["risk_status"] == "HIGH RISK" else 0,
        "moderate_risk_count": 1 if example["risk_status"] == "MODERATE RISK" else 0,
        "low_risk_count": 1 if example["risk_status"] == "LOW RISK" else 0,
        "report_rows": json.loads(risk_points.drop(columns="geometry").to_json(orient="records")),
        "infrastructure_recommendations": [
            {
                "demand_id": f"{example['id']}_demand",
                "decision": example["decision"],
                "decision_reason": example["reason"],
                "canal_score": example["canal_score"],
                "reservoir_score": example["lake_score"],
                "canal_length_m": example["canal_length_m"],
                "canal_gravity_feasibility_pct": example["gravity_pct"],
                "canal_route_ksat_mm_per_hour": example["ksat_mm_per_hour"],
                "canal_route_clay_pct": example["clay_pct"],
                "canal_route_sand_pct": example["sand_pct"],
                "canal_route_silt_pct": example["silt_pct"],
                "canal_route_seepage_class": example["seepage_class"],
                "reservoir_basin_depth_m": example["basin_depth_m"],
                "reservoir_distance_to_demand_m": example["distance_to_demand_m"],
                "reservoir_distance_to_source_m": example["distance_to_source_m"],
                "reservoir_ksat_mm_per_hour": example["ksat_mm_per_hour"],
                "reservoir_stability_status": "SCREENING MOCK - STABLE",
            }
        ],
        "feasibility_sites": json.loads(sites.drop(columns="geometry").to_json(orient="records")),
    }


def _bbox(lat: float, lon: float, size_km: float) -> tuple[float, float, float, float]:
    half = max(float(size_km), 2.0) / 2.0
    dlat = half / 111.32
    dlon = half / 80.0
    return (float(lon) - dlon, float(lat) - dlat, float(lon) + dlon, float(lat) + dlat)
