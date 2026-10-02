import fcntl
import logging
import os
import sys

import pytest
from unittest.mock import MagicMock, patch

from opentopodata import activity, api, tiles

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "docker"))
import idle_watcher
import otd_reload


IDLE_SECONDS = 30 * 60
NOW = 1_000_000.0


@pytest.fixture
def last_request_path(tmp_path):
    path = tmp_path / "otd-last-request"
    with patch("opentopodata.activity.LAST_REQUEST_PATH", path.as_posix()):
        yield path


@pytest.fixture
def spool(tmp_path, monkeypatch):
    spool = tmp_path / "spool"
    monkeypatch.setenv("TILES_SPOOL_DIR", spool.as_posix())
    monkeypatch.delenv("TILES_TOKEN", raising=False)
    return spool


class TestActivity:
    def test_touch_creates_file(self, last_request_path):
        assert activity.last_request_time() is None
        activity.touch()
        assert activity.last_request_time() == pytest.approx(
            last_request_path.stat().st_mtime
        )
        assert last_request_path.stat().st_mode & 0o777 == 0o666

    def test_touch_updates_time(self, last_request_path):
        activity.touch()
        os.utime(last_request_path, (0, 0))
        activity.touch()
        assert activity.last_request_time() > 0

    @pytest.mark.parametrize(
        "url",
        [
            "/v1/test-dataset?locations=0,0",
            "/v1/missing-dataset?locations=0,0",
            "/tiles/status",
        ],
    )
    def test_real_requests_recorded(self, last_request_path, spool, url):
        activity.touch()
        os.utime(last_request_path, (0, 0))
        api.app.test_client().get(url)
        assert activity.last_request_time() > 0

    def test_tiles_ensure_recorded(self, last_request_path, spool):
        activity.touch()
        os.utime(last_request_path, (0, 0))
        api.app.test_client().post("/tiles/ensure", json={})
        assert activity.last_request_time() > 0

    @pytest.mark.parametrize("url", ["/health", "/datasets", "/"])
    def test_other_requests_not_recorded(self, last_request_path, url):
        api.app.test_client().get(url)
        assert activity.last_request_time() is None

    def test_preflight_not_recorded(self, last_request_path):
        api.app.test_client().options("/v1/test-dataset")
        assert activity.last_request_time() is None

    def test_failure_doesnt_fail_request(self, tmp_path):
        bad_path = (tmp_path / "missing-folder" / "otd-last-request").as_posix()
        with patch("opentopodata.activity.LAST_REQUEST_PATH", bad_path):
            response = api.app.test_client().get("/v1/test-dataset?locations=0,0")
        assert response.status_code == 200


class TestBusyReason:
    def reason(self, last_activity=NOW - IDLE_SECONDS, queued=(), state=None, **kw):
        return idle_watcher.busy_reason(
            now=NOW,
            last_activity=last_activity,
            idle_seconds=IDLE_SECONDS,
            queued=list(queued),
            downloader_state=state or {},
            reloading=kw.get("reloading", False),
        )

    def test_idle(self):
        assert self.reason() is None
        assert self.reason(last_activity=0) is None

    def test_idle_with_downloader_history(self):
        state = {"current": None, "retrying": {}, "no_data": ["n30w070"]}
        assert self.reason(state=state) is None

    def test_recent_request(self):
        assert self.reason(last_activity=NOW - IDLE_SECONDS + 1)

    def test_queued_tile(self):
        assert self.reason(queued=["n40w077"])

    def test_downloading(self):
        assert self.reason(state={"current": "n40w077"})

    def test_retrying(self):
        assert self.reason(state={"retrying": {"n40w077": {"failures": 1}}})

    def test_reloading(self):
        assert self.reason(reloading=True)


class TestIsReloading:
    def test_lock(self, tmp_path):
        lock_path = (tmp_path / "otd-reload.lock").as_posix()
        with patch("otd_reload.LOCK_PATH", lock_path):
            assert not otd_reload.is_reloading()
            with open(lock_path, "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                assert otd_reload.is_reloading()
            assert not otd_reload.is_reloading()


class TestDisabledReason:
    def test_enabled(self):
        assert idle_watcher.disabled_reason(30, "cluster", "service") is None

    @pytest.mark.parametrize(
        "idle_minutes, cluster, service",
        [(0, "cluster", "service"), (30, None, "service"), (30, "cluster", "")],
    )
    def test_disabled(self, idle_minutes, cluster, service):
        assert idle_watcher.disabled_reason(idle_minutes, cluster, service)


class TestIdleWatcher:
    @pytest.fixture
    def clock(self):
        class Clock:
            now = NOW

            def __call__(self):
                return self.now

        return Clock()

    @pytest.fixture
    def client(self):
        return MagicMock()

    @pytest.fixture
    def watcher(self, spool, last_request_path, clock, client):
        self.reloading = False
        return idle_watcher.IdleWatcher(
            "otd-cluster",
            "otd-service",
            idle_minutes=30,
            spool=spool.as_posix(),
            ecs_client=lambda: client,
            reloading=lambda: self.reloading,
            clock=clock,
        )

    def test_startup_counts_as_activity(self, watcher, clock, client):
        clock.now += IDLE_SECONDS - 1
        assert not watcher.check()
        clock.now += 1
        assert watcher.check()
        client.update_service.assert_called_once_with(
            cluster="otd-cluster", service="otd-service", desiredCount=0
        )

    def test_recent_request_keeps_awake(self, watcher, clock, client):
        clock.now += IDLE_SECONDS
        with patch("opentopodata.activity.last_request_time", return_value=clock.now):
            assert not watcher.check()
        client.update_service.assert_not_called()

    def test_not_called_again_after_success(self, watcher, clock, client):
        clock.now += IDLE_SECONDS
        assert watcher.check()
        assert watcher.check()
        assert client.update_service.call_count == 1

    def test_failure_logged_and_retried(self, watcher, clock, client, caplog):
        client.update_service.side_effect = [Exception("AccessDenied"), None]
        clock.now += IDLE_SECONDS
        with caplog.at_level(logging.ERROR, logger="idlewatcher"):
            assert not watcher.check()
        assert "AccessDenied" in caplog.text
        assert watcher.check()
        assert client.update_service.call_count == 2

    def test_queued_tile_keeps_awake(self, watcher, spool, clock, client):
        tiles.queue_tiles(["n40w077"], spool.as_posix())
        clock.now += IDLE_SECONDS
        assert not watcher.check()
        client.update_service.assert_not_called()

    def test_downloader_state_keeps_awake(self, watcher, spool, clock, client):
        tiles.write_state(spool.as_posix(), {"current": "n40w077"})
        clock.now += IDLE_SECONDS
        assert not watcher.check()
        tiles.write_state(spool.as_posix(), {"retrying": {"n40w077": {}}})
        assert not watcher.check()
        client.update_service.assert_not_called()

    def test_reload_keeps_awake(self, watcher, clock, client):
        self.reloading = True
        clock.now += IDLE_SECONDS
        assert not watcher.check()
        client.update_service.assert_not_called()


class TestMain:
    @pytest.mark.parametrize(
        "environ",
        [
            {},
            {"ECS_CLUSTER": "otd-cluster"},
            {"ECS_SERVICE": "otd-service"},
            {"IDLE_MINUTES": "0", "ECS_CLUSTER": "c", "ECS_SERVICE": "s"},
        ],
    )
    def test_disabled_makes_no_aws_calls(self, environ):
        ecs_client = MagicMock()
        sleep = MagicMock()
        idle_watcher.main(environ=environ, sleep=sleep, ecs_client=ecs_client)
        ecs_client.assert_not_called()
        sleep.assert_not_called()

    def test_enabled(self, spool, last_request_path):
        environ = {"IDLE_MINUTES": "5", "ECS_CLUSTER": "c", "ECS_SERVICE": "s"}
        sleep = MagicMock()
        ecs_client = MagicMock()
        with patch("idle_watcher.IdleWatcher") as watcher_class:
            watcher_class.return_value.check.side_effect = [False, False, True]
            idle_watcher.main(environ=environ, sleep=sleep, ecs_client=ecs_client)

        watcher_class.assert_called_once_with(
            "c", "s", 5.0, spool.as_posix(), ecs_client=ecs_client
        )
        assert sleep.call_count == 2
        sleep.assert_called_with(idle_watcher.DEFAULT_CHECK_SECONDS)
        assert activity.last_request_time() is not None
