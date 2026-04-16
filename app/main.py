import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

logger = logging.getLogger("raw_renderer")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

SUPPORTED_EXTENSIONS = {
    ".dng",
    ".arw",
    ".nef",
    ".cr2",
    ".cr3",
    ".orf",
    ".rw2",
    ".raf",
}
RENDER_TIMEOUT_SECONDS = 90


def get_darktable_cli_path() -> Optional[str]:
    return shutil.which("darktable-cli")


def cleanup_tmpdir(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
        logger.debug("Temporary directory removed: %s", path)
    except Exception:
        logger.exception("Failed to remove temporary directory: %s", path)


def error_response(status_code: int, message: str, details: Optional[Any] = None) -> JSONResponse:
    payload = {"error": message}
    if details is not None:
        payload["details"] = details
    return JSONResponse(status_code=status_code, content=payload)


def parse_int_field(name: str, raw_value: str, minimum: int, maximum: int) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        raise ValueError(f"Parameter '{name}' muss eine Ganzzahl sein") from None
    if value < minimum or value > maximum:
        raise ValueError(f"Parameter '{name}' muss zwischen {minimum} und {maximum} liegen")
    return value


def parse_binary_int_field(name: str, raw_value: str) -> int:
    value = parse_int_field(name, raw_value, 0, 1)
    if value not in (0, 1):
        raise ValueError(f"Parameter '{name}' muss 0 oder 1 sein")
    return value


def parse_bool_field(name: str, raw_value: str) -> bool:
    normalized = str(raw_value).strip().lower()
    truthy = {"1", "true", "yes", "on"}
    falsy = {"0", "false", "no", "off"}
    if normalized in truthy:
        return True
    if normalized in falsy:
        return False
    raise ValueError(
        f"Parameter '{name}' muss ein Boolean sein (erlaubt: true/false/1/0/yes/no/on/off)"
    )


app = FastAPI(title="darktable-cli-server")


@app.on_event("startup")
def startup_check() -> None:
    cli_path = get_darktable_cli_path()
    if cli_path:
        logger.info("darktable-cli gefunden: %s", cli_path)
    else:
        logger.error(
            "darktable-cli wurde nicht gefunden. Der Endpunkt /render wird mit HTTP 500 antworten, "
            "bis darktable-cli im Container installiert ist."
        )


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "darktable_cli_available": get_darktable_cli_path() is not None,
    }


@app.post("/render")
async def render_raw_to_jpeg(
    file: UploadFile = File(...),
    width: str = Form("2000"),
    height: str = Form("2000"),
    quality: str = Form("80"),
    hq: str = Form("0"),
    upscale: str = Form("0"),
    apply_custom_presets: str = Form("false"),
):
    temp_dir = Path(tempfile.mkdtemp(prefix="raw-render-"))
    cleanup_in_finally = True

    try:
        cli_path = get_darktable_cli_path()
        if cli_path is None:
            return error_response(
                500,
                "darktable-cli ist nicht verfuegbar",
                "Installiere darktable-cli im Laufzeitumfeld und starte den Service neu.",
            )

        try:
            width_value = parse_int_field("width", width, 1, 8000)
            height_value = parse_int_field("height", height, 1, 8000)
            quality_value = parse_int_field("quality", quality, 1, 100)
            hq_value = parse_binary_int_field("hq", hq)
            upscale_value = parse_binary_int_field("upscale", upscale)
            apply_custom_presets_value = parse_bool_field("apply_custom_presets", apply_custom_presets)
        except ValueError as exc:
            return error_response(400, "Ungueltige Anfrageparameter", str(exc))

        if not file.filename:
            return error_response(400, "Keine Datei hochgeladen")

        suffix = Path(file.filename).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            return error_response(
                415,
                "Nicht unterstuetzter Dateityp",
                {
                    "received_extension": suffix or "",
                    "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
                },
            )

        input_path = temp_dir / f"input{suffix}"
        output_path = temp_dir / "output.jpg"

        with input_path.open("wb") as input_file:
            shutil.copyfileobj(file.file, input_file)

        if not input_path.exists() or input_path.stat().st_size == 0:
            return error_response(400, "Die hochgeladene Datei ist leer")

        cmd = [
            cli_path,
            str(input_path),
            str(output_path),
            "--width",
            str(width_value),
            "--height",
            str(height_value),
            "--hq",
            str(hq_value),
            "--upscale",
            str(upscale_value),
            "--apply-custom-presets",
            "true" if apply_custom_presets_value else "false",
            "--core",
            "--conf",
            f"plugins/imageio/format/jpeg/quality={quality_value}",
        ]

        logger.info("Starte Rendering mit darktable-cli")
        logger.debug("darktable-cli command: %s", cmd)

        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=RENDER_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            logger.error("darktable-cli timeout nach %s Sekunden", RENDER_TIMEOUT_SECONDS)
            return error_response(
                504,
                "Rendering-Timeout",
                {"timeout_seconds": RENDER_TIMEOUT_SECONDS, "stderr": exc.stderr or ""},
            )
        except FileNotFoundError:
            logger.exception("darktable-cli wurde beim Ausfuehren nicht gefunden")
            return error_response(
                500,
                "darktable-cli ist nicht installiert oder nicht im PATH",
            )

        logger.info("darktable-cli exit code: %s", result.returncode)
        if result.stdout:
            logger.info("darktable-cli stdout: %s", result.stdout.strip())
        if result.stderr:
            logger.warning("darktable-cli stderr: %s", result.stderr.strip())

        if result.returncode != 0:
            return error_response(
                500,
                "Render-Fehler durch darktable-cli",
                {
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                },
            )

        if not output_path.exists() or output_path.stat().st_size == 0:
            return error_response(500, "Kein gueltiges JPEG erzeugt")

        cleanup_in_finally = False
        return FileResponse(
            path=output_path,
            media_type="image/jpeg",
            headers={"Content-Disposition": 'inline; filename="preview.jpg"'},
            background=BackgroundTask(cleanup_tmpdir, temp_dir),
        )
    finally:
        await file.close()
        if cleanup_in_finally:
            cleanup_tmpdir(temp_dir)
