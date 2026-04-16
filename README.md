# darktable-cli-server

Kleiner HTTP-Renderer-Service als Proof of Concept:
- Nimmt RAW-Dateien per `multipart/form-data` Upload entgegen
- Rendert im Container mit `darktable-cli` nach JPEG
- Liefert das erzeugte JPEG direkt als HTTP-Response zurueck
- Arbeitet ausschliesslich mit temporaeren Verzeichnissen

## Architektur

- FastAPI + Uvicorn fuer die HTTP-API
- `subprocess.run(...)` fuer `darktable-cli`
- Timeout-Schutz: 90 Sekunden
- Zuverlaessiges Cleanup:
  - Bei Fehlern sofortiges Loeschen im `finally`
  - Bei Erfolg Cleanup via `BackgroundTask` nach abgeschlossener Datei-Response

## API

### `GET /health`

Beispielantwort:

```json
{
  "status": "ok",
  "darktable_cli_available": true
}
```

### `POST /render`

`multipart/form-data`

Pflichtfeld:
- `file`: Upload-Datei (`.dng`, `.arw`, `.nef`, `.cr2`, `.cr3`, `.orf`, `.rw2`, `.raf`)

Optionale Felder:
- `width` (int, Default `2000`, Bereich `1..8000`)
- `height` (int, Default `2000`, Bereich `1..8000`)
- `quality` (int, Default `80`, Bereich `1..100`)
- `hq` (int, Default `0`, nur `0` oder `1`)
- `upscale` (int, Default `0`, nur `0` oder `1`)
- `apply_custom_presets` (bool, Default `false`)

Erfolgsantwort:
- `200 OK`
- `Content-Type: image/jpeg`
- `Content-Disposition: inline; filename="preview.jpg"`

Fehlerantworten (JSON):
- `400` ungueltige Parameter / leere Datei
- `415` nicht unterstuetzter Dateityp
- `500` Render-Fehler oder fehlendes `darktable-cli`
- `504` Timeout

## Starten

### Mit Docker Compose (empfohlen)

```bash
docker compose up --build
```

Service ist dann erreichbar unter:
- `http://localhost:8080`

### Mit Docker direkt

```bash
docker build -t darktable-cli-server .
docker run --rm -p 8080:8080 darktable-cli-server
```

## Testen

Health:

```bash
curl http://localhost:8080/health
```

Render (Beispiel):

```bash
curl -X POST "http://localhost:8080/render" \
  -F "file=@/path/to/file.dng" \
  -F "width=2000" \
  -F "height=2000" \
  -F "quality=80" \
  -o preview.jpg
```

Lokale Tests (ohne Docker):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

## Annahmen

- `darktable-cli` ist im Container ueber das Paket `darktable` verfuegbar.
- Der Service liefert ein einzelnes JPEG pro Request.
- Fuer den POC wird kein Job-Queueing, kein Caching und keine Authentifizierung umgesetzt.

## Bekannte Grenzen des POC

- `darktable-cli` ist CPU-intensiv; bei parallelen grossen RAW-Dateien kann die Antwortzeit steigen.
- Es gibt keine Dateigroessenbegrenzung auf API-Ebene.
- Metadaten- und Farbprofil-Handling werden nicht gesondert konfiguriert.
- Keine persistente Speicherung der Ergebnisse (gewollt, da nur temporaere Verarbeitung).
