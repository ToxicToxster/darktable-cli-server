import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

app = FastAPI(title="darktable-cli-server")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/render")
async def render_raw_to_jpeg(file: UploadFile = File(...)):
    suffix = Path(file.filename or "upload.raw").suffix.lower()
    if not suffix:
        suffix = ".raw"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        input_path = tmp_path / f"input{suffix}"
        output_path = tmp_path / "output.jpg"

        with input_path.open("wb") as f:
            shutil.copyfileobj(file.file, f)

        cmd = [
            "darktable-cli",
            str(input_path),
            str(output_path),
            "--width", "2000",
            "--height", "2000",
            "--hq", "0",
            "--upscale", "0",
            "--apply-custom-presets", "0",
            "--core",
            "--conf", "plugins/imageio/format/jpeg/quality=80",
        ]

        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=300,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="darktable-cli timeout")

        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail={
                    "message": "darktable-cli failed",
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "returncode": result.returncode,
                },
            )

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise HTTPException(status_code=500, detail="No JPEG output created")

        return FileResponse(
            path=str(output_path),
            media_type="image/jpeg",
            filename="output.jpg",
        )
