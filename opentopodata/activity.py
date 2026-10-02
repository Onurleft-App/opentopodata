"""Time of the last real request, for docker/idle_watcher.py.

Stored as the modification time of a file, so every uwsgi worker can update it
without locking, and it survives worker restarts.
"""

import os


LAST_REQUEST_PATH = "/tmp/otd-last-request"


def touch(path=None):
    """Set the last request time to now."""
    path = path or LAST_REQUEST_PATH
    try:
        os.utime(path)
    except FileNotFoundError:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # Another process created it first.
            os.utime(path)
            return

        # uwsgi runs as www-data and the idle watcher as root, so both need to
        # be able to update the time.
        os.fchmod(fd, 0o666)
        os.close(fd)


def last_request_time(path=None):
    """Unix time of the last request, or None if there hasn't been one."""
    try:
        return os.stat(path or LAST_REQUEST_PATH).st_mtime
    except FileNotFoundError:
        return None
