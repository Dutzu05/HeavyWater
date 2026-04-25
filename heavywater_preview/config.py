from __future__ import annotations

from datetime import date
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOCAL_EUHYDRO_DATA_DIR = DATA_DIR / "euhydro"
LOCAL_SAR_DATA_DIR = DATA_DIR / "sar"
LOCAL_DEMO_SAR_PATH = LOCAL_SAR_DATA_DIR / "demo_sar_vv.tif"

EXTERNAL_EUHYDRO_DATA_DIR = Path(r"C:\Projects\EuHydro\rivers_final")
EUHYDRO_DATA_DIR = LOCAL_EUHYDRO_DATA_DIR if any(LOCAL_EUHYDRO_DATA_DIR.glob("*.gpkg")) else EXTERNAL_EUHYDRO_DATA_DIR
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"
WGS84_CRS = "EPSG:4326"
EUHYDRO_CRS = "EPSG:3035"

WATER_LINES_LAYER = "water_lines"
WATER_POLYGONS_LAYER = "water_polygons"
RIVER_BASINS_LAYER = "river_basins"
COMMUNITIES_LAYER = "communities"
IMPACT_ZONE_LAYER = "impact_zone"

WATER_GPKG_NAME = "clipped_water.gpkg"
COMMUNITY_GPKG_NAME = "clipped_communities.gpkg"
SAR_CLIPPED_NAME = "sar_vv_clipped.tif"
SAR_FILTERED_DB_NAME = "sar_vv_filtered_db.tif"
QGS_NAME = "water_preview.qgs"
MAP_HTML_NAME = "map_preview.html"
INDEX_HTML_NAME = "index.html"

LINE_LAYERS = ("River_Net_l", "Canals_l", "Ditches_l")
POLYGON_LAYERS = ("InlandWater", "River_Net_p", "Canals_p", "Ditches_p", "Coastal_p")
BASIN_LAYERS = ("RiverBasins",)

PLANETARY_COMPUTER_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
SENTINEL1_GRD_COLLECTION = "sentinel-1-grd"
SAR_DEFAULT_POLARIZATION = "vv"

DEFAULT_BBOX_SIZE_KM = 20.0
DEFAULT_URBAN_THRESHOLD_DB = -10.0
DEFAULT_BUFFER_METERS = 500.0
DEFAULT_MIN_CLUSTER_AREA_M2 = 20_000.0
DEFAULT_COMMUNITY_THRESHOLD = 1.0
DEFAULT_MIN_COMMUNITY_AREA_M2 = 2_000.0


def default_date_range() -> str:
    today = date.today()
    return f"{today.replace(year=today.year - 1).isoformat()}/{today.isoformat()}"
