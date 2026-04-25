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
        fields = []
        aliases = []
        if "name" in water_lines.columns:
            fields.append("name")
            aliases.append("Name")
        if "source_layer" in water_lines.columns:
            fields.append("source_layer")
            aliases.append("Type")

        tooltip = None
        if fields:
            tooltip = folium.GeoJsonTooltip(fields=fields, aliases=aliases, sticky=True)

        folium.GeoJson(
            data=json.loads(water_lines.to_crs("EPSG:4326").to_json(default=str)),
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
