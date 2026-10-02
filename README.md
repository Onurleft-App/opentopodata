# Open Topo Data

__Documentation__: [www.opentopodata.org](https://www.opentopodata.org)


Open Topo Data is a REST API server for your elevation data.


```
curl http://localhost:5000/v1/test-dataset?locations=56,123
```

```json
{
    "results": [{
        "elevation": 815.0,
        "location": {
            "lat": 56.0,
            "lng": 123.0
        },
        "dataset": "test-dataset"
    }],
    "status": "OK"
}
```


You can self-host with your own dataset or use the [free public API](https://www.opentopodata.org) which is configured with a number of open elevation datasets. The API is largely compatible with the Google Maps Elevation API.




## Installation

Install [docker](https://docs.docker.com/install/) and [git](https://git-scm.com/book/en/v2/Getting-Started-Installing-Git) then run:

```bash
git clone https://github.com/ajnisbet/opentopodata.git
cd opentopodata
make build
make run
```

This will start an Open Topo Data server on `http://localhost:5000/`.

On M1/Apple Silicon (and other ARM machines), some geo dependencies don't have prebuilt wheels, so use the ARM image instead. It's larger and slower to build, but works the same:

```bash
make build-m1
make run
```

Some extra steps might be needed for [Windows and Kubernetes](https://www.opentopodata.org/server/).

### Downloading USGS tiles on request

`POST /tiles/ensure` with a body like `{"tiles": ["n40w077"]}` queues USGS NED 10m tiles (named by their north-west corner) for download in the background, and returns `202` straight away. Once a tile is downloaded, Open Topo Data reloads itself and the tile's points start returning elevations. `GET /tiles/status` shows progress. The `data` folder must be mounted writable, which `make run` does. Configure the feature with these environment variables:

* `TILES_DATASET`: dataset in `config.yaml` to download into. Default: `ned10m`.
* `TILES_TOKEN`: if set, requests need an `Authorization: Bearer <token>` header.
* `TILES_MAX_PER_REQUEST`: max tiles per request. Default: `20`.

See the [server docs](docs/server.md#downloading-usgs-tiles-on-request) for details.

### Going to sleep when idle on ECS

When running as an AWS ECS service, Open Topo Data can scale its own service to 0 tasks after a period with no `/v1/...` or `/tiles/...` requests. It stays awake while tiles are downloading. It's off unless `ECS_CLUSTER` and `ECS_SERVICE` are set:

* `IDLE_MINUTES`: minutes without requests before going to sleep. `0` turns it off. Default: `30`.
* `ECS_CLUSTER` and `ECS_SERVICE`: this server's own ECS cluster and service.
* `IDLE_CHECK_SECONDS`: how often to check. Default: `60`.

The ECS task role needs `ecs:UpdateService` on `arn:aws:ecs:<region>:<account>:service/<cluster>/<service>`. See the [server docs](docs/server.md#going-to-sleep-when-idle-on-ecs) for details.


Open Topo Data supports a wide range of raster file formats and tiling schemes, including most of those used by popular open elevation datasets. See the [server docs](https://www.opentopodata.org/server/) for more about configuration and adding datasets.



## Usage

Open Topo Data has a single endpoint: a point query endpoint that returns the elevation at a single point or a series of points.


```
curl http://localhost:5000/v1/test-dataset?locations=56,123
```

```json
{
    "results": [{
        "elevation": 815.0,
        "location": {
            "lat": 56.0,
            "lng": 123.0
        },
        "dataset": "test-dataset"
    }],
    "status": "OK"
}
```

The interpolation algorithm used can be configured as a request parameter, multiple locations can be given in a single request, and locations can also be provided in Google Polyline format.


See the [API docs](https://www.opentopodata.org/api/) for more about request and response formats.



## Public API

I'm hosting a free public API at [api.opentopodata.org](https://api.opentopodata.org).


```
curl https://api.opentopodata.org/v1/srtm30m?locations=57.688709,11.976404
```

```json
{
  "results": [
    {
      "elevation": 55.0,
      "location": {
        "lat": 57.688709,
        "lng": 11.976404
      },
      "dataset": "srtm30m"
    }
  ],
  "status": "OK"
}
```

The following datasets are available on the public API:

* [ASTER](https://www.opentopodata.org/datasets/aster/)
* [ETOPO1](https://www.opentopodata.org/datasets/etopo1/)
* [EU-DEM](https://www.opentopodata.org/datasets/eudem/)
* [Mapzen](https://www.opentopodata.org/datasets/mapzen/)
* [NED 10m](https://www.opentopodata.org/datasets/ned/)
* [NZ DEM](https://www.opentopodata.org/datasets/nzdem/)
* [SRTM (30m or 90m)](https://www.opentopodata.org/datasets/srtm/)
* [EMOD Bathymetry](https://www.opentopodata.org/datasets/emod2018/)
* [GEBCO Bathymetry](https://www.opentopodata.org/datasets/gebco2020/)
* [BKG (200m)](https://www.opentopodata.org/datasets/bkg/)




## License
[MIT](https://choosealicense.com/licenses/mit/)


## Support

Need help getting Open Topo Data running? Send me an email at [andrew@opentopodata.org](mailto:andrew@opentopodata.org) or open an [issue](https://github.com/ajnisbet/opentopodata/issues)!


## Paid hosting

If you'd like me to host and manage an elevation API for your business, email me at [andrew@opentopodata.org](mailto:andrew@opentopodata.org) or check out my sister project [GPXZ](https://www.gpxz.io).
