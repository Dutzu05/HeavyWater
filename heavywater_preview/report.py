from __future__ import annotations

import json
from pathlib import Path

from heavywater_preview.soil import SoilTextureEstimate, query_soilgrids_textures


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
        return {
            "query_point": {"lat": lat, "lon": lon},
            "error": str(exc),
        }
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
