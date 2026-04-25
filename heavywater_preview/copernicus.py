from __future__ import annotations

import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np

from heavywater_preview.aoi import reproject_bounds_to_euhydro
from heavywater_preview.config import (
    CDSE_CLIENT_ID_ENV_VARS,
    CDSE_CLIENT_SECRET_ENV_VARS,
    CDSE_SENTINELHUB_PROCESS_URL,
    CDSE_TOKEN_URL,
)


def projected_dimensions(bbox_wgs84: tuple[float, float, float, float], resolution_m: float) -> tuple[int, int]:
    projected = reproject_bounds_to_euhydro(bbox_wgs84)
    width_m = max(projected.bounds[2] - projected.bounds[0], resolution_m)
    height_m = max(projected.bounds[3] - projected.bounds[1], resolution_m)
    width = max(1, int(np.ceil(width_m / resolution_m)))
    height = max(1, int(np.ceil(height_m / resolution_m)))
    return width, height


def fetch_cdse_access_token() -> str:
    client_id = first_env_value(CDSE_CLIENT_ID_ENV_VARS)
    client_secret = first_env_value(CDSE_CLIENT_SECRET_ENV_VARS)
    if not client_id or not client_secret:
        raise RuntimeError(
            "Copernicus Data Space access requires OAuth credentials. "
            f"Set one of {CDSE_CLIENT_ID_ENV_VARS} and one of {CDSE_CLIENT_SECRET_ENV_VARS}."
        )

    body = urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode("utf-8")
    request = Request(
        CDSE_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=60) as response:
        token_payload = json.loads(response.read().decode("utf-8"))
    return token_payload["access_token"]


def post_cdse_process_request(payload: dict, token: str) -> bytes:
    request = Request(
        CDSE_SENTINELHUB_PROCESS_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urlopen(request, timeout=180) as response:
        return response.read()


def first_env_value(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None
