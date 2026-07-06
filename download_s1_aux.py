#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import math
import os
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET


COPERNICUS_DEM_BASE_URL = "https://copernicus-dem-30m.s3.amazonaws.com"
CHUNK_SIZE = 1024 * 1024


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download the Sentinel-1 orbit file and intersecting Copernicus 30 m "
            "DEM tiles for one SAFE scene."
        )
    )
    parser.add_argument(
        "scene",
        type=Path,
        help="Path to a Sentinel-1 .SAFE directory or .SAFE.zip file.",
    )
    parser.add_argument(
        "--orbit-dir",
        "--orbit-output",
        dest="orbit_dir",
        type=Path,
        default=Path("."),
        help="Directory for downloaded orbit .EOF files. Defaults to current directory.",
    )
    parser.add_argument(
        "--dem-dir",
        "--dem-output",
        dest="dem_dir",
        type=Path,
        default=Path("."),
        help="Directory for downloaded Copernicus DEM .tif files. Defaults to current directory.",
    )
    parser.add_argument(
        "--orbit-type",
        choices=("precise", "restituted"),
        default="precise",
        help="Orbit type to request from sentineleof. Precise falls back to restituted.",
    )
    parser.add_argument(
        "--no-force-asf",
        dest="force_asf",
        action="store_false",
        default=True,
        help="Try Copernicus Data Space before ASF for orbit files.",
    )
    parser.add_argument(
        "--force-dem",
        action="store_true",
        help="Redownload DEM tiles even if the destination file already exists.",
    )
    return parser.parse_args(argv)


def read_map_overlay_kml(scene: Path) -> str:
    if scene.is_dir():
        kml_path = scene / "preview" / "map-overlay.kml"
        if not kml_path.exists():
            raise FileNotFoundError(f"SAFE map overlay not found: {kml_path}")
        return kml_path.read_text(encoding="utf-8")

    if scene.is_file() and zipfile.is_zipfile(scene):
        with zipfile.ZipFile(scene) as zf:
            matches = [name for name in zf.namelist() if name.endswith("preview/map-overlay.kml")]
            if not matches:
                raise FileNotFoundError(f"SAFE map overlay not found in zip: {scene}")
            with zf.open(matches[0]) as fh:
                return fh.read().decode("utf-8")

    raise ValueError(f"Expected a .SAFE directory or .SAFE.zip file: {scene}")


def parse_kml_coordinates(kml_text: str) -> list[tuple[float, float]]:
    root = ET.fromstring(kml_text)
    for elem in root.iter():
        tag = elem.tag.rsplit("}", 1)[-1]
        if tag != "coordinates" or not elem.text:
            continue
        coords = []
        for item in elem.text.split():
            parts = item.split(",")
            if len(parts) < 2:
                continue
            lon, lat = float(parts[0]), float(parts[1])
            coords.append((lon, lat))
        if len(coords) >= 3:
            return coords
    raise ValueError("No usable KML coordinates found in SAFE map overlay")


def point_in_polygon(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    for i, (x1, y1) in enumerate(polygon):
        x2, y2 = polygon[(i + 1) % len(polygon)]
        if (y1 > y) != (y2 > y):
            x_intersection = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < x_intersection:
                inside = not inside
    return inside


def orientation(
    a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]
) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def point_on_segment(
    a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]
) -> bool:
    eps = 1e-12
    return (
        min(a[0], b[0]) - eps <= c[0] <= max(a[0], b[0]) + eps
        and min(a[1], b[1]) - eps <= c[1] <= max(a[1], b[1]) + eps
        and abs(orientation(a, b, c)) < eps
    )


def segments_intersect(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> bool:
    o1 = orientation(a, b, c)
    o2 = orientation(a, b, d)
    o3 = orientation(c, d, a)
    o4 = orientation(c, d, b)

    if o1 * o2 < 0 and o3 * o4 < 0:
        return True

    return (
        point_on_segment(a, b, c)
        or point_on_segment(a, b, d)
        or point_on_segment(c, d, a)
        or point_on_segment(c, d, b)
    )


def tile_intersects_polygon(
    lon: int, lat: int, polygon: list[tuple[float, float]]
) -> bool:
    tile = [(lon, lat), (lon + 1, lat), (lon + 1, lat + 1), (lon, lat + 1)]

    if any(lon <= x <= lon + 1 and lat <= y <= lat + 1 for x, y in polygon):
        return True

    if any(point_in_polygon(corner, polygon) for corner in tile):
        return True

    for i in range(len(polygon)):
        poly_a = polygon[i]
        poly_b = polygon[(i + 1) % len(polygon)]
        for j in range(len(tile)):
            if segments_intersect(poly_a, poly_b, tile[j], tile[(j + 1) % len(tile)]):
                return True

    return False


def copernicus_tile_id(lat: int, lon: int) -> str:
    lat_prefix = "N" if lat >= 0 else "S"
    lon_prefix = "E" if lon >= 0 else "W"
    return (
        f"Copernicus_DSM_COG_10_"
        f"{lat_prefix}{abs(lat):02d}_00_{lon_prefix}{abs(lon):03d}_00_DEM"
    )


def dem_tiles_for_polygon(polygon: list[tuple[float, float]]) -> list[str]:
    min_lon = math.floor(min(lon for lon, _ in polygon))
    max_lon = math.floor(max(lon for lon, _ in polygon))
    min_lat = math.floor(min(lat for _, lat in polygon))
    max_lat = math.floor(max(lat for _, lat in polygon))

    tiles = []
    for lat in range(min_lat, max_lat + 1):
        for lon in range(min_lon, max_lon + 1):
            if tile_intersects_polygon(lon, lat, polygon):
                tiles.append(copernicus_tile_id(lat, lon))

    return tiles


def dem_tiles_for_scene(scene: Path) -> list[str]:
    return dem_tiles_for_polygon(parse_kml_coordinates(read_map_overlay_kml(scene)))


def download_url(url: str, destination: Path) -> None:
    tmp_destination = destination.with_name(f".{destination.name}.tmp")
    request = urllib.request.Request(url, headers={"User-Agent": "s1-aux-dl/0.1"})

    try:
        with urllib.request.urlopen(request) as response, tmp_destination.open("wb") as fh:
            shutil.copyfileobj(response, fh, length=CHUNK_SIZE)
        os.replace(tmp_destination, destination)
    except Exception:
        tmp_destination.unlink(missing_ok=True)
        raise


def download_dem_tiles(tiles: Iterable[str], dem_dir: Path, force: bool = False) -> list[Path]:
    dem_dir.mkdir(parents=True, exist_ok=True)

    downloaded = []
    for tile in tiles:
        destination = dem_dir / f"{tile}.tif"
        if destination.exists() and destination.stat().st_size > 0 and not force:
            print(f"DEM exists, skipping: {destination}")
            downloaded.append(destination)
            continue

        url = f"{COPERNICUS_DEM_BASE_URL}/{tile}/{tile}.tif"
        print(f"Downloading DEM: {tile}")
        try:
            download_url(url, destination)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Failed to download {tile} from {url}: HTTP {exc.code}") from exc
        downloaded.append(destination)

    return downloaded


def download_orbit(
    scene: Path, orbit_dir: Path, orbit_type: str = "precise", force_asf: bool = True
) -> list[Path]:
    from eof import download, log

    log._set_logger_handler(level=logging.INFO)
    orbit_dir.mkdir(parents=True, exist_ok=True)

    orbit_files = download.main(
        sentinel_file=str(scene),
        save_dir=str(orbit_dir),
        orbit_type=orbit_type,
        force_asf=force_asf,
    )
    return [Path(path) for path in orbit_files or []]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    scene = args.scene.expanduser().resolve()
    orbit_dir = args.orbit_dir.expanduser().resolve()
    dem_dir = args.dem_dir.expanduser().resolve()

    if not scene.exists():
        print(f"Scene does not exist: {scene}", file=sys.stderr)
        return 2

    print(f"Scene: {scene}")
    print(f"Orbit output: {orbit_dir}")
    orbit_files = download_orbit(scene, orbit_dir, args.orbit_type, args.force_asf)

    print(f"DEM output: {dem_dir}")
    dem_tiles = dem_tiles_for_scene(scene)
    print("DEM tiles:")
    for tile in dem_tiles:
        print(f"  {tile}")
    dem_files = download_dem_tiles(dem_tiles, dem_dir, force=args.force_dem)

    print("Downloaded/available orbit files:")
    for path in orbit_files:
        print(f"  {path}")

    print("Downloaded/available DEM files:")
    for path in dem_files:
        print(f"  {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
