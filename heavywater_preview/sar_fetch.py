from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path

import rasterio
from rasterio.errors import WindowError
from planetary_computer import sign_inplace
from pystac_client import Client
from rasterio.io import MemoryFile
from rasterio.mask import mask
from rasterio.vrt import WarpedVRT
from shapely.geometry import mapping

from heavywater_preview.config import PLANETARY_COMPUTER_STAC_URL, SAR_DEFAULT_POLARIZATION, SENTINEL1_GRD_COLLECTION
from heavywater_preview.geom import project_geometry


def prepare_sar_raster(
    aoi_wgs84,
    output_path: Path,
    sar_path: str | Path | None = None,
    date_range: str | None = None,
    polarization: str = SAR_DEFAULT_POLARIZATION,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if sar_path is not None:
        return clip_local_raster(Path(sar_path), aoi_wgs84, output_path)
    return fetch_sentinel1_grd(aoi_wgs84, output_path, date_range=date_range, polarization=polarization)


def clip_local_raster(source_path: Path, aoi_wgs84, output_path: Path) -> Path:
    with rasterio.open(source_path) as src:
        try:
            clipped_data, clipped_transform, clipped_crs = _clip_open_dataset(src, aoi_wgs84)
        except (ValueError, WindowError) as exc:
            raise ValueError(
                f"The local SAR raster does not overlap the requested AOI: {source_path}. "
                "Use --fetch-sar to download Sentinel-1 for these coordinates, or pass a different --sar-path."
            ) from exc
        profile = src.profile.copy()

    profile.update(
        driver="GTiff",
        height=clipped_data.shape[1],
        width=clipped_data.shape[2],
        count=clipped_data.shape[0],
        transform=clipped_transform,
        crs=clipped_crs,
        compress="deflate",
    )
    _sanitize_gtiff_profile(profile)
    with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(clipped_data)
    return output_path


def fetch_sentinel1_grd(
    aoi_wgs84,
    output_path: Path,
    date_range: str | None,
    polarization: str = SAR_DEFAULT_POLARIZATION,
) -> Path:
    if not date_range:
        raise ValueError("A date range is required when fetching Sentinel-1 data.")

    catalog = Client.open(PLANETARY_COMPUTER_STAC_URL, modifier=sign_inplace)
    search = catalog.search(
        collections=[SENTINEL1_GRD_COLLECTION],
        intersects=mapping(aoi_wgs84),
        datetime=date_range,
        limit=12,
    )
    items = [item for item in search.items() if polarization.lower() in item.assets]
    if not items:
        raise ValueError("No Sentinel-1 GRD scenes with the requested polarization were found for the AOI/date range.")

    latest_item = sorted(items, key=lambda item: item.properties.get("datetime", ""), reverse=True)[0]
    with ExitStack() as stack:
        src = stack.enter_context(rasterio.open(latest_item.assets[polarization.lower()].href))
        gcps, gcps_crs = src.gcps
        if src.crs is None and gcps and gcps_crs:
            src = stack.enter_context(WarpedVRT(src, src_crs=gcps_crs))
        clipped_data, clipped_transform, clipped_crs = _clip_open_dataset(src, aoi_wgs84)
        profile = src.profile.copy()

    profile.update(
        driver="GTiff",
        height=clipped_data.shape[1],
        width=clipped_data.shape[2],
        count=clipped_data.shape[0],
        transform=clipped_transform,
        crs=clipped_crs,
        compress="deflate",
    )
    _sanitize_gtiff_profile(profile)
    with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(clipped_data)
    return output_path


def _clip_open_dataset(src, aoi_wgs84):
    aoi_in_raster_crs = project_geometry(aoi_wgs84, "EPSG:4326", src.crs)
    clipped_data, clipped_transform = mask(src, [mapping(aoi_in_raster_crs)], crop=True)
    return clipped_data, clipped_transform, src.crs


def _sanitize_gtiff_profile(profile: dict) -> None:
    profile.pop("blockxsize", None)
    profile.pop("blockysize", None)
    profile.pop("tiled", None)
