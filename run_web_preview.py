from __future__ import annotations

import json
import mimetypes
import os
import textwrap
import importlib.util
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

from heavywater_preview.config import (
    DEFAULT_BBOX_SIZE_KM,
    DEFAULT_COMMUNITY_THRESHOLD,
    DEFAULT_DIFFERENTIAL_MOTION_THRESHOLD,
    DEFAULT_EFAS_DAYS_BACK,
    DEFAULT_MIN_COMMUNITY_AREA_M2,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_RIVER_METRIC_LOOKBACK_DAYS,
    DEFAULT_RIVER_METRIC_RESOLUTION_M,
    DEFAULT_STABILITY_BUFFER_M,
    DEFAULT_TERRAIN_RESOLUTION_M,
    DEFAULT_WATER_SOURCE,
    INDEX_HTML_NAME,
    PROJECT_ROOT as PACKAGE_PROJECT_ROOT,
    WATER_SOURCE_EUHYDRO,
    WATER_SOURCE_OVERPASS,
)

try:
    from heavywater_preview.pipeline import run_pipeline
    HAS_GIS_PIPELINE = True
except ImportError:
    run_pipeline = None
    HAS_GIS_PIPELINE = False


PROJECT_ROOT = Path(__file__).resolve().parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"
OUTPUT_DIR = DEFAULT_OUTPUT_DIR
INDEX_PATH = OUTPUT_DIR / INDEX_HTML_NAME
PORT = 8000
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
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

        # River relations → MultiLineString
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

    # Keep it bounded — buildings can be huge in cities.
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
    warning_html = "" if not warning else f"<div class='note'>⚠ {warning}</div>"

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
          <div class="sub">Lat {lat:.5f}, Lon {lon:.5f} • AOI {size_km:g} km • Fallback preview (Overpass)</div>
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

        const aoi = L.rectangle([[{south:.6f}, {west:.6f}], [{north:.6f}, {east:.6f}]], {{ 
          color: 'rgba(15,124,107,0.95)', 
          weight: 2, 
          fillOpacity: 0.06 
        }}).addTo(map);
        
        L.circleMarker([{lat:.6f}, {lon:.6f}], {{ radius: 7, color: 'rgba(223,108,59,0.95)', weight: 2, fillOpacity: 0.3 }}).addTo(map);
        map.fitBounds(aoi.getBounds(), {{ padding: [18, 18] }});

        const waterData = {water_geojson_js};
        const communitiesData = {communities_geojson_js};

        const waterLayer = L.geoJSON(waterData, {{
          style: (f) => {{
            const t = f?.geometry?.type || '';
            const isPoly = t === 'Polygon' || t === 'MultiPolygon';
            return isPoly
              ? {{ color: 'rgba(15,124,107,0.92)', weight: 2, opacity: 0.85, fillOpacity: 0.10, fillColor: 'rgba(86,212,195,0.26)' }}
              : {{ color: 'rgba(15,124,107,0.92)', weight: 2, opacity: 0.9 }};
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
            size_km = self._optional_float(payload, "size_km", DEFAULT_BBOX_SIZE_KM)
            water_source = payload.get("water_source") or DEFAULT_WATER_SOURCE
            communities_raster = payload.get("communities_raster") or None
            community_threshold = self._optional_float(payload, "community_threshold", DEFAULT_COMMUNITY_THRESHOLD)
            min_community_area_m2 = self._optional_float(
                payload,
                "min_community_area_m2",
                DEFAULT_MIN_COMMUNITY_AREA_M2,
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

            if HAS_GIS_PIPELINE and run_pipeline:
                outputs = run_pipeline(
                    lat=lat,
                    lon=lon,
                    size_km=size_km,
                    output_dir=OUTPUT_DIR,
                    water_source=water_source,
                    communities_raster=communities_raster,
                    community_threshold=community_threshold,
                    min_community_area_m2=min_community_area_m2,
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
                )
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
        self._write_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "has_preview": INDEX_PATH.exists(),
                "preview_url": "/output/index.html" if INDEX_PATH.exists() else None,
                "has_gis_pipeline": HAS_GIS_PIPELINE,
                "defaults": {
                    "size_km": DEFAULT_BBOX_SIZE_KM,
                    "water_source": DEFAULT_WATER_SOURCE,
                    "community_threshold": DEFAULT_COMMUNITY_THRESHOLD,
                    "min_community_area_m2": DEFAULT_MIN_COMMUNITY_AREA_M2,
                    "terrain_resolution_m": DEFAULT_TERRAIN_RESOLUTION_M,
                    "river_metrics": False,
                    "river_discharge": False,
                    "river_metric_resolution_m": DEFAULT_RIVER_METRIC_RESOLUTION_M,
                    "river_metric_lookback_days": DEFAULT_RIVER_METRIC_LOOKBACK_DAYS,
                    "efas_days_back": DEFAULT_EFAS_DAYS_BACK,
                    "stability": False,
                    "stability_buffer_m": DEFAULT_STABILITY_BUFFER_M,
                    "differential_motion_threshold": DEFAULT_DIFFERENTIAL_MOTION_THRESHOLD,
                },
            },
        )

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
