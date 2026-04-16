# darktable-cli-server

A production-grade local HTTP service that wraps `darktable-cli` for rendering RAW/DNG files into JPEG, PNG, or TIFF.

## What it does

- **`POST /render`** — Flexible rendering: raw binary body + query parameters. Capabilities depend on `SECURITY_LEVEL`.
- **`POST /preview`** — Fixed preset preview for integrations (e.g. Nextcloud): raw binary body, no parameters, all settings from server config.
- **`GET /health`** — Health check.
- **`GET /version`** — Version info including darktable availability.

Both `/render` and `/preview` use **identical transport**: raw HTTP body + `X-Filename` header → binary image response. The only difference is the control surface: `/preview` uses fixed server presets, `/render` accepts user-specified query parameters (gated by `SECURITY_LEVEL`).

The response filename preserves the original upload basename with the correct output extension.
Example: upload `IMG20251231222841.dng` → receive `IMG20251231222841.jpg`.

## Security model

### Security levels

| Level | `/preview` | `/render` | `dt_arg`/`dt_conf` | Access security |
|-------|-----------|-----------|---------------------|-----------------|
| **1** | ✓ | ✗ (403) | ✗ | Optional |
| **2** (default) | ✓ | ✓ (safe params only) | ✗ (403) | Optional |
| **3** | ✓ | ✓ (all params) | ✓ (validated) | **Forced on** |

Set via `SECURITY_LEVEL` environment variable.

Level 3 is a **trusted local power-user mode**, not a normal LAN-safe default. It enables advanced passthrough functionality (`dt_arg`, `dt_conf`) and should be treated as a higher-risk operating mode. If you use level 3, combine it with access-security controls and preferably `ACCESS_LOCALHOST_ONLY=true` or similarly strict network restrictions.

### Always-on hardening

These protections are always active regardless of security level:

| Protection | Description |
|---|---|
| **No shell injection** | Commands are always Python lists, never concatenated strings. `shell=True` is never used. |
| **No path traversal** | Uploaded filenames are never used as filesystem paths. Temp filenames are generated internally. |
| **Safe temp files** | Dedicated temp directory. Cleanup runs via background task or immediately on error. |
| **Input validation** | Upload size limits, integer range checks, format allowlists, extension allowlists. |
| **Bounded concurrency** | `asyncio.Semaphore` limits parallel renders. Excess requests get 504. |
| **Timeout protection** | `darktable-cli` is killed after `REQUEST_TIMEOUT` seconds. |
| **Container hardening** | Runs as non-root user `dtuser` inside Docker. |

### Access security (optional layer)

Set `ACCESS_SECURITY_ENABLED=true` to enable. At `SECURITY_LEVEL=3`, access security is effectively forced on internally, but only the per-feature flags set to `true` actually activate their feature:

| Feature | Variable | Description |
|---|---|---|
| API key | `ACCESS_REQUIRE_API_KEY=true` + `API_KEY=...` | Require `X-API-Key` header on protected endpoints |
| Localhost only | `ACCESS_LOCALHOST_ONLY=true` | Only allow loopback IPs (direct socket, no proxy trust) |
| IP allowlist | `ACCESS_ENABLE_IP_ALLOWLIST=true` + `ACCESS_IP_ALLOWLIST=...` | Comma-separated IPs/CIDRs |
| CORS restriction | `ACCESS_ENABLE_CORS_RESTRICTION=true` + `ACCESS_CORS_ALLOWED_ORIGINS=...` | Browser-only protection, not primary access control |
| Rate limiting | `ACCESS_ENABLE_RATE_LIMIT=true` + `ACCESS_RATE_LIMIT_RPM=60` | In-memory per-IP sliding window |

All IP-based checks use `request.client.host` only — no `X-Forwarded-For` or proxy headers are trusted.

CORS is only relevant for browser clients. It does not protect server-to-server callers, CLI tools, reverse proxies, or other non-browser clients, so it must not be treated as a primary access-control mechanism.

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

Raw binary body + query parameters. Returns the rendered image as binary response.

**Headers:**

| Header | Required | Description |
|---|---|---|
| `Content-Type` | yes | `application/octet-stream` |
| `X-Filename` | yes | Original filename (e.g. `IMG_001.dng`) |
| `X-API-Key` | if configured | API key |

**Query parameters:**

| Parameter | Default | Description |
|---|---|---|
| `output_format` | `jpg` | `jpg`, `jpeg`, `png`, `tif`, `tiff` |
| `width` | `0` (no limit) | Max width (0 = darktable default) |
| `height` | `0` (no limit) | Max height |
| `quality` | `80` | JPEG quality (1-100) |
| `hq` | `false` | High quality resampling |
| `upscale` | `false` | Allow upscaling |
| `apply_custom_presets` | `false` | Apply custom presets |
| `dt_arg` | | Extra darktable-cli argv tokens (repeated, level 3 only) |
| `dt_conf` | | Extra `--conf key=value` entries (repeated, level 3 only) |

**Error responses (JSON):** 400, 403, 413, 415, 500, 504 using the unified shape:

```json
{
  "error": "Human-readable summary",
  "details": {"optional": "structured context"}
}
```

### `POST /preview`

Fixed server-side preset endpoint. The RAW file is sent as the raw HTTP body. All rendering settings come from server configuration.

**Headers:**

| Header | Required | Description |
|---|---|---|
| `Content-Type` | yes | `application/octet-stream` |
| `X-Filename` | yes | Original filename (e.g. `IMG_001.dng`) |
| `X-API-Key` | if configured | API key |

**Response:** rendered image with `Content-Type` (e.g. `image/jpeg`) and `Content-Disposition: inline; filename="<name>.<ext>"`.

**Error responses (JSON):** 400, 413, 415, 500, 504 using the same unified error shape.

## Configuration

All settings via environment variables, `.env` file, or Docker Compose. See `.env.example` for the full list.

| Variable | Default | Description |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8000` | Bind port |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` |
| `SECURITY_LEVEL` | `2` | 1, 2, or 3 (see table above) |
| `MAX_UPLOAD_BYTES` | `209715200` (200 MB) | Maximum upload size |
| `REQUEST_TIMEOUT` | `120` | darktable-cli timeout in seconds |
| `MAX_CONCURRENT_RENDERS` | `4` | Concurrent render limit |
| `TEMP_DIR` | `/tmp/darktable-work` | Working directory for temp files |
| `API_KEY` | *(empty)* | API key value (used with `ACCESS_REQUIRE_API_KEY`) |
| `PREVIEW_FORMAT` | `jpg` | Preview output format |
| `PREVIEW_QUALITY` | `80` | Preview JPEG quality |
| `PREVIEW_WIDTH` | `2000` | Preview max width |
| `PREVIEW_HEIGHT` | `2000` | Preview max height |
| `PREVIEW_HQ` | `false` | Preview high quality |
| `PREVIEW_UPSCALE` | `false` | Preview upscaling |
| `PREVIEW_APPLY_CUSTOM_PRESETS` | `false` | Preview custom presets |
| `ALLOWED_OUTPUT_FORMATS` | `jpg,jpeg,png,tif,tiff` | Allowed output formats |
| `ALLOWED_RAW_EXTENSIONS` | `.dng,.arw,.nef,...` | Allowed input extensions |
| `ACCESS_SECURITY_ENABLED` | `false` | Master switch for access features |
| `ACCESS_REQUIRE_API_KEY` | `false` | Require API key header |
| `ACCESS_LOCALHOST_ONLY` | `false` | Restrict to localhost |
| `ACCESS_ENABLE_IP_ALLOWLIST` | `false` | Enable IP allowlist |
| `ACCESS_IP_ALLOWLIST` | *(empty)* | Comma-separated IPs/CIDRs |
| `ACCESS_ENABLE_CORS_RESTRICTION` | `false` | Enable CORS restriction |
| `ACCESS_CORS_ALLOWED_ORIGINS` | *(empty)* | Comma-separated origins |
| `ACCESS_ENABLE_RATE_LIMIT` | `false` | Enable rate limiting |
| `ACCESS_RATE_LIMIT_RPM` | `60` | Requests per minute per IP |

## Quick start

### Docker (recommended)

```bash
docker build -t darktable-cli-server .
docker run --rm -e HOST=0.0.0.0 -e PORT=8000 -p 8000:8000 darktable-cli-server
```

To change the runtime port, set `PORT` and publish the same container port, for example `-e PORT=9000 -p 9000:9000`.

### Docker Compose

```bash
docker compose up --build
```

`docker-compose.yml` now forwards `HOST` and `PORT` into the container and uses `PORT` for the published port mapping.

### Local Python

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Requires darktable-cli installed on the host
HOST=0.0.0.0 PORT=8000 python -m app
```

## Usage examples

### Health check

```bash
curl http://localhost:8000/health
```

### Render a RAW file to JPEG

```bash
curl -X POST "http://localhost:8000/render?output_format=jpg&width=2000&quality=90" \
  -H "Content-Type: application/octet-stream" \
  -H "X-Filename: IMG_001.dng" \
  --data-binary "@/path/to/IMG_001.dng" \
  -OJ
```

### Preview with server defaults

```bash
curl -X POST http://localhost:8000/preview \
  -H "Content-Type: application/octet-stream" \
  -H "X-Filename: IMG_001.dng" \
  --data-binary "@/path/to/IMG_001.dng" \
  -OJ
```

### Render to PNG

```bash
curl -X POST "http://localhost:8000/render?output_format=png&width=4000" \
  -H "Content-Type: application/octet-stream" \
  -H "X-Filename: photo.arw" \
  --data-binary "@/path/to/photo.arw" \
  -OJ
```

### With API key

```bash
curl -X POST "http://localhost:8000/render?quality=90" \
  -H "Content-Type: application/octet-stream" \
  -H "X-Filename: photo.dng" \
  -H "X-API-Key: your-secret-key" \
  --data-binary "@photo.dng" \
  -OJ
```

### Advanced: extra darktable options (level 3 only)

```bash
curl -X POST "http://localhost:8000/render?dt_arg=--verbose&dt_conf=plugins/imageio/format/jpeg/quality=95" \
  -H "Content-Type: application/octet-stream" \
  -H "X-Filename: photo.dng" \
  --data-binary "@photo.dng" \
  -OJ
```

### Postman

1. Method: `POST`, URL: `http://localhost:8000/render?output_format=jpg&quality=90`
2. Headers tab: set `X-Filename` to your filename (e.g. `photo.dng`)
3. Body tab → select **binary** → choose your RAW file
4. If API key is required: add `X-API-Key` header
5. Send and save the response

## Running tests

```bash
pip install -r requirements.txt
pytest -v
```

## Deployment recommendations

- **SECURITY_LEVEL=1** for pure preview integrations (minimal attack surface)
- **SECURITY_LEVEL=2** (default) for general use with safe parameter control
- **SECURITY_LEVEL=3** only for trusted local power-user scenarios that need `dt_arg`/`dt_conf` passthrough
- At level 3, enable concrete access controls such as API key, localhost-only mode, IP allowlist, or rate limiting as needed
- Prefer `ACCESS_LOCALHOST_ONLY=true` or an equally strict boundary when running level 3
- Use a reverse proxy (nginx, Traefik) for TLS termination and additional rate limiting
- For multi-worker deployments, use an external rate limiter instead of the built-in one

## Limitations

- `darktable-cli` is CPU-intensive; parallel large RAW files can saturate the server.
- No job queue or caching (by design for simplicity).
- No persistent storage of results.
- TIFF output quality is not configurable via `--conf` (darktable limitation).
- Rate limiter is in-memory, single-worker only.
- Sidecar `.xmp` files are not supported via the API.
