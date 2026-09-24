# Scrypted NVR Archive Browser

A self-contained, read-only viewer and MP4 exporter for an existing Scrypted
NVR recording store. It works directly from the recorded files: no running
Scrypted installation, license, database, or camera connection is required.

The service discovers cameras automatically from this layout:

```text
<camera>/<session>/session.json
<camera>/<session>/<hour>/<segment-start-epoch-ms>.rtsp
<camera>.events/...
<camera>.remote/...            # optional low-resolution stream
```

## Install beside the footage

Put this repository's files in the root of the recording store, beside the
camera directories. Then run:

```sh
cp .env.example .env
docker compose up -d --build
```

Open <http://localhost:8787>. On first start the viewer scans the archive and
builds an in-memory index. The scan repeats after the container is recreated;
no persistent cache is written beside the footage.

If you prefer to clone the repository into a child directory of the recording
store, set `NVR_DATA_DIR=..` in `.env`. `NVR_DATA_DIR=.` is the default and
means that `compose.yaml` is beside the footage.

The included `.gitignore` uses a deny-all allowlist. Even when the repository
is initialized at the footage root, `git add .` can stage only the application
source and cannot stage recordings, exports, caches, or `.env`.

## What the viewer does

- Discovers all camera folders from `session.json` files.
- Shows recorded days, contiguous coverage, and motion events on a timeline.
- Plays one camera or multiple cameras on a synchronized wall-clock timeline.
- Uses the high-resolution stream or the optional `.remote` substream.
- Generates hover previews on demand and reuses them only while the container
  is running.
- Remuxes the original H.264 RTP data into MP4 without re-encoding video.
- Exports individual clips or a time range from the command line.

Camera IDs are shown as names by default. Add friendly labels in `.env`:

```dotenv
NVR_CAMERA_NAMES=camera-id=Front Door,another-id=Garage
```

## Export clips

List discovered cameras and available time ranges:

```sh
docker compose run --rm exporter list
```

Export one clip:

```sh
docker compose run --rm exporter clip --cam <camera-id> \
  --start '2025-01-15 02:20' --duration 600 --out /export
```

Export a range as one-hour MP4 files:

```sh
docker compose run --rm exporter bulk --cam <camera-id> \
  --start 2025-01-15 --end 2025-01-16 --duration 3600 --out /export
```

Files are written to `./export`.

## Read-only guarantees

The recording store is protected in several layers:

| Layer | Guarantee |
|---|---|
| Archive mount | `${NVR_DATA_DIR:-.}:/data:ro` makes the entire recording store read-only inside the containers. |
| Volatile workspace | `/cache` is a size-limited tmpfs. Generated data lives in RAM and disappears with the container. |
| Container filesystem | The viewer root filesystem is read-only, apart from the `/tmp` and `/cache` tmpfs mounts. |
| Process | The service runs as unprivileged uid 10001 with all capabilities dropped and `no-new-privileges`. |
| Application | Startup fails if a configured writable path resolves inside the recording store. Archive files are opened only for reading. |

The viewer writes no cache directory on the host. Its index, JIT MP4s, and
previews live under the container's volatile `/cache`; Docker removes them with
the container. Explicit exporter output still goes to `./export`. The Docker
build context is only `./app`, so footage is never sent to the Docker daemon.

`NVR_CACHE_TMPFS_SIZE` sets the total RAM-backed workspace size (default `2g`).
`NVR_CACHE_LIMIT_GB` sets the MP4 portion (default `1.5`). This is temporary
working memory rather than durable caching: after a container restart, clips
and previews are generated again when requested. Linux may swap tmpfs pages
under memory pressure; disable swap on the Docker host if absolutely no backing
storage writes are acceptable.

There is no authentication. The default `NVR_BIND=127.0.0.1` exposes the UI
only on the local machine. Use `0.0.0.0` only on a trusted network or behind an
authenticated reverse proxy.

## Preview warming

`NVR_WARM` controls how preview frames are generated:

| Value | Behavior |
|---|---|
| `off` | Decode only when hovering. |
| `day` | Warm the opened day into volatile RAM; this is the default. |
| `all` | Also warm the entire archive in the background at startup. |

Warming performs scattered reads and consumes RAM. Use `off` for strictly JIT
behavior, especially with a mechanical or fragile source disk.
`NVR_WARM_DELAY_MS` adds a pause between batches, and `NVR_THUMB_BUCKET`
controls the interval between cached previews.

## How it works

Scrypted NVR stores recordings as RTSP-interleaved RTP packets. The reader
depacketizes H.264, preserves the original 90 kHz RTP timestamps, and writes an
MP4 sample table. It starts clips at the preceding keyframe because recording
segments may begin in the middle of a GOP. Preview generation uses recorded
seek-point sidecars to read only enough data to find a nearby keyframe.

## Development

The Python code has no third-party runtime dependencies. Run the unit tests
with:

```sh
python3 -m unittest discover -s tests -v
```

Project layout:

```text
compose.yaml          viewer and exporter services
.env.example          configuration reference
app/Dockerfile        minimal Python + ffmpeg image
app/nvr_lib.py        archive scanner, RTP reader, and MP4 muxer
app/nvr_server.py     timeline web UI and JSON/media endpoints
app/nvr_export.py     list, clip, and bulk export CLI
tests/                dependency-free unit tests
```
