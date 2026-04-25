# HeavyWater Preview

Local Python pipeline for:
- clipping nearby EuHydro water bodies for a `lat, lon` AOI
- extracting community polygons from a Copernicus imperviousness or built-up GeoTIFF
- generating a Folium preview map and a basic QGIS project

## Project Layout

- `extract_water_preview.py`: thin CLI entry point
- `heavywater_preview/`: package source code
- `data/euhydro/`: optional local location for EuHydro `.gpkg` files
- `output/`: generated HTML, QGIS, raster, and GeoPackage outputs

## Data Resolution

EuHydro is resolved in this order:
1. `data/euhydro`
2. `C:\Projects\EuHydro\rivers_final`

Communities are read from a Copernicus imperviousness or built-up GeoTIFF passed with `--communities-raster`.
If no raster is passed, the map is still generated with an empty `Communities` layer.

If you want this project to be fully self-contained, copy your EuHydro `.gpkg` files into `data/euhydro`.

## Install

```powershell
python -m pip install -r requirements.txt
```

## Run

Generate a rivers-only preview while community data is not available:

```powershell
python .\extract_water_preview.py 46.66 23.69
```

This writes outputs into `output\` and prints the path to `output\map_preview.html`.

To use the Copernicus Impervious Built-Up or Imperviousness Density GeoTIFF:

```powershell
python .\extract_water_preview.py 46.66 23.69 --communities-raster C:\path\to\copernicus_impervious_2021.tif
```

For a density raster, increase the threshold to keep only more built-up pixels:

```powershell
python .\extract_water_preview.py 46.66 23.69 --communities-raster C:\path\to\impervious_density_2021.tif --community-threshold 20
```
