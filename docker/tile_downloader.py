"""Download USGS tiles queued by the /tiles/ensure endpoint.

Runs as a single supervisord program, so each tile is only downloaded once at a
time. Polls the spool directory for request files, downloads each tile into the
TILES_DATASET folder, then restarts OTD once per batch so the new tiles are
picked up.
"""

from glob import glob
import logging
import os
from pathlib import Path
import sys
import time


APP_DIR = Path(__file__).resolve().parent.parent
sys.path.append(APP_DIR.as_posix())
from opentopodata import config, tiles
from otd_reload import restart_otd


POLL_INTERVAL_S = 5

# Wait before retrying a failed download. A tile is given up on once these
# run out. It can be queued again by a later request.
RETRY_DELAYS_S = [30, 120]

# Restart OTD once the queue is empty, or once a finished tile has waited this
# long for one, so a long batch doesn't hold back the tiles already done.
RELOAD_MAX_DELAY_S = 120

# Number of recent downloads and failures to keep for /tiles/status.
HISTORY_LENGTH = 20


# Logger setup.
logger = logging.getLogger("tiledownloader")
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(message)s"
formatter = logging.Formatter(LOG_FORMAT)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
handler.setFormatter(formatter)
logger.addHandler(handler)


def _find_dataset_folder():
    return tiles.dataset_path(config.load_config(), tiles.dataset_name())


def _restart_otd():
    restart_otd(logger, "new elevation tiles")


class Downloader:
    def __init__(
        self,
        spool,
        find_folder=_find_dataset_folder,
        restart_otd=_restart_otd,
        clock=time.time,
    ):
        self.spool = spool
        self.find_folder = find_folder
        self.restart_otd = restart_otd
        self.clock = clock

        # Tiles that failed and are waiting to retry: {tile: (n_failures, retry_at)}.
        self.retries = {}

        # When the oldest tile not yet visible to OTD finished downloading.
        self.unloaded_since = None

        state = tiles.read_state(spool)
        self.no_data = set(state.get("no_data", []))
        self.recent_downloads = state.get("recent_downloads", [])
        self.recent_failures = state.get("recent_failures", [])
        self.current = None

    def save_state(self):
        retrying = {
            tile: {"failures": n, "retry_at": _timestamp(retry_at)}
            for tile, (n, retry_at) in self.retries.items()
        }
        state = {
            "current": self.current,
            "retrying": retrying,
            "recent_downloads": self.recent_downloads[-HISTORY_LENGTH:],
            "recent_failures": self.recent_failures[-HISTORY_LENGTH:],
            "no_data": sorted(self.no_data),
            "updated_at": _timestamp(self.clock()),
        }
        tiles.write_state(self.spool, state)

    def ready_tiles(self):
        now = self.clock()
        queued = tiles.queued_tiles(self.spool)
        return [t for t in queued if self.retries.get(t, (0, 0))[1] <= now]

    def process(self, tile, folder):
        """Download one queued tile, and record what happened."""
        if tile in self.no_data or tiles.tile_corner(tile) in tiles.existing_corners(
            folder
        ):
            self.retries.pop(tile, None)
            tiles.remove_request(self.spool, tile)
            return

        self.current = tile
        self.save_state()
        logger.info(f"Downloading {tile} from {tiles.tile_url(tile)}")
        try:
            path = tiles.download_tile(tile, folder)

        except tiles.NoDataError as e:
            logger.info(f"No USGS tile for {tile}: {e}")
            self.no_data.add(tile)
            self.retries.pop(tile, None)
            tiles.remove_request(self.spool, tile)

        except tiles.DownloadError as e:
            n_failures = self.retries.get(tile, (0, 0))[0] + 1
            if n_failures <= len(RETRY_DELAYS_S):
                delay = RETRY_DELAYS_S[n_failures - 1]
                logger.warning(f"Download of {tile} failed, retrying in {delay}s: {e}")
                self.retries[tile] = (n_failures, self.clock() + delay)
            else:
                logger.error(
                    f"Download of {tile} failed {n_failures} times, giving up: {e}"
                )
                self.retries.pop(tile, None)
                self.recent_failures.append(
                    {"tile": tile, "error": str(e), "at": _timestamp(self.clock())}
                )
                tiles.remove_request(self.spool, tile)

        else:
            logger.info(f"Downloaded {tile} to {path}")
            self.retries.pop(tile, None)
            self.recent_downloads.append(
                {"tile": tile, "path": path, "at": _timestamp(self.clock())}
            )
            tiles.remove_request(self.spool, tile)
            if self.unloaded_since is None:
                self.unloaded_since = self.clock()

        finally:
            self.current = None
            self.save_state()

    def maybe_restart_otd(self):
        """Restart OTD after a batch of downloads."""
        if self.unloaded_since is None:
            return
        waited = self.clock() - self.unloaded_since
        if self.ready_tiles() and waited < RELOAD_MAX_DELAY_S:
            return
        self.restart_otd()
        self.unloaded_since = None

    def run_once(self):
        """Download everything that's ready, then restart OTD if needed."""
        ready = self.ready_tiles()
        if ready:
            folder = self.find_folder()
            while ready:
                self.process(ready[0], folder)
                self.maybe_restart_otd()
                ready = self.ready_tiles()
        self.maybe_restart_otd()


def _timestamp(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _remove_stale_part_files(folder):
    """Remove downloads interrupted by a restart. Only this process writes them."""
    pattern = os.path.join(folder, "**", "*" + tiles.PART_EXTENSION)
    for path in glob(pattern, recursive=True):
        logger.info(f"Removing interrupted download {path}")
        os.remove(path)


if __name__ == "__main__":
    os.chdir(APP_DIR)
    spool = tiles.spool_dir()
    os.makedirs(spool, exist_ok=True)

    # uwsgi runs as www-data and needs to add request files.
    os.chmod(spool, 0o1777)

    try:
        folder = _find_dataset_folder()
        _remove_stale_part_files(folder)
        logger.info(f"Downloading requested tiles into {folder}")
    except config.ConfigError as e:
        logger.warning(f"Tile dataset isn't usable yet: {e}")

    downloader = Downloader(spool)
    downloader.save_state()
    while True:
        try:
            downloader.run_once()
        except Exception:
            logger.exception("Error in tile downloader")
        time.sleep(POLL_INTERVAL_S)
