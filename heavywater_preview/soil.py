from __future__ import annotations

import json
from dataclasses import dataclass
from math import isfinite, log
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SOILGRIDS_PROPERTIES_URL = "https://rest.isric.org/soilgrids/v2.0/properties/query"


@dataclass
class SoilTextureEstimate:
    clay_pct: float
    sand_pct: float
    silt_pct: float
    organic_matter_pct: float
    ksat_mm_per_hour: float | None
    seepage_class: str
    engineering_note: str


def query_soilgrids_textures(lat: float, lon: float) -> SoilTextureEstimate:
    payload = _fetch_soilgrids_payload(lat=lat, lon=lon)
    clay_pct = _to_percent(_find_layer_value(payload, "clay"))
    sand_pct = _to_percent(_find_layer_value(payload, "sand"))
    silt_pct = _to_percent(_find_layer_value(payload, "silt"))
    soc_value = _find_layer_value(payload, "soc")

    if clay_pct is None or sand_pct is None or silt_pct is None:
        raise RuntimeError("SoilGrids returned incomplete 60-100 cm texture values.")

    organic_matter_pct = _soc_to_organic_matter_pct(soc_value)
    ksat = estimate_ksat_mm_per_hour(
        sand_pct=sand_pct,
        clay_pct=clay_pct,
        organic_matter_pct=organic_matter_pct,
    )
    seepage_class, engineering_note = classify_seepage_risk(ksat)
    return SoilTextureEstimate(
        clay_pct=clay_pct,
        sand_pct=sand_pct,
        silt_pct=silt_pct,
        organic_matter_pct=organic_matter_pct,
        ksat_mm_per_hour=ksat,
        seepage_class=seepage_class,
        engineering_note=engineering_note,
    )


def estimate_ksat_mm_per_hour(sand_pct: float, clay_pct: float, organic_matter_pct: float = 0.0) -> float | None:
    sand = sand_pct / 100.0
    clay = clay_pct / 100.0
    organic_matter = organic_matter_pct / 100.0

    theta_1500_t = (
        -0.024 * sand
        + 0.487 * clay
        + 0.006 * organic_matter
        + 0.005 * sand * clay
        + 0.013 * clay * organic_matter
        + 0.068
    )
    theta_33_t = (
        -0.251 * sand
        + 0.195 * clay
        + 0.011 * organic_matter
        + 0.006 * sand * clay
        + 0.027 * clay * organic_matter
        + 0.452
    )
    theta_s33_t = (
        0.278 * sand
        + 0.034 * clay
        + 0.022 * organic_matter
        - 0.018 * sand * clay
        - 0.027 * clay * organic_matter
        - 0.584 * sand * organic_matter
        + 0.078
    )

    theta_1500 = theta_1500_t + (0.14 * theta_1500_t - 0.02)
    theta_33 = theta_33_t + (1.283 * theta_33_t * theta_33_t - 0.374 * theta_33_t - 0.015)
    theta_s33 = theta_s33_t + (0.636 * theta_s33_t - 0.107)
    theta_s = theta_33 + theta_s33 - 0.097 * sand + 0.043

    safe_theta_1500 = max(theta_1500, 0.001)
    safe_theta_33 = max(theta_33, safe_theta_1500 + 0.001)
    safe_theta_s = max(theta_s, safe_theta_33 + 0.001)
    lam = (log(safe_theta_33) - log(safe_theta_1500)) / (log(1500.0) - log(33.0))
    ksat = 1930.0 * pow(max(safe_theta_s - safe_theta_33, 0.0001), max(3.0 - lam, 0.1))
    return float(ksat) if isfinite(ksat) else None


def classify_seepage_risk(ksat_mm_per_hour: float | None) -> tuple[str, str]:
    if ksat_mm_per_hour is None:
        return "Unavailable", "Soil permeability estimate unavailable for this point."
    if ksat_mm_per_hour < 5.0:
        return "Low Seepage", "Natural Clay Basin - High Feasibility. No liner required."
    if ksat_mm_per_hour <= 20.0:
        return "Medium Seepage", "Moderate Permeability - Soil compaction recommended."
    return "High Seepage", "High Risk - Sandy soil detected. HDPE geomembrane liner mandatory."


def _fetch_soilgrids_payload(lat: float, lon: float) -> dict:
    query = urlencode(
        [
            ("lon", f"{lon:.6f}"),
            ("lat", f"{lat:.6f}"),
            ("property", "clay"),
            ("property", "sand"),
            ("property", "silt"),
            ("property", "soc"),
            ("depth", "60-100cm"),
            ("value", "mean"),
        ]
    )
    request = Request(f"{SOILGRIDS_PROPERTIES_URL}?{query}", headers={"Accept": "application/json"})
    with urlopen(request, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def _find_layer_value(payload: dict, property_name: str) -> float | None:
    layers = payload.get("properties", {}).get("layers", [])
    for layer in layers:
        if layer.get("name") != property_name:
            continue
        for depth in layer.get("depths", []):
            label = str(depth.get("label") or depth.get("range") or "")
            if label != "60-100cm":
                continue
            value = depth.get("values", {}).get("mean")
            if isinstance(value, (int, float)):
                return float(value)
    return None


def _to_percent(value: float | None) -> float | None:
    if value is None:
        return None
    return value / 10.0


def _soc_to_organic_matter_pct(value: float | None) -> float:
    if value is None:
        return 0.0
    soc_g_per_kg = value / 10.0
    return soc_g_per_kg * 0.1724
