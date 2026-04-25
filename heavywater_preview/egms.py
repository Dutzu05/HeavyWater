from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen
import io
import zipfile

import geopandas as gpd
import pandas as pd

from heavywater_preview.config import (
    DEFAULT_EGMS_RELEASE,
    EGMS_COMBINED_NAME,
    EGMS_HORIZONTAL_NAME,
    EGMS_RELEASE_ENV_VARS,
    EGMS_TOKEN_ENV_VARS,
    EGMS_VERTICAL_NAME,
    EUHYDRO_CRS,
    LOCAL_EGMS_DATA_DIR,
)
from heavywater_preview.copernicus import first_env_value
from heavywater_preview.stability import load_egms_ortho_vertical_points
from heavywater_preview.aoi import reproject_bounds_to_euhydro


@dataclass
class EgmsFetchResult:
    vertical_path: Path
    horizontal_path: Path | None
    combined_path: Path | None
    output_dir: Path


def ensure_egms_components_for_bbox(
    *,
    bbox_wgs84: tuple[float, float, float, float],
    output_dir: Path | None = None,
    release: str | None = None,
) -> EgmsFetchResult:
    target_dir = Path(output_dir) if output_dir is not None else LOCAL_EGMS_DATA_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    vertical_path = target_dir / EGMS_VERTICAL_NAME
    horizontal_path = target_dir / EGMS_HORIZONTAL_NAME
    combined_path = target_dir / EGMS_COMBINED_NAME
    if vertical_path.exists():
        return EgmsFetchResult(
            vertical_path=vertical_path,
            horizontal_path=horizontal_path if horizontal_path.exists() else None,
            combined_path=combined_path if combined_path.exists() else None,
            output_dir=target_dir,
        )

    release_value = release or first_env_value(EGMS_RELEASE_ENV_VARS) or DEFAULT_EGMS_RELEASE
    raw_dir = target_dir / "toolkit_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    tile_pairs = _required_l3_tiles(bbox_wgs84)
    release_suffix = _release_suffix(release_value)
    token = first_env_value(EGMS_TOKEN_ENV_VARS)

    vertical_csvs = _download_component_tiles(
        tile_pairs=tile_pairs,
        component_code="U",
        release_suffix=release_suffix,
        token=token,
        raw_dir=raw_dir,
    )
    horizontal_csvs = _download_component_tiles(
        tile_pairs=tile_pairs,
        component_code="E",
        release_suffix=release_suffix,
        token=token,
        raw_dir=raw_dir,
    )
    if not vertical_csvs:
        raise RuntimeError(
            "No EGMS L3UD CSV files were found for the requested AOI. "
            "Place the downloaded EGMS zip files in Downloads or set EGMS_TOKEN for portal fallback."
        )

    vertical = _merge_component_csvs(vertical_csvs, vertical_path, component_name="vertical", bbox_wgs84=bbox_wgs84)
    horizontal = _merge_component_csvs(horizontal_csvs, horizontal_path, component_name="horizontal", bbox_wgs84=bbox_wgs84) if horizontal_csvs else None
    combined = _combine_components(vertical, horizontal, combined_path) if horizontal is not None else None
    return EgmsFetchResult(
        vertical_path=vertical_path,
        horizontal_path=horizontal_path if horizontal is not None else None,
        combined_path=combined_path if combined is not None else None,
        output_dir=target_dir,
    )


def _required_l3_tiles(bbox_wgs84: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    projected = reproject_bounds_to_euhydro(bbox_wgs84)
    minx, miny, maxx, maxy = projected.bounds
    eastings = range(int(minx // 100000), int(maxx // 100000) + 1)
    northings = range(int(miny // 100000), int(maxy // 100000) + 1)
    return [(easting, northing) for easting in eastings for northing in northings]


def _release_suffix(release_value: str) -> str:
    mapping = {
        "2015_2021": "",
        "2018_2022": "_2018_2022_1",
        "2019_2023": "_2019_2023_1",
    }
    if release_value not in mapping:
        raise RuntimeError(f"Unsupported EGMS release: {release_value}")
    return mapping[release_value]


def _download_component_tiles(
    *,
    tile_pairs: list[tuple[int, int]],
    component_code: str,
    release_suffix: str,
    token: str,
    raw_dir: Path,
) -> list[Path]:
    csv_paths: list[Path] = []
    component_dir = raw_dir / ("L3UD" if component_code == "U" else "L3EW")
    component_dir.mkdir(parents=True, exist_ok=True)
    for easting, northing in tile_pairs:
        zip_name = f"EGMS_L3_E{easting:02d}N{northing:02d}_100km_{component_code}{release_suffix}.zip"
        csv_path = component_dir / zip_name.replace(".zip", "") / zip_name.replace(".zip", ".csv")
        if csv_path.exists():
            csv_paths.append(csv_path)
            continue
        local_zip = _find_local_egms_zip(zip_name)
        if local_zip is not None:
            _extract_archive_csv(local_zip, component_dir / zip_name.replace(".zip", ""))
        elif token:
            url = f"https://egms.land.copernicus.eu/insar-api/archive/download/{zip_name}?id={token}"
            try:
                payload = _download_bytes(url)
            except Exception:
                continue
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                archive.extractall(component_dir / zip_name.replace(".zip", ""))
        else:
            continue
        if csv_path.exists():
            csv_paths.append(csv_path)
        else:
            discovered = list((component_dir / zip_name.replace(".zip", "")).rglob("*.csv"))
            if discovered:
                csv_paths.append(discovered[0])
    return csv_paths


def _find_local_egms_zip(zip_name: str) -> Path | None:
    candidates = [
        LOCAL_EGMS_DATA_DIR / zip_name,
        Path.home() / "Downloads" / zip_name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _extract_archive_csv(archive_path: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(output_dir)


def _download_bytes(url: str) -> bytes:
    request = Request(url, headers={"Accept": "application/zip,*/*"})
    with urlopen(request, timeout=180) as response:
        return response.read()


def _merge_component_csvs(
    csv_paths: list[Path],
    target_path: Path,
    *,
    component_name: str,
    bbox_wgs84: tuple[float, float, float, float],
) -> gpd.GeoDataFrame:
    frames = [load_egms_ortho_vertical_points(path) for path in csv_paths if path.exists()]
    if not frames:
        points = gpd.GeoDataFrame(columns=["mean_velocity_mm_per_year", "geometry"], geometry="geometry", crs=EUHYDRO_CRS)
    else:
        points = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), geometry="geometry", crs=EUHYDRO_CRS)
        points["_geometry_wkb"] = points.geometry.to_wkb()
        points = points.drop_duplicates(subset=["_geometry_wkb", "mean_velocity_mm_per_year"]).drop(columns="_geometry_wkb")
        points = points.clip(reproject_bounds_to_euhydro(bbox_wgs84))
    points["component"] = component_name
    points.to_file(target_path, driver="GeoJSON")
    return points


def _combine_components(vertical: gpd.GeoDataFrame, horizontal: gpd.GeoDataFrame, target_path: Path) -> gpd.GeoDataFrame:
    if vertical.empty and horizontal.empty:
        combined = gpd.GeoDataFrame(columns=["vertical_velocity_mm_per_year", "horizontal_velocity_mm_per_year", "geometry"], geometry="geometry", crs=EUHYDRO_CRS)
        combined.to_file(target_path, driver="GeoJSON")
        return combined

    base = vertical.copy()
    base["geometry_wkb"] = base.geometry.to_wkb()
    base = base.rename(columns={"mean_velocity_mm_per_year": "vertical_velocity_mm_per_year"})

    ew = horizontal.copy()
    ew["geometry_wkb"] = ew.geometry.to_wkb()
    ew = ew.rename(columns={"mean_velocity_mm_per_year": "horizontal_velocity_mm_per_year"})

    merged = pd.merge(
        base[["geometry_wkb", "vertical_velocity_mm_per_year", "geometry"]],
        ew[["geometry_wkb", "horizontal_velocity_mm_per_year"]],
        on="geometry_wkb",
        how="outer",
    )
    combined = gpd.GeoDataFrame(merged.drop(columns="geometry_wkb"), geometry="geometry", crs=EUHYDRO_CRS)
    combined.to_file(target_path, driver="GeoJSON")
    return combined


def _find_value_column(gdf: gpd.GeoDataFrame) -> str | None:
    for candidate in ("mean_velocity", "velocity", "vel", "avg_velocity"):
        for column in gdf.columns:
            if candidate in str(column).lower():
                return str(column)
    return None
