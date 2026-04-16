# darktable-cli-server

A production-grade local HTTP service that wraps `darktable-cli` for rendering RAW/DNG files into JPEG, PNG, or TIFF.

## What it does

- **`POST /render`** &mdash; General-purpose rendering: upload a RAW file, get a rendered image back with full parameter control.
- **`POST /preview`** &mdash; Quick preview: upload a RAW file, get a preview image using server-side defaults (no parameters needed).
- **`GET /health`** &mdash; Health check.
- **`GET /version`** &mdash; Version info including darktable availability.

The response filename always preserves the original upload basename with the correct output extension.
Example: upload `IMG20251231222841.dng` &rarr; receive `IMG20251231222841.jpg`.

## Security model

This service is designed for local/trusted-network use with defense-in-depth:

1. **No shell injection** &mdash; Commands are always built as Python lists, never concatenated strings. `shell=True` is never used.
2. **No path traversal** &mdash; Uploaded filenames are never used as filesystem paths. Internal temp filenames are generated; the original basename is only used for `Content-Disposition`.
3. **Safe temp files** &mdash; A dedicated temp directory is used. Cleanup runs via `BackgroundTask` after response delivery, or immediately on error.
4. **Input validation** &mdash; Upload size limits, integer range checks, format allowlists, extension allowlists.
5. **Bounded concurrency** &mdash; `asyncio.Semaphore` limits parallel renders. Requests exceeding the limit get 504.
6. **Timeout protection** &mdash; `darktable-cli` is killed after `REQUEST_TIMEOUT` seconds.
7. **Optional API key** &mdash; Set `API_KEY` to require `X-API-Key` header on `/render` and `/preview`.
8. **Container hardening** &mdash; Runs as non-root user `dtuser` inside Docker.

### Why arbitrary shell strings are not supported

The `dt_arg` and `dt_conf` fields provide safe extensibility for advanced darktable options. They are passed as individual argv tokens, validated, and checked against a denylist. Raw shell command strings are intentionally **not** supported because they would bypass all injection protections.

## API reference

### `GET /health`

```json
{"status": "ok"}
```

### `GET /version`

```json
{
  "app_version": "1.0.0",
  "python_version": "3.12.x",
  "darktable_cli_available": true,
  "darktable_version": "darktable 4.x.y"
}
```

### `POST /render`

Multipart form upload. Returns the rendered image as binary response.

| Field | Type | Default | Description |
|---|---|---|---|
| `file` | file | *required* | RAW file upload |
| `output_format` | string | `jpg` | `jpg`, `jpeg`, `png`, `tif`, `tiff` |
| `width` | int | `0` (no limit) | Max width (0 = darktable default) |
| `height` | int | `0` (no limit) | Max height (0 = darktable default) |
| `quality` | int | `80` | JPEG quality (1-100) |
| `hq` | bool | `false` | High quality resampling |
| `upscale` | bool | `false` | Allow upscaling |
| `apply_custom_presets` | bool | `false` | Apply custom presets |
| `dt_arg` | string (repeated) | | Extra darktable-cli argv tokens |
| `dt_conf` | string (repeated) | | Extra `--conf key=value` entries |

Error responses are JSON:
- `400` invalid parameters
- `413` upload too large
- `415` unsupported file type
- `500` render failure
- `504` timeout

### `POST /preview`

Fixed server-side preset endpoint for integrations (e.g. Nextcloud). Accepts **only** the uploaded file — all rendering settings come exclusively from application configuration (env vars, `.env`, Docker Compose). No per-request parameters are accepted.

```bash
curl -X POST http://localhost:8000/preview \
  -F "file=@photo.dng" \
  -OJ
```

The output filename preserves the original upload basename with the configured preview format extension (e.g. `photo.dng` → `photo.jpg`).

## Configuration

All settings can be configured via CLI args, environment variables, `.env` file, or Docker Compose. Precedence: CLI > env > `.env` > defaults.

| Variable | Default | Description |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8000` | Bind port |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` |
| `MAX_UPLOAD_BYTES` | `209715200` (200 MB) | Maximum upload size |
| `REQUEST_TIMEOUT` | `120` | darktable-cli timeout in seconds |
| `MAX_CONCURRENT_RENDERS` | `4` | Concurrent render limit |
| `TEMP_DIR` | `/tmp/darktable-work` | Working directory for temp files |
| `API_KEY` | *(empty)* | If set, require `X-API-Key` header |
| `PREVIEW_FORMAT` | `jpg` | Preview output format |
| `PREVIEW_QUALITY` | `80` | Preview JPEG quality |
| `PREVIEW_WIDTH` | `2000` | Preview max width |
| `PREVIEW_HEIGHT` | `2000` | Preview max height |
| `PREVIEW_HQ` | `false` | Preview high quality |
| `PREVIEW_UPSCALE` | `false` | Preview upscaling |
| `PREVIEW_APPLY_CUSTOM_PRESETS` | `false` | Preview custom presets |
| `ALLOWED_OUTPUT_FORMATS` | `jpg,jpeg,png,tif,tiff` | Allowed output formats |
| `ALLOWED_RAW_EXTENSIONS` | `.dng,.arw,.nef,...` | Allowed input extensions |
| `DT_ARG_DENYLIST` | *(empty)* | Comma-separated denied dt_arg tokens |

## Quick start

### Docker (recommended)

```bash
docker build -t darktable-cli-server .
docker run --rm -p 8000:8000 darktable-cli-server
```

### Docker Compose

```bash
docker compose up --build
```

Edit `docker-compose.yml` environment variables to customize preview defaults.

### Local Python

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Requires darktable-cli installed on the host
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Usage examples

### Health check

```bash
curl http://localhost:8000/health
```

### Render a RAW file to JPEG

```bash
curl -X POST http://localhost:8000/render \
  -F "file=@/path/to/IMG_001.dng" \
  -F "output_format=jpg" \
  -F "width=2000" \
  -F "height=2000" \
  -F "quality=90" \
  -o IMG_001.jpg
```

### Preview with server defaults

```bash
curl -X POST http://localhost:8000/preview \
  -F "file=@/path/to/IMG_001.dng" \
  -o IMG_001_preview.jpg
```

### Render to PNG

```bash
curl -X POST http://localhost:8000/render \
  -F "file=@/path/to/photo.arw" \
  -F "output_format=png" \
  -F "width=4000" \
  -o photo.png
```

### With API key

```bash
curl -X POST http://localhost:8000/render \
  -H "X-API-Key: your-secret-key" \
  -F "file=@photo.dng" \
  -o photo.jpg
```

### Advanced: extra darktable options

```bash
curl -X POST http://localhost:8000/render \
  -F "file=@photo.dng" \
  -F "dt_arg=--verbose" \
  -F "dt_conf=plugins/imageio/format/jpeg/quality=95" \
  -o photo.jpg
```

## Running tests

```bash
pip install -r requirements.txt
pytest -v
```

Tests cover: filename sanitization, parameter validation, command building, endpoint responses, and API key auth.

## Limitations

- `darktable-cli` is CPU-intensive; parallel large RAW files can saturate the server.
- No job queue or caching (by design for simplicity).
- No persistent storage of results.
- TIFF output quality is not configurable via `--conf` (darktable limitation).
- The service trusts the network it runs on unless `API_KEY` is set.
- Sidecar `.xmp` files are not supported via the API.
