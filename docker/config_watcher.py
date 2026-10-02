import logging
import time
from pathlib import Path
import sys

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from otd_reload import restart_otd


# Paths.
CONFIG_DIR = Path("/app/")
CONFIG_PATH = Path("/app/config.yaml")
EXAMPLE_CONFIG_PATH = Path("/app/example-config.yaml")

# Debouncing: once the config has been reloaded, any queued unprocessed events should be discarded.
LAST_INVOCATION_TIME = time.time()


# Logger setup.
logger = logging.getLogger("configwatcher")
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(message)s"
formatter = logging.Formatter(LOG_FORMAT)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
handler.setFormatter(formatter)
logger.addHandler(handler)


def reload_config():
    global LAST_INVOCATION_TIME
    LAST_INVOCATION_TIME = time.time()
    restart_otd(logger, "config change")
    LAST_INVOCATION_TIME = time.time()


class Handler(FileSystemEventHandler):

    def on_any_event(self, event):
        watch_paths_str = [
            EXAMPLE_CONFIG_PATH.as_posix(),
            CONFIG_PATH.as_posix(),
        ]

        # Filter unwanted events.
        if event.event_type not in {"modified", "created"}:
            logger.debug(f"Dropping event with type {event.event_type=}")
            return
        if event.is_directory:
            logger.debug(f"Dropping dir event")
            return
        if event.src_path not in watch_paths_str:
            logger.debug(f"Dropping event with path {event.src_path=}")
            return
        if not Path(event.src_path).exists():
            logger.debug(f"Dropping event for nonexistent path {event.src_path=}")
            return

        # Debouncing.
        mtime = Path(event.src_path).lstat().st_mtime
        if mtime < LAST_INVOCATION_TIME:
            msg = f"Dropping event for file that hasn't been modified since the last run. {event.src_path=}"
            logger.debug(msg)
            return

        logger.debug(f"Dispatching event on {event.src_path=}")
        reload_config()


if __name__ == "__main__":

    event_handler = Handler()
    observer = Observer()
    observer.schedule(event_handler, CONFIG_DIR, recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(1)
    finally:
        observer.stop()
        observer.join()
