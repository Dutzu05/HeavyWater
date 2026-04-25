# HeavyWater Preview

Local Python pipeline for:
- fetching nearby rivers and waterways from the OpenStreetMap Overpass API for a `lat, lon` AOI
- optionally clipping nearby EuHydro water bodies if you already have the local datasets
- extracting community polygons from a Copernicus imperviousness or built-up GeoTIFF
- fetching Copernicus GLO-30 terrain for the AOI through the Copernicus Data Space Sentinel Hub Process API
- generating a Folium preview map and a basic QGIS project

## Project Layout

- `extract_water_preview.py`: thin CLI entry point
- `heavywater_preview/`: package source code
- `data/euhydro/`: optional local location for EuHydro `.gpkg` files if you want to use that source
- `output/`: generated HTML, QGIS, raster, and GeoPackage outputs

## Water Data

By default, HeavyWater fetches water features from the OpenStreetMap Overpass API, so it does not require any local EuHydro files.

If you want to use EuHydro instead, it is resolved in this order:
1. `data/euhydro`
2. `C:\Projects\EuHydro\rivers_final`

Use `--water-source euhydro` only if those datasets exist locally.

Communities are read from a Copernicus imperviousness or built-up GeoTIFF passed with `--communities-raster`.
If no raster is passed, the map is still generated with an empty `Communities` layer.

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

To explicitly use the Overpass API:

```powershell
python .\extract_water_preview.py 46.66 23.69 --water-source overpass
```

To use local EuHydro data instead:

```powershell
python .\extract_water_preview.py 46.66 23.69 --water-source euhydro
```

To use the Copernicus Impervious Built-Up or Imperviousness Density GeoTIFF:

```powershell
python .\extract_water_preview.py 46.66 23.69 --communities-raster C:\path\to\copernicus_impervious_2021.tif
```

For a density raster, increase the threshold to keep only more built-up pixels:

```powershell
python .\extract_water_preview.py 46.66 23.69 --communities-raster C:\path\to\impervious_density_2021.tif --community-threshold 20
```

To include terrain from Copernicus GLO-30, first set OAuth credentials from your Copernicus Data Space Sentinel Hub client:

```powershell
$env:CDSE_CLIENT_ID="your-client-id"
$env:CDSE_CLIENT_SECRET="your-client-secret"
```

Then run:

```powershell
python .\extract_water_preview.py 46.66 23.69 --communities-raster C:\path\to\copernicus_impervious_2021.tif --terrain
```

This writes:
- `output\terrain_dem.tif`: fetched DEM
- `output\terrain_hillshade.tif`: terrain hillshade used in the map
- `output\terrain_summary.json`: min/max/mean elevation and slope summary
