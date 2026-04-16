# darktable-cli-server

Kleiner HTTP-Service, der RAW-Dateien per Upload entgegennimmt und mit `darktable-cli` als JPEG rendert.

## Build

```bash
docker build -t darktable-cli-server .
## Run
```bash
docker run --rm -p 8000:8000 darktable-cli-server
## Health
```bash
curl http://localhost:8000/health
## Render
```bash
curl -X POST http://localhost:8000/render \
  -F "file=@/path/to/file.dng" \
  --output output.jpg
