# s1-aux-dl

Small CLI helper for downloading auxiliary data for one Sentinel-1 SAFE scene.

It downloads:

- the matching Sentinel-1 orbit `.EOF` file
- the intersecting Copernicus DEM 30 m `.tif` tiles

The input can be a `.SAFE` directory or a `.SAFE.zip` file. The tool reads the SAFE `preview/map-overlay.kml`, finds the DEM tiles intersecting the scene footprint, then downloads the orbit and DEM files to the requested output directories.

## Setup with uv

Requires Python 3.12 or newer.

```bash
uv sync
```

Run the CLI without activating the virtual environment:

```bash
uv run python download_s1_aux.py --help
```

If dependencies or `uv.lock` change, sync again:

```bash
uv sync
```

## Usage

Basic run:

```bash
uv run python download_s1_aux.py /path/to/S1_SCENE.SAFE \
  --orbit-dir ./orbits \
  --dem-dir ./dem
```

The same works with zipped SAFE products:

```bash
uv run python download_s1_aux.py /path/to/S1_SCENE.SAFE.zip \
  --orbit-dir ./orbits \
  --dem-dir ./dem
```

Useful options:

- `--orbit-dir`, `--orbit-output`: directory for downloaded orbit `.EOF` files. Defaults to the current directory.
- `--dem-dir`, `--dem-output`: directory for downloaded Copernicus DEM `.tif` files. Defaults to the current directory.
- `--orbit-type precise|restituted`: orbit type to request. Default is `precise`; precise orbit lookup can fall back to restituted orbits.
- `--no-force-asf`: try Copernicus Data Space first for orbit files instead of going directly to ASF.
- `--force-dem`: redownload DEM tiles even when non-empty files already exist.

## Data Sources

- DEM tiles: Copernicus DEM 30 m public S3 bucket, `https://copernicus-dem-30m.s3.amazonaws.com`. Tile files are downloaded as `https://copernicus-dem-30m.s3.amazonaws.com/<tile>/<tile>.tif`.
- Orbit files by default: ASF public Sentinel-1 orbit S3 bucket used by `sentineleof`, `https://s1-orbits.s3.amazonaws.com`. Precise orbits use the `AUX_POEORB` prefix; restituted orbits use `AUX_RESORB`.
- Orbit files with `--no-force-asf`: Copernicus Data Space Ecosystem. `sentineleof` queries `https://catalogue.dataspace.copernicus.eu/odata/v1/Products` and downloads from `https://zipper.dataspace.copernicus.eu/odata/v1/Products`. Authentication uses `https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token`.
