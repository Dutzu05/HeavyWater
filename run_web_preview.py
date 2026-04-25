from __future__ import annotations

import json
import mimetypes
import os
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from heavywater_preview.cli import _load_dotenv
from heavywater_preview.config import (
    DEFAULT_BBOX_SIZE_KM,
    DEFAULT_COMMUNITY_THRESHOLD,
    DEFAULT_MIN_COMMUNITY_AREA_M2,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_TERRAIN_RESOLUTION_M,
    DEFAULT_WATER_SOURCE,
    INDEX_HTML_NAME,
    WATER_SOURCE_EUHYDRO,
    WATER_SOURCE_OVERPASS,
)
from heavywater_preview.pipeline import run_pipeline


PROJECT_ROOT = Path(__file__).resolve().parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"
OUTPUT_DIR = DEFAULT_OUTPUT_DIR
INDEX_PATH = OUTPUT_DIR / INDEX_HTML_NAME
PORT = 8000


class PreviewRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory: str | None = None, **kwargs):
        super().__init__(*args, directory=str(PROJECT_ROOT), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            self._handle_status()
            return
        if parsed.path == "/":
            self.path = "/frontend/index.html"
        elif parsed.path == "/app.js":
            self.path = "/frontend/app.js"
        elif parsed.path == "/styles.css":
            self.path = "/frontend/styles.css"
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/generate":
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint.")
            return

        try:
            payload = self._read_json_body()
            lat = self._require_float(payload, "lat")
            lon = self._require_float(payload, "lon")
            size_km = self._optional_float(payload, "size_km", DEFAULT_BBOX_SIZE_KM)
            community_threshold = self._optional_float(payload, "community_threshold", DEFAULT_COMMUNITY_THRESHOLD)
            min_community_area_m2 = self._optional_float(
                payload,
                "min_community_area_m2",
                DEFAULT_MIN_COMMUNITY_AREA_M2,
            )
            terrain_resolution_m = self._optional_float(
                payload,
                "terrain_resolution_m",
                DEFAULT_TERRAIN_RESOLUTION_M,
            )
            include_terrain = bool(payload.get("terrain", False))
            communities_raster = payload.get("communities_raster") or None
            water_source = payload.get("water_source") or DEFAULT_WATER_SOURCE
            if water_source not in {WATER_SOURCE_EUHYDRO, WATER_SOURCE_OVERPASS}:
                raise ValueError("Unsupported water source.")

            outputs = run_pipeline(
                lat=lat,
                lon=lon,
                size_km=size_km,
                output_dir=OUTPUT_DIR,
                water_source=water_source,
                communities_raster=communities_raster,
                community_threshold=community_threshold,
                min_community_area_m2=min_community_area_m2,
                include_terrain=include_terrain,
                terrain_resolution_m=terrain_resolution_m,
            )
        except Exception as exc:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "ok": False,
                    "error": str(exc),
                },
            )
            return

        self._write_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "lat": lat,
                "lon": lon,
                "size_km": size_km,
                "map_url": self._public_path(outputs.map_html_path),
                "index_url": self._public_path(outputs.index_html_path),
                "output_dir": str(outputs.output_dir),
            },
        )

    def guess_type(self, path: str) -> str:
        if path.endswith(".js"):
            return "application/javascript; charset=utf-8"
        if path.endswith(".css"):
            return "text/css; charset=utf-8"
        if path.endswith(".json"):
            return "application/json; charset=utf-8"
        return mimetypes.guess_type(path)[0] or "application/octet-stream"

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args) -> None:
        super().log_message(format, *args)

    def _handle_status(self) -> None:
        self._write_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "has_preview": INDEX_PATH.exists(),
                "preview_url": "/output/index.html" if INDEX_PATH.exists() else None,
                "defaults": {
                    "size_km": DEFAULT_BBOX_SIZE_KM,
                    "water_source": DEFAULT_WATER_SOURCE,
                    "community_threshold": DEFAULT_COMMUNITY_THRESHOLD,
                    "min_community_area_m2": DEFAULT_MIN_COMMUNITY_AREA_M2,
                    "terrain_resolution_m": DEFAULT_TERRAIN_RESOLUTION_M,
                },
            },
        )

    def _read_json_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        if not raw_body:
            return {}
        return json.loads(raw_body.decode("utf-8"))

    def _require_float(self, payload: dict, key: str) -> float:
        if key not in payload:
            raise ValueError(f"Missing required field: {key}")
        return float(payload[key])

    def _optional_float(self, payload: dict, key: str, default: float) -> float:
        value = payload.get(key)
        return default if value in (None, "") else float(value)

    def _public_path(self, path: Path) -> str:
        return "/" + path.relative_to(PROJECT_ROOT).as_posix()

    def _write_json(self, status: HTTPStatus, payload: dict) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    _load_dotenv()
    FRONTEND_DIR.mkdir(parents=True, exist_ok=True)
    os.chdir(PROJECT_ROOT)

    handler = partial(PreviewRequestHandler, directory=str(PROJECT_ROOT))
    with ThreadingHTTPServer(("127.0.0.1", PORT), handler) as server:
        print(f"http://127.0.0.1:{PORT}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
