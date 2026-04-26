from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import sys
import textwrap
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from urllib.parse import parse_qs
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

from heavywater_preview.config import (
    DEFAULT_BBOX_SIZE_KM,
    COMMUNITY_GPKG_NAME,
    COMMUNITIES_LAYER,
    DEFAULT_COMMUNITY_MERGE_DISTANCE_M,
    DEFAULT_COMMUNITY_PIXEL_AREA_M2,
    DEFAULT_COMMUNITY_THRESHOLD,
    DEFAULT_DIFFERENTIAL_MOTION_THRESHOLD,
    DEFAULT_EFAS_DAYS_BACK,
    DEFAULT_FARM_DEMAND_M3_DAY,
    DEFAULT_GLOFAS_DAYS_BACK,
    DEFAULT_MIN_COMMUNITY_AREA_M2,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_PEOPLE_PER_CLUSTER_PIXEL,
    DEFAULT_RIVER_METRIC_LOOKBACK_DAYS,
    DEFAULT_RIVER_METRIC_RESOLUTION_M,
    DEFAULT_STABILITY_BUFFER_M,
    DEFAULT_TERRAIN_RESOLUTION_M,
    DEFAULT_WATER_SOURCE,
    INDEX_HTML_NAME,
    MAP_HTML_NAME,
    PROJECT_ROOT as PACKAGE_PROJECT_ROOT,
    REPORT_INPUTS_NAME,
    TERRAIN_DEM_NAME,
    TERRAIN_HILLSHADE_NAME,
    WATER_GPKG_NAME,
    WATER_LINES_LAYER,
    WATER_RISK_CANALS_NAME,
    WATER_RISK_POINTS_NAME,
    WATER_RISK_SITES_NAME,
    WATER_SOURCE_EUHYDRO,
    WATER_SOURCE_OVERPASS,
)

try:
    from heavywater_preview.pipeline import run_pipeline
    HAS_GIS_PIPELINE = True
except ImportError:
    run_pipeline = None
    HAS_GIS_PIPELINE = False

try:
    from heavywater_preview.terrain import sample_terrain_point
except ImportError:
    sample_terrain_point = None

try:
    import geopandas as gpd
except ImportError:
    gpd = None

try:
    from heavywater_preview.leaflet import write_preview_map
except ImportError:
    write_preview_map = None

try:
    from heavywater_preview.mock_examples import find_mock_example, write_mock_outputs
except ImportError:
    find_mock_example = None
    write_mock_outputs = None


PROJECT_ROOT = Path(__file__).resolve().parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"
OUTPUT_DIR = DEFAULT_OUTPUT_DIR
INDEX_PATH = OUTPUT_DIR / INDEX_HTML_NAME
GUIDELINE_REPORT_PATH = OUTPUT_DIR / "romania_water_legal_guideline.docx"
CASE_STUDY_REPORT_PATH = OUTPUT_DIR / "technical_feasibility_case_study_template.docx"
PORT = 8000
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)


def _bundled_document_python() -> Path | None:
    candidate = (
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "python"
        / "python.exe"
    )
    return candidate if candidate.exists() else None


try:
    bundled_site_packages = (
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "python"
        / "Lib"
        / "site-packages"
    )
    if bundled_site_packages.exists():
        sys.path.insert(0, str(bundled_site_packages))
    from tools.build_water_reports_docx import build_case_study, build_guideline
except ImportError:
    def _build_reports_with_bundled_python() -> None:
        python_exe = _bundled_document_python()
        if python_exe is None:
            raise RuntimeError("Report generation requires python-docx or the bundled document Python runtime.")
        subprocess.run(
            [str(python_exe), str(PROJECT_ROOT / "tools" / "build_water_reports_docx.py")],
            cwd=str(PROJECT_ROOT),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def build_guideline():
        _build_reports_with_bundled_python()
        return GUIDELINE_REPORT_PATH

    def build_case_study():
        _build_reports_with_bundled_python()
        return CASE_STUDY_REPORT_PATH


def _can_rebuild_preview_from_existing_outputs() -> bool:
    required = [
        OUTPUT_DIR / WATER_GPKG_NAME,
        OUTPUT_DIR / COMMUNITY_GPKG_NAME,
    ]
    return gpd is not None and write_preview_map is not None and all(path.exists() for path in required)


def _rewrite_preview_from_existing_outputs(lat: float, lon: float, size_km: float):
    if not _can_rebuild_preview_from_existing_outputs():
        raise RuntimeError("Existing geospatial outputs are not complete enough to rebuild the preview.")

    water_lines = gpd.read_file(OUTPUT_DIR / WATER_GPKG_NAME, layer=WATER_LINES_LAYER)
    communities = gpd.read_file(OUTPUT_DIR / COMMUNITY_GPKG_NAME, layer=COMMUNITIES_LAYER)
    water_risk_mode = None
    report_inputs_path = OUTPUT_DIR / REPORT_INPUTS_NAME
    if report_inputs_path.exists():
        try:
            payload = json.loads(report_inputs_path.read_text(encoding="utf-8"))
            water_risk_mode = ((payload.get("water_risk") or {}).get("mode"))
        except (TypeError, ValueError, json.JSONDecodeError):
            water_risk_mode = None

    water_risk_points = None
    if (OUTPUT_DIR / WATER_RISK_POINTS_NAME).exists():
        water_risk_points = gpd.read_file(OUTPUT_DIR / WATER_RISK_POINTS_NAME)
        if water_risk_mode == "community" and not water_risk_points.empty:
            communities = water_risk_points
            water_risk_points = None

    canal_paths = None
    if (OUTPUT_DIR / WATER_RISK_CANALS_NAME).exists():
        canal_paths = gpd.read_file(OUTPUT_DIR / WATER_RISK_CANALS_NAME)

    feasibility_sites = None
    if (OUTPUT_DIR / WATER_RISK_SITES_NAME).exists():
        feasibility_sites = gpd.read_file(OUTPUT_DIR / WATER_RISK_SITES_NAME)

    bbox_wgs84 = _approx_bbox(lat, lon, size_km)
    map_html_path = OUTPUT_DIR / MAP_HTML_NAME
    index_html_path = OUTPUT_DIR / INDEX_HTML_NAME
    terrain_dem_raster = OUTPUT_DIR / TERRAIN_DEM_NAME if (OUTPUT_DIR / TERRAIN_DEM_NAME).exists() else None
    terrain_hillshade_raster = OUTPUT_DIR / TERRAIN_HILLSHADE_NAME if (OUTPUT_DIR / TERRAIN_HILLSHADE_NAME).exists() else None

    write_preview_map(
        html_path=map_html_path,
        index_path=index_html_path,
        lat=lat,
        lon=lon,
        bbox_wgs84=bbox_wgs84,
        water_lines=water_lines,
        communities=communities,
        terrain_dem_raster=terrain_dem_raster,
        terrain_hillshade_raster=terrain_hillshade_raster,
        terrain_query_data=None,
        water_risk_points=water_risk_points,
        canal_paths=canal_paths,
        feasibility_sites=feasibility_sites,
    )
    return SimpleNamespace(map_html_path=map_html_path, index_html_path=index_html_path, output_dir=OUTPUT_DIR)


def _cached_outputs_match_request(lat: float, lon: float, size_km: float, tolerance: float = 1e-4) -> bool:
    report_inputs_path = OUTPUT_DIR / REPORT_INPUTS_NAME
    if not report_inputs_path.exists():
        return False
    try:
        payload = json.loads(report_inputs_path.read_text(encoding="utf-8"))
        location = payload.get("location") or {}
        cached_lat = float(location.get("lat"))
        cached_lon = float(location.get("lon"))
        cached_size = float(location.get("size_km"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return (
        abs(cached_lat - float(lat)) <= tolerance
        and abs(cached_lon - float(lon)) <= tolerance
        and abs(cached_size - float(size_km)) <= tolerance
    )


def _load_dotenv() -> None:
    """
    Keep the web server startup lightweight.

    We intentionally avoid importing `heavywater_preview.cli` here because it eagerly imports the
    geospatial pipeline (Fiona/GeoPandas/Rasterio), which may not be available on newer Python
    versions (e.g. 3.14) even when you only want to view the frontend.
    """
    for dotenv_path in (PACKAGE_PROJECT_ROOT / ".env", PACKAGE_PROJECT_ROOT / ".env.local"):
        if not dotenv_path.exists():
            continue
        for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = value


def _approx_bbox(lat: float, lon: float, size_km: float) -> tuple[float, float, float, float]:
    """
    Lightweight bbox approximation for the 3.14 fallback generator.
    Returns (south, west, north, east) in degrees.
    """
    half_km = max(float(size_km), 1.0) / 2.0
    dlat = half_km / 111.32
    cos_lat = max(0.2, abs(__import__("math").cos(__import__("math").radians(lat))))
    dlon = half_km / (111.32 * cos_lat)
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


def _fetch_overpass_water_geojson(south: float, west: float, north: float, east: float) -> dict:
    """
    Fetch a small set of water features from Overpass and return as GeoJSON.
    Uses only stdlib so it can work on newer Python versions without compiled deps.
    """
    # Keep it conservative to avoid huge responses.
    query = f"""
    [out:json][timeout:25];
    (
      way["waterway"~"river|stream|canal|ditch"]({south},{west},{north},{east});
      relation["waterway"~"river|stream|canal|ditch"]({south},{west},{north},{east});
      way["natural"="water"]({south},{west},{north},{east});
      relation["natural"="water"]({south},{west},{north},{east});
    );
    out body;
    >;
    out skel qt;
    """
    # Overpass expects application/x-www-form-urlencoded; add headers to avoid 406 from stricter instances.
    form_body = ("data=" + __import__("urllib.parse").parse.quote(textwrap.dedent(query).strip())).encode("utf-8")
    last_exc: Exception | None = None
    payload: dict | None = None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            req = Request(
                endpoint,
                data=form_body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                    "Accept": "application/json",
                    "User-Agent": "HeavyWaterPreview/1.0 (local)",
                },
                method="POST",
            )
            with urlopen(req, timeout=50) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except HTTPError as exc:
            last_exc = exc
            # 406 tends to be instance-specific; try the next endpoint.
            if exc.code == 406:
                continue
            raise
        except (URLError, TimeoutError, ValueError) as exc:
            last_exc = exc
            continue
    if payload is None:
        raise last_exc or RuntimeError("Overpass fetch failed.")

    nodes: dict[int, tuple[float, float]] = {}
    ways: list[dict] = []
    relations: list[dict] = []
    for el in payload.get("elements", []):
        t = el.get("type")
        if t == "node":
            nodes[int(el["id"])] = (float(el["lon"]), float(el["lat"]))
        elif t == "way":
            ways.append(el)
        elif t == "relation":
            relations.append(el)

    features: list[dict] = []

    def build_way_coords(way: dict) -> list[tuple[float, float]]:
        coords = [nodes.get(int(n)) for n in way.get("nodes", [])]
        return [c for c in coords if c is not None]

    def tags_to_props(tags: dict | None) -> dict:
        tags = tags or {}
        props = {k: v for k, v in tags.items() if k in ("name", "waterway", "natural")}
        return props

    def way_to_feature(way: dict) -> dict | None:
        coords = build_way_coords(way)
        if len(coords) < 2:
            return None
        is_polygon = coords[0] == coords[-1] and len(coords) >= 4
        geom = {"type": "Polygon", "coordinates": [coords]} if is_polygon else {"type": "LineString", "coordinates": coords}
        return {"type": "Feature", "properties": tags_to_props(way.get("tags")), "geometry": geom}

    def stitch_rings(way_coord_list: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
        """
        Join way segments into rings (best-effort).
        Returns a list of closed rings.
        """
        remaining = [seg[:] for seg in way_coord_list if len(seg) >= 2]
        rings: list[list[tuple[float, float]]] = []

        while remaining:
            cur = remaining.pop(0)
            changed = True
            while changed and remaining:
                changed = False
                end = cur[-1]
                start = cur[0]
                for i, seg in enumerate(remaining):
                    if seg[0] == end:
                        cur.extend(seg[1:])
                        remaining.pop(i)
                        changed = True
                        break
                    if seg[-1] == end:
                        cur.extend(list(reversed(seg[:-1])))
                        remaining.pop(i)
                        changed = True
                        break
                    if seg[-1] == start:
                        cur = seg[:-1] + cur
                        remaining.pop(i)
                        changed = True
                        break
                    if seg[0] == start:
                        cur = list(reversed(seg[1:])) + cur
                        remaining.pop(i)
                        changed = True
                        break

            if len(cur) >= 4 and cur[0] == cur[-1]:
                rings.append(cur)

        return rings

    ways_by_id: dict[int, dict] = {int(w.get("id")): w for w in ways if "id" in w}

    # Include relation multipolygons (lakes/reservoirs) and relation rivers where possible.
    for rel in relations[:120]:
        tags = rel.get("tags") or {}
        members = rel.get("members") or []
        rel_type = tags.get("type")
        props = tags_to_props(tags)

        member_way_coords: list[list[tuple[float, float]]] = []
        for m in members:
            if m.get("type") != "way":
                continue
            way = ways_by_id.get(int(m.get("ref", 0)))
            if not way:
                continue
            coords = build_way_coords(way)
            if len(coords) >= 2:
                member_way_coords.append(coords)

        if not member_way_coords:
            continue

        # Multipolygon water bodies
        if rel_type in ("multipolygon", "boundary") and tags.get("natural") == "water":
            rings = stitch_rings(member_way_coords)
            if rings:
                features.append(
                    {
                        "type": "Feature",
                        "properties": {**props, "layer": "water"},
                        "geometry": {"type": "MultiPolygon", "coordinates": [[ring] for ring in rings]},
                    }
                )
            continue

        # River relations â†’ MultiLineString
        if tags.get("waterway") in ("river", "stream", "canal", "ditch"):
            features.append(
                {
                    "type": "Feature",
                    "properties": {**props, "layer": "water"},
                    "geometry": {"type": "MultiLineString", "coordinates": member_way_coords},
                }
            )

    for way in ways[:420]:
        feat = way_to_feature(way)
        if feat:
            feat["properties"]["layer"] = "water"
            features.append(feat)

    return {"type": "FeatureCollection", "features": features}


def _fetch_overpass_communities_geojson(south: float, west: float, north: float, east: float) -> dict:
    """
    Approximate "Communities" using OSM landuse/building polygons.
    This is a fallback for Python 3.14 where the raster-based extraction pipeline may be unavailable.
    """
    query = f"""
    [out:json][timeout:25];
    (
      way["landuse"~"residential|commercial|industrial|retail"]({south},{west},{north},{east});
      relation["landuse"~"residential|commercial|industrial|retail"]({south},{west},{north},{east});
      way["building"]({south},{west},{north},{east});
      relation["building"]({south},{west},{north},{east});
    );
    out body;
    >;
    out skel qt;
    """
    form_body = ("data=" + __import__("urllib.parse").parse.quote(textwrap.dedent(query).strip())).encode("utf-8")
    last_exc: Exception | None = None
    payload: dict | None = None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            req = Request(
                endpoint,
                data=form_body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                    "Accept": "application/json",
                    "User-Agent": "HeavyWaterPreview/1.0 (local)",
                },
                method="POST",
            )
            with urlopen(req, timeout=50) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except HTTPError as exc:
            last_exc = exc
            if exc.code == 406:
                continue
            raise
        except (URLError, TimeoutError, ValueError) as exc:
            last_exc = exc
            continue
    if payload is None:
        raise last_exc or RuntimeError("Overpass fetch failed.")

    nodes: dict[int, tuple[float, float]] = {}
    ways: list[dict] = []
    relations: list[dict] = []
    for el in payload.get("elements", []):
        t = el.get("type")
        if t == "node":
            nodes[int(el["id"])] = (float(el["lon"]), float(el["lat"]))
        elif t == "way":
            ways.append(el)
        elif t == "relation":
            relations.append(el)

    features: list[dict] = []

    def build_way_coords(way: dict) -> list[tuple[float, float]]:
        coords = [nodes.get(int(n)) for n in way.get("nodes", [])]
        return [c for c in coords if c is not None]

    def tags_to_props(tags: dict | None) -> dict:
        tags = tags or {}
        props = {k: v for k, v in tags.items() if k in ("name", "landuse", "building")}
        return props

    def way_to_polygon_feature(way: dict) -> dict | None:
        coords = build_way_coords(way)
        if len(coords) < 4:
            return None
        if coords[0] != coords[-1]:
            # Only closed rings for the communities layer.
            return None
        return {
            "type": "Feature",
            "properties": {**tags_to_props(way.get("tags")), "layer": "communities"},
            "geometry": {"type": "Polygon", "coordinates": [coords]},
        }

    def stitch_rings(way_coord_list: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
        remaining = [seg[:] for seg in way_coord_list if len(seg) >= 2]
        rings: list[list[tuple[float, float]]] = []
        while remaining:
            cur = remaining.pop(0)
            changed = True
            while changed and remaining:
                changed = False
                end = cur[-1]
                start = cur[0]
                for i, seg in enumerate(remaining):
                    if seg[0] == end:
                        cur.extend(seg[1:])
                        remaining.pop(i)
                        changed = True
                        break
                    if seg[-1] == end:
                        cur.extend(list(reversed(seg[:-1])))
                        remaining.pop(i)
                        changed = True
                        break
                    if seg[-1] == start:
                        cur = seg[:-1] + cur
                        remaining.pop(i)
                        changed = True
                        break
                    if seg[0] == start:
                        cur = list(reversed(seg[1:])) + cur
                        remaining.pop(i)
                        changed = True
                        break
            if len(cur) >= 4 and cur[0] == cur[-1]:
                rings.append(cur)
        return rings

    ways_by_id: dict[int, dict] = {int(w.get("id")): w for w in ways if "id" in w}

    # Add relation multipolygons for landuse/building areas (best-effort).
    for rel in relations[:140]:
        tags = rel.get("tags") or {}
        rel_type = tags.get("type")
        if rel_type not in ("multipolygon", "boundary"):
            continue
        if not (tags.get("landuse") or tags.get("building")):
            continue
        members = rel.get("members") or []
        member_way_coords: list[list[tuple[float, float]]] = []
        for m in members:
            if m.get("type") != "way":
                continue
            way = ways_by_id.get(int(m.get("ref", 0)))
            if not way:
                continue
            coords = build_way_coords(way)
            if len(coords) >= 2:
                member_way_coords.append(coords)
        rings = stitch_rings(member_way_coords)
        if rings:
            features.append(
                {
                    "type": "Feature",
                    "properties": {**tags_to_props(tags), "layer": "communities"},
                    "geometry": {"type": "MultiPolygon", "coordinates": [[ring] for ring in rings]},
                }
            )

    # Keep it bounded â€” buildings can be huge in cities.
    for way in ways[:380]:
        feat = way_to_polygon_feature(way)
        if feat:
            features.append(feat)

    return {"type": "FeatureCollection", "features": features}


def _write_fallback_preview(lat: float, lon: float, size_km: float, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    south, west, north, east = _approx_bbox(lat, lon, size_km)
    water_geojson: dict | None = None
    communities_geojson: dict | None = None
    warning: str | None = None
    try:
        water_geojson = _fetch_overpass_water_geojson(south, west, north, east)
        communities_geojson = _fetch_overpass_communities_geojson(south, west, north, east)
    except (URLError, HTTPError, TimeoutError, ValueError) as exc:
        warning = f"Overpass fetch failed: {exc}"

    map_html_path = output_dir / "map_preview.html"
    index_html_path = output_dir / "index.html"

    water_geojson_js = "null" if water_geojson is None else json.dumps(water_geojson)
    communities_geojson_js = "null" if communities_geojson is None else json.dumps(communities_geojson)
    warning_html = "" if not warning else f"<div class='note'>âš  {warning}</div>"

    html = f"""<!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8"/>
      <meta name="viewport" content="width=device-width, initial-scale=1"/>
      <title>HeavyWater Preview</title>
      <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css">
      <style>
        :root {{ color-scheme: light; }}
        body {{ margin:0; font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; background:#f4f7f1; color:#0e1b1a; }}
        header {{ padding:14px 16px; border-bottom:1px solid rgba(18,56,44,.12); background:rgba(255,255,255,.86); backdrop-filter: blur(10px); }}
        header strong {{ font-size: 14px; letter-spacing: .12em; text-transform: uppercase; }}
        .wrap {{ max-width: 1100px; margin: 0 auto; }}
        .sub {{ color: rgba(14,27,26,.62); font-size: 14px; margin-top: 6px; }}
        #map {{ height: calc(100vh - 86px); }}
        .note {{ margin-top: 10px; padding: 10px 12px; border:1px solid rgba(18,56,44,.12); border-radius: 12px; background: rgba(255,255,255,.8); }}
      </style>
    </head>
    <body>
      <header>
        <div class="wrap">
          <strong>HeavyWater Preview</strong>
          <div class="sub">Lat {lat:.5f}, Lon {lon:.5f} â€¢ AOI {size_km:g} km â€¢ Fallback preview (Overpass)</div>
          {warning_html}
        </div>
      </header>
      <div id="map" aria-label="HeavyWater preview map"></div>
      <script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js"></script>
      <script>
        const map = L.map('map', {{ zoomControl: true }}).setView([{lat:.6f}, {lon:.6f}], 11);

        const osm = L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
          maxZoom: 19,
          attribution: '&copy; OpenStreetMap contributors'
        }}).addTo(map);

        const satellite = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
          attribution: 'Esri',
          maxZoom: 19
        }});

        const waterData = {water_geojson_js};
        const communitiesData = {communities_geojson_js};

        const waterLayer = L.geoJSON(waterData, {{
          style: (f) => {{
            const t = f?.geometry?.type || '';
            const isPoly = t === 'Polygon' || t === 'MultiPolygon';
            return isPoly
              ? {{ color: '#0057ff', weight: 2, opacity: 0.85, fillOpacity: 0.10, fillColor: '#0057ff' }}
              : {{ color: '#0057ff', weight: 3, opacity: 0.9 }};
          }},
          onEachFeature: (feature, layer) => {{
            if (feature.properties && feature.properties.name) {{
              layer.bindTooltip(feature.properties.name, {{ sticky: true }});
            }}
          }}
        }});

        const communitiesLayer = L.geoJSON(communitiesData, {{
          style: () => ({{ color: 'rgba(223,108,59,0.78)', weight: 1, opacity: 0.75, fillOpacity: 0.10, fillColor: 'rgba(223,108,59,0.22)' }}),
          onEachFeature: (feature, layer) => {{
            if (feature.properties && (feature.properties.landuse || feature.properties.name)) {{
              layer.bindTooltip(feature.properties.name || feature.properties.landuse, {{ sticky: true }});
            }}
          }}
        }});

        const baseMaps = {{
          "Street Map": osm,
          "Satellite": satellite
        }};

        const overlays = {{}};
        if (waterData && waterData.features && waterData.features.length) {{
          overlays["Water"] = waterLayer.addTo(map);
        }}
        if (communitiesData && communitiesData.features && communitiesData.features.length) {{
          overlays["Communities"] = communitiesLayer.addTo(map);
        }}

        L.control.layers(baseMaps, overlays, {{ collapsed: false }}).addTo(map);
        L.control.scale({{ position: 'bottomleft' }}).addTo(map);
      </script>
    </body>
    </html>
    """

    map_html_path.write_text(textwrap.dedent(html), encoding="utf-8")
    # The frontend expects an index.html; keep it as a minimal wrapper around the map.
    index_html_path.write_text(textwrap.dedent(html), encoding="utf-8")
    return map_html_path, index_html_path


class PreviewRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory: str | None = None, **kwargs):
        super().__init__(*args, directory=str(PROJECT_ROOT), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            self._handle_status()
            return
        if parsed.path == "/api/terrain-query":
            self._handle_terrain_query(parsed.query)
            return
        if parsed.path == "/":
            self.path = "/frontend/index.html"
        elif parsed.path == "/app.js":
            self.path = "/frontend/app.js"
        elif parsed.path == "/styles.css":
            self.path = "/frontend/styles.css"
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/generate":
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint.")
            return

        try:
            payload = self._read_json_body()
            lat = self._require_float(payload, "lat")
            lon = self._require_float(payload, "lon")
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                raise ValueError("Latitude must be -90 to 90 and longitude must be -180 to 180.")
            size_km = self._optional_float(payload, "size_km", DEFAULT_BBOX_SIZE_KM)
            if size_km <= 0:
                raise ValueError("AOI size must be a positive number.")
            water_source = payload.get("water_source") or DEFAULT_WATER_SOURCE
            communities_raster = payload.get("communities_raster") or None
            community_threshold = self._optional_float(payload, "community_threshold", DEFAULT_COMMUNITY_THRESHOLD)
            min_community_area_m2 = self._optional_float(
                payload,
                "min_community_area_m2",
                DEFAULT_MIN_COMMUNITY_AREA_M2,
            )
            community_merge_distance_m = self._optional_float(
                payload,
                "community_merge_distance_m",
                DEFAULT_COMMUNITY_MERGE_DISTANCE_M,
            )
            include_terrain = bool(payload.get("terrain", False))
            terrain_resolution_m = self._optional_float(
                payload,
                "terrain_resolution_m",
                DEFAULT_TERRAIN_RESOLUTION_M,
            )

            # Hydrology parameters
            include_river_metrics = bool(payload.get("river_metrics", False))
            include_river_discharge = bool(payload.get("river_discharge", False))
            river_metric_resolution_m = self._optional_float(
                payload, "river_metric_resolution_m", DEFAULT_RIVER_METRIC_RESOLUTION_M
            )
            river_metric_lookback_days = int(self._optional_float(
                payload, "river_metric_lookback_days", float(DEFAULT_RIVER_METRIC_LOOKBACK_DAYS)
            ))
            efas_days_back = int(self._optional_float(
                payload, "efas_days_back", float(DEFAULT_EFAS_DAYS_BACK)
            ))

            # Stability parameters
            include_stability = bool(payload.get("stability", False))
            egms_ortho_vertical = payload.get("egms_ortho_vertical") or None
            stability_buffer_m = self._optional_float(
                payload, "stability_buffer_m", DEFAULT_STABILITY_BUFFER_M
            )
            differential_motion_threshold = self._optional_float(
                payload, "differential_motion_threshold", DEFAULT_DIFFERENTIAL_MOTION_THRESHOLD
            )
            include_water_risk = bool(payload.get("water_risk", False))
            water_risk_mode = payload.get("water_risk_mode") or "community"
            if water_risk_mode not in {"community", "farm"}:
                raise ValueError("Risk mode must be either 'community' or 'farm'.")
            farm_demand_m3_day = self._optional_float(
                payload, "farm_demand_m3_day", DEFAULT_FARM_DEMAND_M3_DAY
            )
            if include_water_risk and water_risk_mode == "farm" and farm_demand_m3_day <= 0:
                raise ValueError("Farm demand must be greater than zero for farm risk mode.")
            cluster_pixel_area_m2 = self._optional_float(
                payload, "cluster_pixel_area_m2", DEFAULT_COMMUNITY_PIXEL_AREA_M2
            )
            people_per_cluster_pixel = self._optional_float(
                payload, "people_per_cluster_pixel", DEFAULT_PEOPLE_PER_CLUSTER_PIXEL
            )
            glofas_days_back = int(self._optional_float(
                payload, "glofas_days_back", float(DEFAULT_GLOFAS_DAYS_BACK)
            ))

            mock_example = None
            mock_writer = write_mock_outputs
            try:
                from heavywater_preview.mock_examples import find_mock_example as local_find_mock_example
                from heavywater_preview.mock_examples import write_mock_outputs as local_write_mock_outputs

                mock_example = local_find_mock_example(lat, lon)
                mock_writer = local_write_mock_outputs
            except Exception:
                mock_example = find_mock_example(lat, lon) if find_mock_example else None
            if mock_example and mock_writer:
                outputs = mock_writer(example=mock_example, output_dir=OUTPUT_DIR, size_km=size_km)
            elif HAS_GIS_PIPELINE and run_pipeline:
                try:
                    outputs = run_pipeline(
                        lat=lat,
                        lon=lon,
                        size_km=size_km,
                        output_dir=OUTPUT_DIR,
                        water_source=water_source,
                        communities_raster=communities_raster,
                        community_threshold=community_threshold,
                        min_community_area_m2=min_community_area_m2,
                        community_merge_distance_m=community_merge_distance_m,
                        include_terrain=include_terrain,
                        terrain_resolution_m=terrain_resolution_m,
                        include_river_metrics=include_river_metrics,
                        include_river_discharge=include_river_discharge,
                        river_metric_resolution_m=river_metric_resolution_m,
                        river_metric_lookback_days=river_metric_lookback_days,
                        efas_days_back=efas_days_back,
                        include_stability=include_stability,
                        egms_ortho_vertical=egms_ortho_vertical,
                        stability_buffer_m=stability_buffer_m,
                        differential_motion_threshold_mm_per_year=differential_motion_threshold,
                        include_water_risk=include_water_risk,
                        water_risk_mode=water_risk_mode,
                        farm_demand_m3_day=farm_demand_m3_day,
                        cluster_pixel_area_m2=cluster_pixel_area_m2,
                        people_per_cluster_pixel=people_per_cluster_pixel,
                        glofas_days_back=glofas_days_back,
                    )
                except Exception as pipeline_exc:
                    pipeline_error_text = str(pipeline_exc)
                    recoverable_pipeline_failure = any(
                        marker in pipeline_error_text
                        for marker in (
                            "Copernicus Data Space access requires OAuth credentials",
                            "forbidden by its access permissions",
                            "WinError 10013",
                        )
                    )
                    if (
                        recoverable_pipeline_failure
                        and _can_rebuild_preview_from_existing_outputs()
                        and _cached_outputs_match_request(lat=lat, lon=lon, size_km=size_km)
                    ):
                        outputs = _rewrite_preview_from_existing_outputs(lat=lat, lon=lon, size_km=size_km)
                    else:
                        raise
            else:
                # Fallback generator (Overpass-only)
                map_html_path, index_html_path = _write_fallback_preview(
                    lat=lat,
                    lon=lon,
                    size_km=size_km,
                    output_dir=OUTPUT_DIR,
                )
                outputs = type(
                    "FallbackOutputs",
                    (),
                    {
                        "map_html_path": map_html_path,
                        "index_html_path": index_html_path,
                        "output_dir": OUTPUT_DIR,
                    },
                )()
            if build_guideline and not GUIDELINE_REPORT_PATH.exists():
                build_guideline()
            if build_case_study and HAS_GIS_PIPELINE:
                build_case_study()
        except Exception as exc:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "ok": False,
                    "error": str(exc),
                },
            )
            return

        self._write_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "lat": lat,
                "lon": lon,
                "size_km": size_km,
                "map_url": self._public_path(outputs.map_html_path),
                "index_url": self._public_path(outputs.index_html_path),
                "output_dir": str(outputs.output_dir),
            },
        )

    def guess_type(self, path: str) -> str:
        if path.endswith(".js"):
            return "application/javascript; charset=utf-8"
        if path.endswith(".css"):
            return "text/css; charset=utf-8"
        if path.endswith(".json"):
            return "application/json; charset=utf-8"
        return mimetypes.guess_type(path)[0] or "application/octet-stream"

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args) -> None:
        super().log_message(format, *args)

    def _handle_status(self) -> None:
        if build_guideline and not GUIDELINE_REPORT_PATH.exists():
            try:
                build_guideline()
            except Exception:
                pass
        if build_case_study and not CASE_STUDY_REPORT_PATH.exists() and (OUTPUT_DIR / REPORT_INPUTS_NAME).exists():
            try:
                build_case_study()
            except Exception:
                pass
        self._write_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "has_preview": INDEX_PATH.exists(),
                "preview_url": "/output/index.html" if INDEX_PATH.exists() else None,
                "has_gis_pipeline": HAS_GIS_PIPELINE,
                "documents": {
                    "guideline": {
                        "available": GUIDELINE_REPORT_PATH.exists(),
                        "url": self._public_path(GUIDELINE_REPORT_PATH) if GUIDELINE_REPORT_PATH.exists() else None,
                        "label": "Romania legal guideline",
                    },
                    "case_study": {
                        "available": CASE_STUDY_REPORT_PATH.exists(),
                        "url": self._public_path(CASE_STUDY_REPORT_PATH) if CASE_STUDY_REPORT_PATH.exists() else None,
                        "label": "Technical feasibility report",
                    },
                },
                "defaults": {
                    "size_km": DEFAULT_BBOX_SIZE_KM,
                    "water_source": DEFAULT_WATER_SOURCE,
                    "community_threshold": DEFAULT_COMMUNITY_THRESHOLD,
                    "min_community_area_m2": DEFAULT_MIN_COMMUNITY_AREA_M2,
                    "community_merge_distance_m": DEFAULT_COMMUNITY_MERGE_DISTANCE_M,
                    "terrain_resolution_m": DEFAULT_TERRAIN_RESOLUTION_M,
                    "river_metrics": False,
                    "river_discharge": False,
                    "river_metric_resolution_m": DEFAULT_RIVER_METRIC_RESOLUTION_M,
                    "river_metric_lookback_days": DEFAULT_RIVER_METRIC_LOOKBACK_DAYS,
                    "efas_days_back": DEFAULT_EFAS_DAYS_BACK,
                    "stability": False,
                    "stability_buffer_m": DEFAULT_STABILITY_BUFFER_M,
                    "differential_motion_threshold": DEFAULT_DIFFERENTIAL_MOTION_THRESHOLD,
                    "water_risk": False,
                    "water_risk_mode": "community",
                    "farm_demand_m3_day": DEFAULT_FARM_DEMAND_M3_DAY,
                    "cluster_pixel_area_m2": DEFAULT_COMMUNITY_PIXEL_AREA_M2,
                    "people_per_cluster_pixel": DEFAULT_PEOPLE_PER_CLUSTER_PIXEL,
                    "glofas_days_back": DEFAULT_GLOFAS_DAYS_BACK,
                },
            },
        )

    def _handle_terrain_query(self, query: str) -> None:
        if sample_terrain_point is None or not (OUTPUT_DIR / TERRAIN_DEM_NAME).exists():
            self._write_json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": "Terrain sampling is unavailable for the current preview."},
            )
            return
        params = parse_qs(query)
        try:
            lat = float(params.get("lat", [None])[0])
            lon = float(params.get("lon", [None])[0])
        except (TypeError, ValueError):
            self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "lat and lon are required numeric query parameters."})
            return
        try:
            payload = sample_terrain_point(OUTPUT_DIR / TERRAIN_DEM_NAME, lat=lat, lon=lon)
        except Exception as exc:
            self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        self._write_json(HTTPStatus.OK, {"ok": True, **payload})

    def _read_json_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        if not raw_body:
            return {}
        return json.loads(raw_body.decode("utf-8"))

    def _require_float(self, payload: dict, key: str) -> float:
        if key not in payload:
            raise ValueError(f"Missing required field: {key}")
        return float(payload[key])

    def _optional_float(self, payload: dict, key: str, default: float) -> float:
        value = payload.get(key)
        return default if value in (None, "") else float(value)

    def _public_path(self, path: Path) -> str:
        return "/" + path.relative_to(PROJECT_ROOT).as_posix()

    def _write_json(self, status: HTTPStatus, payload: dict) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    _load_dotenv()
    FRONTEND_DIR.mkdir(parents=True, exist_ok=True)
    os.chdir(PROJECT_ROOT)

    handler = partial(PreviewRequestHandler, directory=str(PROJECT_ROOT))
    with ThreadingHTTPServer(("127.0.0.1", PORT), handler) as server:
        try:
            print(f"http://127.0.0.1:{PORT}", flush=True)
        except OSError:
            pass
        server.serve_forever()


if __name__ == "__main__":
    main()
