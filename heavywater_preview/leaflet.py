from __future__ import annotations

import json
from pathlib import Path

import folium


def write_preview_map(
    html_path: Path,
    index_path: Path,
    lat: float,
    lon: float,
    bbox_wgs84: tuple[float, float, float, float],
    water_lines,
    communities,
) -> None:
    fmap = folium.Map(location=[lat, lon], zoom_start=12, tiles="OpenStreetMap")

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
        folium.GeoJson(
            data=json.loads(water_lines.to_crs("EPSG:4326").to_json(default=str)),
            style_function=lambda _: {"color": "#0057ff", "weight": 4, "opacity": 0.95},
        ).add_to(rivers_group)
    rivers_group.add_to(fmap)

    fmap.fit_bounds([[bbox_wgs84[1], bbox_wgs84[0]], [bbox_wgs84[3], bbox_wgs84[2]]])
    folium.LayerControl(collapsed=False).add_to(fmap)
    fmap.save(html_path)
    index_path.write_text(html_path.read_text(encoding="utf-8"), encoding="utf-8")
