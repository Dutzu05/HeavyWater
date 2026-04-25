from __future__ import annotations

import json
from pathlib import Path

import folium
import numpy as np
import rasterio
from rasterio.vrt import WarpedVRT


def write_preview_map(
    html_path: Path,
    index_path: Path,
    lat: float,
    lon: float,
    bbox_wgs84: tuple[float, float, float, float],
    water_lines,
    communities,
    terrain_dem_raster: Path | None = None,
    terrain_hillshade_raster: Path | None = None,
    terrain_query_data: dict | None = None,
) -> None:
    fmap = folium.Map(location=[lat, lon], zoom_start=12, tiles="OpenStreetMap")

    if terrain_dem_raster is not None and terrain_hillshade_raster is not None:
        terrain_group = folium.FeatureGroup(name="Terrain", show=True)
        overlay_image, overlay_bounds = build_terrain_overlay(terrain_dem_raster, terrain_hillshade_raster)
        folium.raster_layers.ImageOverlay(
            image=overlay_image,
            bounds=overlay_bounds,
            interactive=False,
            opacity=0.95,
        ).add_to(terrain_group)
        terrain_group.add_to(fmap)

    community_group = folium.FeatureGroup(name="Communities", show=True)
    if not communities.empty:
        folium.GeoJson(
            data=json.loads(communities.to_crs("EPSG:4326").to_json(default=str)),
            style_function=lambda _: {"color": "#8f1d14", "fillColor": "#d7301f", "weight": 1, "fillOpacity": 0.65, "opacity": 0.95},
            tooltip=folium.GeoJsonTooltip(fields=["area_m2"], aliases=["Area m2"], localize=True),
        ).add_to(community_group)
    community_group.add_to(fmap)

    rivers_group = folium.FeatureGroup(name="Rivers", show=True)
    if not water_lines.empty:
        river_popup_fields, river_popup_aliases = _river_popup_fields(water_lines)
        folium.GeoJson(
            data=json.loads(_format_river_properties(water_lines).to_crs("EPSG:4326").to_json(default=str)),
            style_function=lambda _: {"color": "#0057ff", "weight": 4, "opacity": 0.95},
            popup=folium.GeoJsonPopup(fields=river_popup_fields, aliases=river_popup_aliases, localize=True, labels=True),
        ).add_to(rivers_group)
    rivers_group.add_to(fmap)

    fmap.fit_bounds([[bbox_wgs84[1], bbox_wgs84[0]], [bbox_wgs84[3], bbox_wgs84[2]]])
    folium.LayerControl(collapsed=False).add_to(fmap)
    if terrain_query_data is not None:
        fmap.get_root().script.add_child(
            folium.Element(_terrain_click_script(fmap.get_name(), terrain_query_data))
        )
    fmap.save(html_path)
    index_path.write_text(html_path.read_text(encoding="utf-8"), encoding="utf-8")


def build_terrain_overlay(dem_raster_path: Path, hillshade_raster_path: Path) -> tuple[np.ndarray, list[list[float]]]:
    with rasterio.open(dem_raster_path) as dem_src:
        with WarpedVRT(dem_src, crs="EPSG:4326") as dem_vrt:
            dem = dem_vrt.read(1, masked=True).astype("float32").filled(np.nan)
            west, south, east, north = dem_vrt.bounds

    with rasterio.open(hillshade_raster_path) as shade_src:
        with WarpedVRT(
            shade_src,
            crs="EPSG:4326",
            width=dem.shape[1],
            height=dem.shape[0],
            transform=rasterio.transform.from_bounds(west, south, east, north, dem.shape[1], dem.shape[0]),
        ) as shade_vrt:
            hillshade = shade_vrt.read(1, masked=True).astype("float32").filled(np.nan)

    rgba = np.zeros((dem.shape[0], dem.shape[1], 4), dtype="uint8")
    finite = np.isfinite(dem) & np.isfinite(hillshade)
    if not finite.any():
        return rgba, [[south, west], [north, east]]

    elevation = dem[finite]
    p2 = float(np.percentile(elevation, 2))
    p98 = float(np.percentile(elevation, 98))
    if p98 > p2:
        normalized = (dem - p2) / (p98 - p2)
    else:
        normalized = np.zeros_like(dem, dtype="float32")
    normalized = np.clip(normalized, 0.0, 1.0)

    colorized = _hypsometric_tint(normalized)
    shade = np.clip(hillshade / 255.0, 0.0, 1.0)
    shade = 0.35 + 0.9 * shade

    rgba[..., 0] = np.where(finite, np.clip(colorized[..., 0] * shade, 0.0, 255.0), 0).astype("uint8")
    rgba[..., 1] = np.where(finite, np.clip(colorized[..., 1] * shade, 0.0, 255.0), 0).astype("uint8")
    rgba[..., 2] = np.where(finite, np.clip(colorized[..., 2] * shade, 0.0, 255.0), 0).astype("uint8")
    rgba[..., 3] = np.where(finite, 220, 0).astype("uint8")

    return rgba, [[south, west], [north, east]]


def _hypsometric_tint(normalized: np.ndarray) -> np.ndarray:
    stops = np.array(
        [
            [0.00, 193, 214, 170],
            [0.18, 181, 205, 151],
            [0.38, 210, 196, 156],
            [0.58, 171, 160, 126],
            [0.78, 142, 127, 101],
            [1.00, 240, 240, 240],
        ],
        dtype="float32",
    )

    flat = normalized.ravel()
    red = np.interp(flat, stops[:, 0], stops[:, 1]).reshape(normalized.shape)
    green = np.interp(flat, stops[:, 0], stops[:, 2]).reshape(normalized.shape)
    blue = np.interp(flat, stops[:, 0], stops[:, 3]).reshape(normalized.shape)
    return np.stack([red, green, blue], axis=-1)


def _terrain_click_script(map_name: str, terrain_query_data: dict) -> str:
    payload = json.dumps(terrain_query_data, separators=(",", ":"))
    return f"""
(function() {{
  const mapName = "{map_name}";
  const terrain = {payload};
  const bounds = terrain.bounds;
  const west = bounds[0], south = bounds[1], east = bounds[2], north = bounds[3];
  const width = terrain.width, height = terrain.height;
  const elevation = terrain.elevation;
  const slope = terrain.slope;

  function sampleGrid(grid, row, col) {{
    const value = grid[row][col];
    return value === null ? null : value;
  }}

  function attachTerrainClick() {{
    const map = window[mapName];
    if (!map) {{
      window.setTimeout(attachTerrainClick, 50);
      return;
    }}

    map.on("click", function(e) {{
      const lat = e.latlng.lat;
      const lon = e.latlng.lng;
      if (lon < west || lon > east || lat < south || lat > north) {{
        L.popup()
          .setLatLng(e.latlng)
          .setContent("Terrain data unavailable outside the fetched AOI.")
          .openOn(map);
        return;
      }}

      const col = Math.max(0, Math.min(width - 1, Math.floor(((lon - west) / (east - west)) * width)));
      const row = Math.max(0, Math.min(height - 1, Math.floor(((north - lat) / (north - south)) * height)));
      const elev = sampleGrid(elevation, row, col);
      const slp = sampleGrid(slope, row, col);
      const elevText = elev === null || Number.isNaN(elev) ? "n/a" : elev.toFixed(1) + " m";
      const slopeText = slp === null || Number.isNaN(slp) ? "n/a" : slp.toFixed(1) + " deg";

      L.popup()
        .setLatLng(e.latlng)
        .setContent(
          "<strong>Terrain</strong><br>" +
          "Lat: " + lat.toFixed(5) + "<br>" +
          "Lon: " + lon.toFixed(5) + "<br>" +
          "Elevation: " + elevText + "<br>" +
          "Slope: " + slopeText
        )
        .openOn(map);
    }});
  }}

  attachTerrainClick();
}})();
""".strip()


def _format_river_properties(water_lines):
    enriched = water_lines.copy()
    formatters = {
        "observed_width_m": _format_width,
        "discharge_m3s": _format_discharge,
        "daily_flow_volume_m3": _format_daily_volume,
        "quantity_score": _format_score,
        "river_length_m": _format_generic,
    }
    for column, formatter in formatters.items():
        if column in enriched.columns:
            enriched[column] = enriched[column].map(formatter)
    return enriched


def _river_popup_fields(water_lines) -> tuple[list[str], list[str]]:
    score_alias = _score_alias(water_lines)
    has_discharge = _has_real_values(water_lines, "discharge_m3s")
    has_daily_volume = _has_real_values(water_lines, "daily_flow_volume_m3")
    candidates = [
        ("observed_width_m", "Width (m)"),
        ("quantity_score", score_alias),
    ]
    if has_daily_volume:
        candidates.insert(1, ("daily_flow_volume_m3", "Water quantity (m3/day)"))
    if has_discharge:
        insert_at = 1 if not has_daily_volume else 2
        candidates.insert(insert_at, ("discharge_m3s", "Flow rate (m3/s)"))
    fields = []
    aliases = []
    for field, alias in candidates:
        if field not in water_lines.columns:
            continue
        if water_lines[field].notna().any() or field in {"observed_width_m", "quantity_score"}:
            fields.append(field)
            aliases.append(alias)
    return fields, aliases


def _score_alias(water_lines) -> str:
    if "score_label" not in water_lines.columns or water_lines["score_label"].dropna().empty:
        return "Score (0-1, relative in this map)"
    labels = water_lines["score_label"].dropna().astype(str)
    if labels.empty:
        return "Score (0-1, relative in this map)"
    return labels.mode().iloc[0]


def _has_real_values(water_lines, column: str) -> bool:
    if column not in water_lines.columns:
        return False
    series = water_lines[column]
    return bool(series.notna().any())


def _format_width(value):
    return _format_numeric(value, decimals=2)


def _format_daily_volume(value):
    return _format_numeric(value, decimals=2)


def _format_score(value):
    return _format_numeric(value, decimals=2)


def _format_generic(value):
    return _format_numeric(value, decimals=2)


def _format_discharge(value):
    if value is None or not np.isfinite(value):
        return "n/a"
    numeric = float(value)
    if numeric == 0.0:
        return "0"
    if abs(numeric) < 0.01:
        return f"{numeric:.4f}"
    if abs(numeric) < 1.0:
        return f"{numeric:.3f}"
    return f"{numeric:.2f}"


def _format_numeric(value, decimals: int) -> str:
    if value is None or not np.isfinite(value):
        return "n/a"
    return f"{float(value):.{decimals}f}"
