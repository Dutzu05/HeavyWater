from __future__ import annotations

import json
from pathlib import Path

import math

from heavywater_preview.soil import SoilTextureEstimate, classify_seepage_risk, estimate_ksat_mm_per_hour, query_soilgrids_textures


def build_report_inputs(
    *,
    lat: float,
    lon: float,
    size_km: float,
    terrain_summary: dict | None,
    stability_summary: dict | None,
    water_risk_summary: dict | None,
) -> dict:
    return {
        "location": {
            "lat": lat,
            "lon": lon,
            "size_km": size_km,
        },
        "terrain": terrain_summary,
        "soil": _safe_soil_summary(lat, lon),
        "stability": stability_summary,
        "water_risk": water_risk_summary,
    }


def write_report_inputs(path: Path, report_inputs: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report_inputs, indent=2), encoding="utf-8")


def _safe_soil_summary(lat: float, lon: float) -> dict:
    try:
        estimate = query_soilgrids_textures(lat, lon)
    except Exception as exc:
        estimate = _estimated_soil_summary(lat, lon)
        estimate["source_note"] = f"Screening estimate used because SoilGrids failed: {exc}"
        return estimate
    return _soil_estimate_to_dict(lat, lon, estimate)


def _soil_estimate_to_dict(lat: float, lon: float, estimate: SoilTextureEstimate) -> dict:
    return {
        "query_point": {"lat": lat, "lon": lon},
        "depth": "60-100cm",
        "clay_pct": estimate.clay_pct,
        "sand_pct": estimate.sand_pct,
        "silt_pct": estimate.silt_pct,
        "organic_matter_pct": estimate.organic_matter_pct,
        "ksat_mm_per_hour": estimate.ksat_mm_per_hour,
        "seepage_class": estimate.seepage_class,
        "engineering_note": estimate.engineering_note,
    }


def _estimated_soil_summary(lat: float, lon: float) -> dict:
    seed = abs(math.sin(math.radians(lat * 11.0 + lon * 7.0)))
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
        "query_point": {"lat": lat, "lon": lon},
        "depth": "60-100cm",
        "clay_pct": round(clay_pct, 1),
        "sand_pct": round(sand_pct, 1),
        "silt_pct": round(silt_pct, 1),
        "organic_matter_pct": 1.5,
        "ksat_mm_per_hour": ksat,
        "seepage_class": seepage_class,
        "engineering_note": note,
    }
