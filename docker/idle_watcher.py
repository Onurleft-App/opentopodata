"""Put this server to sleep after it's been idle.

In the cloud, OTD runs as an ECS service that trails-service scales to 1 before
a refresh and back to 0 afterwards. This is a backstop for when that doesn't
happen: after IDLE_MINUTES with no real requests and no tile downloads, it sets
the service's desired count to 0 and ECS stops the task.

Disabled unless IDLE_MINUTES, ECS_CLUSTER and ECS_SERVICE are all set.
"""

import logging
import os
from pathlib import Path
import sys
import time


APP_DIR = Path(__file__).resolve().parent.parent
sys.path.append(APP_DIR.as_posix())
from opentopodata import activity, tiles
from otd_reload import is_reloading


DEFAULT_IDLE_MINUTES = 30
DEFAULT_CHECK_SECONDS = 60


# Logger setup.
logger = logging.getLogger("idlewatcher")
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(message)s"
formatter = logging.Formatter(LOG_FORMAT)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
handler.setFormatter(formatter)
logger.addHandler(handler)


def busy_reason(now, last_activity, idle_seconds, queued, downloader_state, reloading):
    """Why the server can't go to sleep yet.

    Args:
        now: Current unix time.
        last_activity: Unix time of the last request, or of startup.
        idle_seconds: How long without requests counts as idle.
        queued: Tile names waiting in the downloader spool.
        downloader_state: Dict from tiles.read_state().
        reloading: Whether OTD is being restarted.

    Returns:
        Reason string, or None if the server is idle.
    """
    if reloading:
        return "OTD is reloading"
    if queued:
        return f"{len(queued)} tile(s) queued for download"
    if downloader_state.get("current"):
        return f"downloading tile {downloader_state['current']}"
    if downloader_state.get("retrying"):
        return f"{len(downloader_state['retrying'])} tile download(s) waiting to retry"
    idle_for = now - last_activity
    if idle_for < idle_seconds:
        return f"last request {idle_for / 60:.1f} minutes ago"
    return None


def disabled_reason(idle_minutes, cluster, service):
    """Why the watcher is off, or None if it's on."""
    if idle_minutes <= 0:
        return "IDLE_MINUTES is 0"
    if not cluster or not service:
        return "ECS_CLUSTER and ECS_SERVICE aren't both set"
    return None


def _ecs_client():
    # Default credential chain: the task role on ECS.
    import boto3

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    return boto3.client("ecs", region_name=region)


class IdleWatcher:
    def __init__(
        self,
        cluster,
        service,
        idle_minutes,
        spool,
        ecs_client=_ecs_client,
        reloading=is_reloading,
        clock=time.time,
        last_request_path=None,
    ):
        self.cluster = cluster
        self.service = service
        self.idle_minutes = idle_minutes
        self.spool = spool
        self.ecs_client = ecs_client
        self.reloading = reloading
        self.clock = clock
        self.last_request_path = last_request_path

        # Startup counts as activity, so a server that's just been woken up
        # isn't put back to sleep before its first request arrives.
        self.started_at = clock()
        self.asleep = False
        self.was_busy = True

    def last_activity(self):
        last_request = activity.last_request_time(self.last_request_path)
        return max(self.started_at, last_request or 0)

    def check(self):
        """Put the server to sleep if it's idle.

        Returns:
            True once the ECS service has been scaled to 0.
        """
        if self.asleep:
            return True

        reason = busy_reason(
            now=self.clock(),
            last_activity=self.last_activity(),
            idle_seconds=self.idle_minutes * 60,
            queued=tiles.queued_tiles(self.spool),
            downloader_state=tiles.read_state(self.spool),
            reloading=self.reloading(),
        )
        if reason:
            if not self.was_busy:
                logger.info(f"Busy again: {reason}.")
            self.was_busy = True
            return False
        self.was_busy = False

        logger.info(
            f"No requests for {self.idle_minutes:g} minutes. Putting server to sleep:"
            f" setting desiredCount=0 on ECS service '{self.service}'"
            f" in cluster '{self.cluster}'."
        )
        try:
            self.ecs_client().update_service(
                cluster=self.cluster, service=self.service, desiredCount=0
            )
        except Exception as e:
            logger.error(f"Unable to put server to sleep, will retry: {e!r}")
            return False

        logger.info("ECS service scaled to 0. ECS will now stop this task.")
        self.asleep = True
        return True


def main(environ=os.environ, sleep=time.sleep, ecs_client=_ecs_client):
    idle_minutes = float(environ.get("IDLE_MINUTES") or DEFAULT_IDLE_MINUTES)
    check_seconds = float(environ.get("IDLE_CHECK_SECONDS") or DEFAULT_CHECK_SECONDS)
    cluster = environ.get("ECS_CLUSTER")
    service = environ.get("ECS_SERVICE")

    reason = disabled_reason(idle_minutes, cluster, service)
    if reason:
        logger.info(f"Idle watcher disabled: {reason}.")
        return

    try:
        activity.touch()
    except OSError as e:
        logger.warning(f"Unable to create last request file: {e}")

    watcher = IdleWatcher(
        cluster, service, idle_minutes, tiles.spool_dir(), ecs_client=ecs_client
    )
    logger.info(
        f"Idle watcher enabled: ECS service '{service}' in cluster '{cluster}'"
        f" will be scaled to 0 after {idle_minutes:g} minutes without requests."
    )
    while True:
        try:
            if watcher.check():
                return
        except Exception:
            logger.exception("Error in idle watcher")
        sleep(check_seconds)


if __name__ == "__main__":
    os.chdir(APP_DIR)
    main()
