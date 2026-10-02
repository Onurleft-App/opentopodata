import fcntl
import subprocess


SUPERVISORCTL = ["supervisorctl", "-c", "/app/docker/supervisord.conf"]

# Held during a restart, so the config watcher and the tile downloader can't
# interleave their supervisorctl commands.
LOCK_PATH = "/tmp/otd-reload.lock"


def run_cmd(cmd, logger, shell=False):
    r = subprocess.run(cmd, shell=shell, capture_output=True)
    is_error = r.returncode != 0
    stdout = r.stdout.decode("utf-8")
    if is_error:
        logger.error(f"Error running command, returncode: {r.returncode}")
        logger.error("cmd:")
        logger.error(" ".join(cmd))
        if r.stdout:
            logger.error("stdout:")
            logger.error(stdout)
        if r.stderr:
            logger.error("stderr:")
            logger.error(r.stderr.decode("utf-8"))
        raise ValueError
    return stdout


def restart_otd(logger, reason):
    """Restart OTD so it re-reads the config and the dataset files.

    Datasets are only scanned on startup, and lookups are cached both in
    memcached and in each uwsgi worker, so all of those need restarting.
    """
    with open(LOCK_PATH, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        logger.info(f"Restarting OTD due to {reason}.")
        run_cmd(SUPERVISORCTL + ["stop", "uwsgi"], logger)
        run_cmd(SUPERVISORCTL + ["restart", "memcached"], logger)
        run_cmd(SUPERVISORCTL + ["start", "uwsgi"], logger)
        run_cmd(SUPERVISORCTL + ["start", "warm_cache"], logger)
        logger.info(f"Restarted OTD due to {reason}.")
