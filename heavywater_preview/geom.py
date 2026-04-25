from __future__ import annotations

import geopandas as gpd


def project_geometry(geometry, source_crs: str, target_crs):
    series = gpd.GeoSeries([geometry], crs=source_crs)
    return series.to_crs(target_crs).iloc[0]
