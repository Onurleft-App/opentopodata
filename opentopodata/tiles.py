"""On-demand downloads of USGS 3DEP 1/3 arc-second tiles.

The /tiles/ensure endpoint queues missing tiles by writing one empty file per
tile into a spool directory. docker/tile_downloader.py is a single long-running
process that downloads them, so a tile is never fetched twice at once, and
downloads don't die when uwsgi recycles a worker. The downloader writes its
progress to a state file in the same spool directory, which the endpoint reads.
"""

from glob import glob
import json
import os
import re
import tempfile

import requests

from opentopodata import config


USGS_TILE_REGEX = r"^[ns]\d{2}[ew]\d{3}$"
USGS_URL_TEMPLATE = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current/{tile}/USGS_13_{tile}.tif"
USER_AGENT = "Onurleft opentopodata"
PART_EXTENSION = ".part"
STATE_FILENAME = "state.json"
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
DOWNLOAD_TIMEOUT_S = (10, 60)

DEFAULT_DATASET = "ned10m"
DEFAULT_MAX_PER_REQUEST = 20
DEFAULT_SPOOL_DIR = "/tmp/tile-requests"


class NoDataError(Exception):
    """USGS has no tile at this location (e.g., open ocean)."""


class DownloadError(Exception):
    """Download failed, but might work if retried."""


def dataset_name():
    return os.environ.get("TILES_DATASET") or DEFAULT_DATASET


def token():
    return os.environ.get("TILES_TOKEN") or None


def max_per_request():
    return int(os.environ.get("TILES_MAX_PER_REQUEST") or DEFAULT_MAX_PER_REQUEST)


def spool_dir():
    return os.environ.get("TILES_SPOOL_DIR") or DEFAULT_SPOOL_DIR


def is_valid_tile_name(name):
    return isinstance(name, str) and bool(re.match(USGS_TILE_REGEX, name))


def renamed_filename(tile):
    """Filename Open Topo Data expects for a USGS tile.

    USGS names tiles by their north-west corner, Open Topo Data by their
    south-west corner, so the latitude moves 1 degree south. Matches the rename
    script in docs/datasets/ned.md.

    Args:
        tile: USGS tile name like 'n39w078'.

    Returns:
        Filename like 'USGS_13_n38w078_renamed.tif'.
    """
    northing, easting = tile[:3], tile[3:]
    value = int(northing[1:])
    if northing == "n00":
        new_northing = "s01"
    elif northing[0] == "n":
        new_northing = "n" + str(value - 1).zfill(2)
    else:
        new_northing = "s" + str(value + 1).zfill(2)
    return f"USGS_13_{new_northing}{easting}_renamed.tif"


def tile_url(tile):
    return USGS_URL_TEMPLATE.format(tile=tile)


def tile_corner(tile):
    """South-west corner of a USGS tile, as used by TiledDataset."""
    return config.TiledDataset._filename_to_tile_corner(renamed_filename(tile))


def dataset_path(otd_config, name):
    """Folder of the dataset that tiles are downloaded into.

    Args:
        otd_config: Config dict from config.load_config().
        name: Dataset name.

    Returns:
        Path string.

    Raises:
        ConfigError: If the dataset isn't in the config, or has no folder.
    """
    for d in otd_config["datasets"]:
        if d["name"] == name:
            if "path" not in d:
                raise config.ConfigError(f"Tile dataset '{name}' has no path.")
            return d["path"]
    raise config.ConfigError(f"Tile dataset '{name}' (TILES_DATASET) not in config.")


def existing_corners(path):
    """Tile corners already in a dataset folder.

    Uses the same filename rules as TiledDataset, so a tile counts as present
    however it's named, and a download can never duplicate an existing tile.

    Args:
        path: Dataset folder.

    Returns:
        Set of (northing, easting) Decimal tuples.
    """
    corners = set()
    for p in glob(os.path.join(path, "**", "*"), recursive=True):
        if not os.path.isfile(p) or config.Dataset._is_aux_file(p):
            continue
        if not re.match(config.FILENAME_TILE_REGEX, os.path.basename(p), re.I):
            continue
        corners.add(config.TiledDataset._filename_to_tile_corner(p))
    return corners


def queue_tiles(tiles, spool):
    os.makedirs(spool, exist_ok=True)
    for tile in tiles:
        with open(os.path.join(spool, tile), "a"):
            pass


def queued_tiles(spool):
    try:
        names = os.listdir(spool)
    except FileNotFoundError:
        return []
    return sorted(n for n in names if is_valid_tile_name(n))


def remove_request(spool, tile):
    try:
        os.remove(os.path.join(spool, tile))
    except FileNotFoundError:
        pass


def read_state(spool):
    try:
        with open(os.path.join(spool, STATE_FILENAME)) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def write_state(spool, state):
    """Atomically replace the downloader state file."""
    os.makedirs(spool, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=spool, prefix=".state-")
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=2)
    os.chmod(tmp_path, 0o644)
    os.replace(tmp_path, os.path.join(spool, STATE_FILENAME))


def download_tile(tile, folder):
    """Download a USGS tile into a dataset folder under its renamed filename.

    Streams to a .part file, checks the size against Content-Length, then
    renames into place, so a partial tile is never visible under its final
    name. Open Topo Data ignores .part files.

    Args:
        tile: USGS tile name like 'n39w078'.
        folder: Dataset folder.

    Returns:
        Path of the downloaded tile.

    Raises:
        NoDataError: USGS has no tile there.
        DownloadError: Anything else went wrong.
    """
    final_path = os.path.join(folder, renamed_filename(tile))
    part_path = final_path + PART_EXTENSION
    if not os.access(folder, os.W_OK):
        raise DownloadError(
            f"Dataset folder '{folder}' isn't writable. Is it mounted read-only?"
        )

    try:
        with requests.get(
            tile_url(tile),
            stream=True,
            timeout=DOWNLOAD_TIMEOUT_S,
            headers={"User-Agent": USER_AGENT},
        ) as response:
            if response.status_code in (403, 404):
                raise NoDataError(f"USGS returned HTTP {response.status_code}.")
            if response.status_code != 200:
                raise DownloadError(f"USGS returned HTTP {response.status_code}.")
            if "Content-Length" not in response.headers:
                raise DownloadError("USGS response has no Content-Length.")
            expected_size = int(response.headers["Content-Length"])

            size = 0
            with open(part_path, "wb") as f:
                for chunk in response.iter_content(DOWNLOAD_CHUNK_SIZE):
                    f.write(chunk)
                    size += len(chunk)
                f.flush()
                os.fsync(f.fileno())

        if size != expected_size:
            raise DownloadError(f"Got {size} bytes, expected {expected_size}.")
        os.replace(part_path, final_path)

    except (requests.RequestException, OSError) as e:
        raise DownloadError(str(e)) from e

    finally:
        if os.path.exists(part_path):
            os.remove(part_path)

    return final_path
