from __future__ import annotations

import json
from pathlib import Path

import folium
from folium.plugins import MeasureControl, MousePosition
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
    fmap = folium.Map(location=[lat, lon], zoom_start=12, tiles=None)

    # Basemaps
    folium.TileLayer("OpenStreetMap", name="Street Map").add_to(fmap)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri",
        name="Satellite (Esri)",
        overlay=False,
        control=True
    ).add_to(fmap)

    if terrain_dem_raster is not None and terrain_hillshade_raster is not None:
        terrain_group = folium.FeatureGroup(name="Terrain Overlay", show=True)
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
            tooltip=folium.GeoJsonTooltip(
                fields=["area_m2"],
                aliases=["Area (m²)"],
                localize=True,
                sticky=True
            ),
        ).add_to(community_group)
    community_group.add_to(fmap)

    rivers_group = folium.FeatureGroup(name="Rivers & Waterways", show=True)
    if not water_lines.empty:
        # Determine available fields for tooltip
        river_popup_fields, river_popup_aliases = _river_popup_fields(water_lines)
        
        tooltip = None
        if river_popup_fields:
            tooltip = folium.GeoJsonTooltip(fields=river_popup_fields, aliases=river_popup_aliases, sticky=True)

        folium.GeoJson(
            data=json.loads(_format_river_properties(water_lines).to_crs("EPSG:4326").to_json(default=str)),
            style_function=lambda _: {"color": "#0057ff", "weight": 4, "opacity": 0.95},
            tooltip=tooltip
        ).add_to(rivers_group)
    rivers_group.add_to(fmap)

    fmap.fit_bounds([[bbox_wgs84[1], bbox_wgs84[0]], [bbox_wgs84[3], bbox_wgs84[2]]])
    
    # UI Controls
    folium.LayerControl(collapsed=False).add_to(fmap)
    folium.ScaleControl(position="bottomleft").add_to(fmap)
    MeasureControl(position="topleft", primary_length_unit="kilometers", secondary_length_unit="meters").add_to(fmap)
    MousePosition(position="bottomright", separator=" | ", prefix="Coords: ").add_to(fmap)

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
  const soilApiBase = "https://rest.isric.org/soilgrids/v2.0/properties/query";
  const bounds = terrain.bounds;
  const west = bounds[0], south = bounds[1], east = bounds[2], north = bounds[3];
  const width = terrain.width, height = terrain.height;
  const elevation = terrain.elevation;
  const slope = terrain.slope;

  function sampleGrid(grid, row, col) {{
    const value = grid[row][col];
    return value === null ? null : value;
  }}

  function formatTerrainPopup(lat, lon, elevText, slopeText, geotechHtml) {{
    return (
      "<strong>Terrain</strong><br>" +
      "Lat: " + lat.toFixed(5) + "<br>" +
      "Lon: " + lon.toFixed(5) + "<br>" +
      "Elevation: " + elevText + "<br>" +
      "Slope: " + slopeText + "<br><br>" +
      geotechHtml
    );
  }}

  function soilQueryUrl(lat, lon) {{
    const params = new URLSearchParams();
    params.set("lon", lon.toFixed(6));
    params.set("lat", lat.toFixed(6));
    params.append("property", "clay");
    params.append("property", "sand");
    params.append("property", "silt");
    params.append("property", "soc");
    params.append("depth", "60-100cm");
    params.append("value", "mean");
    return soilApiBase + "?" + params.toString();
  }}

  function findLayerValue(payload, propertyName) {{
    const layers = payload && payload.properties && Array.isArray(payload.properties.layers) ? payload.properties.layers : [];
    for (const layer of layers) {{
      if (!layer || layer.name !== propertyName || !Array.isArray(layer.depths)) {{
        continue;
      }}
      for (const depth of layer.depths) {{
        if (!depth) {{
          continue;
        }}
        const label = depth.label || depth.range || "";
        if (String(label) !== "60-100cm") {{
          continue;
        }}
        const values = depth.values || {{}};
        if (typeof values.mean === "number") {{
          return values.mean;
        }}
      }}
    }}
    return null;
  }}

  function toPercentFromSoilGrids(value) {{
    return value === null || Number.isNaN(value) ? null : value / 10.0;
  }}

  function socToOrganicMatterPercent(socValue) {{
    if (socValue === null || Number.isNaN(socValue)) {{
      return 0.0;
    }}
    const socGPerKg = socValue / 10.0;
    return socGPerKg * 0.1724;
  }}

  function estimateKsatMmPerHour(sandPct, clayPct, organicMatterPct) {{
    if ([sandPct, clayPct].some((value) => value === null || Number.isNaN(value))) {{
      return null;
    }}
    const sand = sandPct / 100.0;
    const clay = clayPct / 100.0;
    const om = (organicMatterPct || 0.0) / 100.0;

    const theta1500t = -0.024 * sand + 0.487 * clay + 0.006 * om + 0.005 * sand * clay + 0.013 * clay * om + 0.068;
    const theta33t = -0.251 * sand + 0.195 * clay + 0.011 * om + 0.006 * sand * clay + 0.027 * clay * om + 0.452;
    const thetaS33t = 0.278 * sand + 0.034 * clay + 0.022 * om - 0.018 * sand * clay - 0.027 * clay * om - 0.584 * sand * om + 0.078;

    const theta1500 = theta1500t + (0.14 * theta1500t - 0.02);
    const theta33 = theta33t + (1.283 * theta33t * theta33t - 0.374 * theta33t - 0.015);
    const thetaS33 = thetaS33t + (0.636 * thetaS33t - 0.107);
    const thetaS = theta33 + thetaS33 - 0.097 * sand + 0.043;

    const safeTheta1500 = Math.max(theta1500, 0.001);
    const safeTheta33 = Math.max(theta33, safeTheta1500 + 0.001);
    const safeThetaS = Math.max(thetaS, safeTheta33 + 0.001);
    const lambda = (Math.log(safeTheta33) - Math.log(safeTheta1500)) / (Math.log(1500.0) - Math.log(33.0));
    const ksat = 1930.0 * Math.pow(Math.max(safeThetaS - safeTheta33, 0.0001), Math.max(3.0 - lambda, 0.1));

    if (!Number.isFinite(ksat)) {{
      return null;
    }}
    return ksat;
  }}

  function classifyPermeability(ksatMmPerHour) {{
    if (ksatMmPerHour === null || Number.isNaN(ksatMmPerHour)) {{
      return {{
        rating: "Unavailable",
        recommendation: "Soil permeability estimate unavailable for this point.",
      }};
    }}
    if (ksatMmPerHour < 5.0) {{
      return {{
        rating: "Low Seepage",
        recommendation: "Natural clay basin - high feasibility. No liner required.",
      }};
    }}
    if (ksatMmPerHour <= 20.0) {{
      return {{
        rating: "Medium Seepage",
        recommendation: "Moderate permeability - soil compaction recommended.",
      }};
    }}
    return {{
      rating: "High Seepage",
      recommendation: "High risk - sandy soil detected. HDPE geomembrane liner mandatory.",
    }};
  }}

  function formatGeotechHtml(soil) {{
    if (soil.error) {{
      return "<strong>Geotechnical Feasibility</strong><br>" + soil.error;
    }}

    const permeability = classifyPermeability(soil.ksatMmPerHour);
    return (
      "<strong>Geotechnical Feasibility</strong><br>" +
      "Clay (60-100 cm): " + soil.clayPct.toFixed(1) + "%<br>" +
      "Sand (60-100 cm): " + soil.sandPct.toFixed(1) + "%<br>" +
      "Silt (60-100 cm): " + soil.siltPct.toFixed(1) + "%<br>" +
      "Estimated Ksat: " + (soil.ksatMmPerHour === null ? "n/a" : soil.ksatMmPerHour.toFixed(2) + " mm/h") + "<br>" +
      "Permeability: " + permeability.rating + "<br>" +
      "Engineering note: " + permeability.recommendation
    );
  }}

  async function fetchSoilData(lat, lon) {{
    const response = await fetch(soilQueryUrl(lat, lon));
    if (!response.ok) {{
      throw new Error("SoilGrids request failed (" + response.status + ").");
    }}
    const payload = await response.json();
    const clayValue = findLayerValue(payload, "clay");
    const sandValue = findLayerValue(payload, "sand");
    const siltValue = findLayerValue(payload, "silt");
    const socValue = findLayerValue(payload, "soc");

    const clayPct = toPercentFromSoilGrids(clayValue);
    const sandPct = toPercentFromSoilGrids(sandValue);
    const siltPct = toPercentFromSoilGrids(siltValue);
    if ([clayPct, sandPct, siltPct].some((value) => value === null || Number.isNaN(value))) {{
      throw new Error("SoilGrids returned incomplete 60-100 cm texture values.");
    }}

    return {{
      clayPct: clayPct,
      sandPct: sandPct,
      siltPct: siltPct,
      organicMatterPct: socToOrganicMatterPercent(socValue),
      ksatMmPerHour: estimateKsatMmPerHour(sandPct, clayPct, socToOrganicMatterPercent(socValue)),
    }};
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
      const popup = L.popup()
        .setLatLng(e.latlng)
        .setContent(formatTerrainPopup(lat, lon, elevText, slopeText, "<strong>Geotechnical Feasibility</strong><br>Loading SoilGrids..."))
        .openOn(map);

      fetchSoilData(lat, lon)
        .then(function(soil) {{
          popup.setContent(formatTerrainPopup(lat, lon, elevText, slopeText, formatGeotechHtml(soil)));
        }})
        .catch(function(error) {{
          popup.setContent(
            formatTerrainPopup(
              lat,
              lon,
              elevText,
              slopeText,
              formatGeotechHtml({{ error: "Soil data unavailable: " + error.message }})
            )
          );
        }});
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
    
    # Add basic attributes if available
    if "name" in water_lines.columns and "name" not in fields:
        fields.insert(0, "name")
        aliases.insert(0, "Name")
    if "source_layer" in water_lines.columns and "source_layer" not in fields:
        fields.append("source_layer")
        aliases.append("Type")
        
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
