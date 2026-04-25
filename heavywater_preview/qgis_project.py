from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET


def write_qgs_project(
    qgs_path: Path,
    water_gpkg: Path,
    community_gpkg: Path,
    bbox_wgs84: tuple[float, float, float, float],
) -> None:
    project = ET.Element("qgis", version="3.34.0", projectname="Rivers Communities Preview")
    ET.SubElement(project, "title").text = "Rivers Communities Preview"

    layer_tree = ET.SubElement(project, "layer-tree-group", {"name": "", "checked": "Qt::Checked", "expanded": "1"})
    project_layers = ET.SubElement(project, "projectlayers")

    layers = [
        {
            "id": "osm_xyz",
            "name": "OpenStreetMap",
            "type": "raster",
            "source": "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmin=0&zmax=19",
            "style": None,
        },
        {
            "id": "communities",
            "name": "Communities",
            "type": "vector",
            "source": f"{community_gpkg}|layername=communities",
            "style": "community",
        },
        {
            "id": "water_lines",
            "name": "Rivers",
            "type": "vector",
            "source": f"{water_gpkg}|layername=water_lines",
            "style": "water_line",
        },
    ]

    for layer in layers:
        ET.SubElement(layer_tree, "layer-tree-layer", {"id": layer["id"], "name": layer["name"], "checked": "Qt::Checked", "expanded": "1"})
        maplayer = ET.SubElement(project_layers, "maplayer", {"type": layer["type"]})
        ET.SubElement(maplayer, "id").text = layer["id"]
        ET.SubElement(maplayer, "layername").text = layer["name"]
        ET.SubElement(maplayer, "datasource").text = layer["source"]
        ET.SubElement(maplayer, "provider").text = "gdal" if layer["type"] == "raster" else "ogr"
        if layer["type"] == "vector":
            _append_vector_style(maplayer, layer["style"])

    map_canvas = ET.SubElement(project, "mapcanvas")
    extent = ET.SubElement(map_canvas, "extent")
    min_lon, min_lat, max_lon, max_lat = bbox_wgs84
    ET.SubElement(extent, "xmin").text = str(min_lon)
    ET.SubElement(extent, "ymin").text = str(min_lat)
    ET.SubElement(extent, "xmax").text = str(max_lon)
    ET.SubElement(extent, "ymax").text = str(max_lat)
    _append_qgis_crs(ET.SubElement(map_canvas, "destinationsrs"), "EPSG:4326")
    _append_qgis_crs(ET.SubElement(project, "projectCrs"), "EPSG:4326")

    tree = ET.ElementTree(project)
    ET.indent(tree, space="  ")
    tree.write(qgs_path, encoding="UTF-8", xml_declaration=True)


def _append_vector_style(maplayer, style_name: str | None) -> None:
    renderer = ET.SubElement(maplayer, "renderer-v2", {"type": "singleSymbol"})
    symbols = ET.SubElement(renderer, "symbols")

    style_map = {
        "water_line": ("line", "SimpleLine", {"line_color": "43,131,186,255", "line_width": "0.7"}),
        "community": ("fill", "SimpleFill", {"color": "228,87,46,180", "outline_color": "180,46,15,255", "outline_width": "0.6"}),
    }
    symbol_type, symbol_class, props = style_map[style_name]
    symbol = ET.SubElement(symbols, "symbol", {"type": symbol_type, "name": "0", "alpha": "1", "clip_to_extent": "1"})
    layer = ET.SubElement(symbol, "layer", {"class": symbol_class})
    for key, value in props.items():
        ET.SubElement(layer, "Option", {"name": key, "value": value, "type": "QString"})


def _append_qgis_crs(parent, authid: str) -> None:
    spatial_ref = ET.SubElement(parent, "spatialrefsys")
    ET.SubElement(spatial_ref, "authid").text = authid
    ET.SubElement(spatial_ref, "description").text = "WGS 84"
    ET.SubElement(spatial_ref, "projectionacronym").text = "longlat"
    ET.SubElement(spatial_ref, "ellipsoidacronym").text = "EPSG:7030"
    ET.SubElement(spatial_ref, "geographicflag").text = "true"
