from decimal import Decimal
import os
import sys

import pytest
from unittest.mock import patch

from opentopodata import api, config, tiles

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "docker"))
import tile_downloader


EXISTING_FILENAMES = [
    "USGS_13_n38w077_renamed.tif",
    "USGS_13_n38w078_renamed.tif",
    "USGS_13_n39w078_renamed.tif",
]
EXISTING_USGS_NAMES = ["n39w077", "n39w078", "n40w078"]
DATASET_NAME = "ned10m"


@pytest.fixture
def dataset_folder(tmp_path):
    folder = tmp_path / "ned10m"
    folder.mkdir()
    for filename in EXISTING_FILENAMES:
        (folder / filename).write_bytes(b"")
    return folder


@pytest.fixture
def spool(tmp_path, monkeypatch):
    spool = tmp_path / "spool"
    monkeypatch.setenv("TILES_SPOOL_DIR", spool.as_posix())
    monkeypatch.delenv("TILES_TOKEN", raising=False)
    monkeypatch.delenv("TILES_DATASET", raising=False)
    monkeypatch.delenv("TILES_MAX_PER_REQUEST", raising=False)
    return spool


@pytest.fixture
def patch_config(dataset_folder):
    otd_config = {
        "datasets": [{"name": DATASET_NAME, "path": dataset_folder.as_posix()}],
        "access_control_allow_origin": None,
    }
    with patch("opentopodata.api._load_config", return_value=otd_config):
        yield


class FakeResponse:
    def __init__(self, status_code=200, body=b"", content_length=None):
        self.status_code = status_code
        self.body = body
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def iter_content(self, chunk_size):
        for i in range(0, len(self.body), chunk_size):
            yield self.body[i : i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class TestTileNames:
    @pytest.mark.parametrize("name", ["n39w077", "s01e010", "n00e000", "n71w180"])
    def test_valid(self, name):
        assert tiles.is_valid_tile_name(name)

    @pytest.mark.parametrize(
        "name",
        ["N39W077", "n39w77", "n039w077", "n39x077", "n39w077.tif", "", None, 39],
    )
    def test_invalid(self, name):
        assert not tiles.is_valid_tile_name(name)

    @pytest.mark.parametrize(
        "usgs_name, filename",
        list(zip(EXISTING_USGS_NAMES, EXISTING_FILENAMES))
        + [
            ("n40w077", "USGS_13_n39w077_renamed.tif"),
            ("n10e010", "USGS_13_n09e010_renamed.tif"),
            ("n00w080", "USGS_13_s01w080_renamed.tif"),
            ("s05w070", "USGS_13_s06w070_renamed.tif"),
        ],
    )
    def test_renamed_filename(self, usgs_name, filename):
        assert tiles.renamed_filename(usgs_name) == filename

    def test_corner_matches_otd(self):
        assert tiles.tile_corner("n40w077") == (Decimal(39), Decimal(-77))

    def test_url(self):
        url = tiles.tile_url("n39w077")
        assert url.endswith("/13/TIFF/current/n39w077/USGS_13_n39w077.tif")

    def test_part_files_ignored_by_otd(self):
        assert config.Dataset._is_aux_file("USGS_13_n39w077_renamed.tif.part")

    def test_existing_corners(self, dataset_folder):
        (dataset_folder / "USGS_13_n39w077_renamed.tif.part").write_bytes(b"")
        corners = tiles.existing_corners(dataset_folder.as_posix())
        assert corners == {tiles.tile_corner(t) for t in EXISTING_USGS_NAMES}


class TestEnsureTiles:
    url = "/tiles/ensure"

    def post(self, json=None, **kwargs):
        test_api = api.app.test_client()
        return test_api.post(self.url, json=json, **kwargs)

    @pytest.mark.parametrize(
        "body",
        [
            None,
            [],
            {},
            {"tiles": []},
            {"tiles": "n39w077"},
            {"tiles": ["n39w077", "bad"]},
            {"tiles": ["N39W077"]},
            {"tiles": [39]},
        ],
    )
    def test_invalid_body(self, spool, patch_config, body):
        response = self.post(json=body)
        assert response.status_code == 400
        assert response.json["status"] == "INVALID_REQUEST"

    def test_not_json(self, spool, patch_config):
        response = self.post(data="tiles=n39w077")
        assert response.status_code == 400

    def test_too_many_tiles(self, spool, patch_config, monkeypatch):
        monkeypatch.setenv("TILES_MAX_PER_REQUEST", "2")
        response = self.post(json={"tiles": ["n41w077", "n42w077", "n43w077"]})
        assert response.status_code == 400
        assert "limit is 2" in response.json["error"]

    def test_default_limit(self, spool, patch_config):
        names = [f"n{lat}w077" for lat in range(41, 62)]
        assert self.post(json={"tiles": names[:20]}).status_code == 202
        assert self.post(json={"tiles": names}).status_code == 400

    @pytest.mark.parametrize(
        "headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "secret"}]
    )
    def test_bad_token(self, spool, patch_config, monkeypatch, headers):
        monkeypatch.setenv("TILES_TOKEN", "secret")
        response = self.post(json={"tiles": ["n40w077"]}, headers=headers)
        assert response.status_code == 401
        assert not tiles.queued_tiles(spool.as_posix())

    def test_good_token(self, spool, patch_config, monkeypatch):
        monkeypatch.setenv("TILES_TOKEN", "secret")
        headers = {"Authorization": "Bearer secret"}
        response = self.post(json={"tiles": ["n40w077"]}, headers=headers)
        assert response.status_code == 202

    def test_present_and_queued(self, spool, patch_config):
        body = {"tiles": ["n40w077", "n39w078", "n39w077", "n40w078", "n40w077"]}
        response = self.post(json=body)
        assert response.status_code == 202
        assert response.json == {
            "status": "OK",
            "present": ["n39w078", "n39w077", "n40w078"],
            "queued": ["n40w077"],
            "no_data": [],
        }
        assert tiles.queued_tiles(spool.as_posix()) == ["n40w077"]

    def test_known_no_data(self, spool, patch_config):
        tiles.write_state(spool.as_posix(), {"no_data": ["n30w070"]})
        response = self.post(json={"tiles": ["n30w070", "n40w077"]})
        assert response.status_code == 202
        assert response.json["no_data"] == ["n30w070"]
        assert response.json["queued"] == ["n40w077"]
        assert tiles.queued_tiles(spool.as_posix()) == ["n40w077"]

    def test_dataset_from_env(self, spool, patch_config, monkeypatch):
        monkeypatch.setenv("TILES_DATASET", "missing")
        response = self.post(json={"tiles": ["n40w077"]})
        assert response.status_code == 500
        assert "missing" in response.json["error"]

    def test_status(self, spool, patch_config, monkeypatch):
        self.post(json={"tiles": ["n40w077"]})
        response = api.app.test_client().get("/tiles/status")
        assert response.status_code == 200
        assert response.json["queue"] == ["n40w077"]

        monkeypatch.setenv("TILES_TOKEN", "secret")
        response = api.app.test_client().get("/tiles/status")
        assert response.status_code == 401


class TestDownloadTile:
    tile = "n40w077"
    filename = "USGS_13_n39w077_renamed.tif"

    def test_complete_download(self, dataset_folder):
        body = b"x" * 2500
        response = FakeResponse(body=body, content_length=len(body))
        with patch("opentopodata.tiles.requests.get", return_value=response) as get:
            path = tiles.download_tile(self.tile, dataset_folder.as_posix())

        assert path == (dataset_folder / self.filename).as_posix()
        assert (dataset_folder / self.filename).read_bytes() == body
        assert not (dataset_folder / (self.filename + ".part")).exists()
        assert get.call_args.args[0] == tiles.tile_url(self.tile)
        assert get.call_args.kwargs["stream"]
        assert get.call_args.kwargs["headers"]["User-Agent"] == tiles.USER_AGENT

    def test_short_download(self, dataset_folder):
        response = FakeResponse(body=b"x" * 100, content_length=2500)
        with patch("opentopodata.tiles.requests.get", return_value=response):
            with pytest.raises(tiles.DownloadError):
                tiles.download_tile(self.tile, dataset_folder.as_posix())

        assert not (dataset_folder / self.filename).exists()
        assert not (dataset_folder / (self.filename + ".part")).exists()

    def test_missing_content_length(self, dataset_folder):
        response = FakeResponse(body=b"x" * 100)
        with patch("opentopodata.tiles.requests.get", return_value=response):
            with pytest.raises(tiles.DownloadError):
                tiles.download_tile(self.tile, dataset_folder.as_posix())
        assert not (dataset_folder / self.filename).exists()

    @pytest.mark.parametrize("status_code", [403, 404])
    def test_no_data(self, dataset_folder, status_code):
        response = FakeResponse(status_code=status_code)
        with patch("opentopodata.tiles.requests.get", return_value=response):
            with pytest.raises(tiles.NoDataError):
                tiles.download_tile(self.tile, dataset_folder.as_posix())
        assert not (dataset_folder / self.filename).exists()

    def test_server_error(self, dataset_folder):
        response = FakeResponse(status_code=503)
        with patch("opentopodata.tiles.requests.get", return_value=response):
            with pytest.raises(tiles.DownloadError):
                tiles.download_tile(self.tile, dataset_folder.as_posix())


class TestDownloader:
    @pytest.fixture
    def clock(self):
        class Clock:
            now = 1_000_000.0

            def __call__(self):
                return self.now

        return Clock()

    @pytest.fixture
    def downloader(self, spool, dataset_folder, clock):
        self.restarts = 0

        def restart_otd():
            self.restarts += 1

        return tile_downloader.Downloader(
            spool.as_posix(),
            find_folder=lambda: dataset_folder.as_posix(),
            restart_otd=restart_otd,
            clock=clock,
        )

    def test_downloads_and_restarts_once(self, spool, dataset_folder, downloader):
        tiles.queue_tiles(["n40w077", "n41w077"], spool.as_posix())
        with patch("opentopodata.tiles.requests.get") as get:
            get.side_effect = lambda *a, **k: FakeResponse(body=b"x", content_length=1)
            downloader.run_once()

        assert get.call_count == 2
        assert (dataset_folder / "USGS_13_n39w077_renamed.tif").exists()
        assert (dataset_folder / "USGS_13_n40w077_renamed.tif").exists()
        assert tiles.queued_tiles(spool.as_posix()) == []
        assert self.restarts == 1
        state = tiles.read_state(spool.as_posix())
        assert [d["tile"] for d in state["recent_downloads"]] == ["n40w077", "n41w077"]
        assert state["current"] is None

    def test_present_tile_not_downloaded(self, spool, downloader):
        tiles.queue_tiles(["n40w078"], spool.as_posix())
        with patch("opentopodata.tiles.requests.get") as get:
            downloader.run_once()
        assert get.call_count == 0
        assert tiles.queued_tiles(spool.as_posix()) == []
        assert self.restarts == 0

    def test_no_data_recorded(self, spool, downloader):
        tiles.queue_tiles(["n30w070"], spool.as_posix())
        response = FakeResponse(status_code=404)
        with patch("opentopodata.tiles.requests.get", return_value=response) as get:
            downloader.run_once()
            tiles.queue_tiles(["n30w070"], spool.as_posix())
            downloader.run_once()

        assert get.call_count == 1
        assert tiles.read_state(spool.as_posix())["no_data"] == ["n30w070"]
        assert tiles.queued_tiles(spool.as_posix()) == []
        assert self.restarts == 0

    def test_retries_then_gives_up(self, spool, downloader, clock):
        tiles.queue_tiles(["n40w077"], spool.as_posix())
        response = FakeResponse(status_code=500)
        with patch("opentopodata.tiles.requests.get", return_value=response) as get:
            downloader.run_once()
            assert get.call_count == 1
            assert tiles.queued_tiles(spool.as_posix()) == ["n40w077"]

            clock.now += 29
            downloader.run_once()
            assert get.call_count == 1

            clock.now += 1
            downloader.run_once()
            assert get.call_count == 2

            clock.now += 120
            downloader.run_once()
            assert get.call_count == 3

        assert tiles.queued_tiles(spool.as_posix()) == []
        state = tiles.read_state(spool.as_posix())
        assert state["recent_failures"][-1]["tile"] == "n40w077"
        assert state["retrying"] == {}

    def test_restart_not_held_back_by_long_batch(self, spool, downloader, clock):
        names = ["n40w077", "n41w077", "n42w077", "n43w077"]
        tiles.queue_tiles(names, spool.as_posix())

        def slow_download(*args, **kwargs):
            clock.now += 100
            return FakeResponse(body=b"x", content_length=1)

        with patch("opentopodata.tiles.requests.get", side_effect=slow_download):
            downloader.run_once()

        # Restarted after the 3rd tile, once the 1st had waited 2 minutes, then
        # again after the batch.
        assert self.restarts == 2
