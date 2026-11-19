# -*- coding: utf-8 -*-
"""OCR en español con Windows.Media.Ocr: imágenes y texto permanecen en esta PC."""
import functools
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

MOTOR = "windows-media-ocr"
LADO_MAYOR_PX = 3600
PUENTE = Path(__file__).with_name("ocr_windows.ps1")


class ErrorOCR(Exception):
    pass


def _ejecutar(*argumentos, timeout=120) -> dict:
    ejecutable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / (
        "System32/WindowsPowerShell/v1.0/powershell.exe")
    try:
        proceso = subprocess.run(
            [str(ejecutable), "-NoProfile", "-NonInteractive", "-File", str(PUENTE), *argumentos],
            capture_output=True, encoding="utf-8-sig", errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise ErrorOCR("El OCR local agotó su tiempo de espera. Reintenta con una página o un recorte menor.") from None
    except OSError:
        raise ErrorOCR("No se pudo iniciar Windows PowerShell para ejecutar el OCR local.") from None
    try:
        datos = json.loads(proceso.stdout.strip())
    except (ValueError, TypeError):
        raise ErrorOCR("Windows no pudo ejecutar el puente OCR. Revisa la disponibilidad de Windows.Media.Ocr.") from None
    if proceso.returncode or not isinstance(datos, dict) or datos.get("error"):
        causa = datos.get("error", "error del motor") if isinstance(datos, dict) else "respuesta inválida"
        raise ErrorOCR("OCR local: " + str(causa)[:300])
    return datos


@functools.lru_cache(maxsize=1)
def estado() -> dict:
    if sys.platform != "win32":
        return {"disponible": False, "causa": "El OCR local de Windows solo está disponible en Windows."}
    try:
        return {**_ejecutar("-Estado", timeout=20), "motor": MOTOR, "causa": ""}
    except ErrorOCR as e:
        return {"disponible": False, "motor": MOTOR, "causa": str(e)}


def reconocer(png: bytes, region=None) -> dict:
    disponibilidad = estado()
    if not disponibilidad["disponible"]:
        raise ErrorOCR(disponibilidad["causa"])
    with tempfile.TemporaryDirectory(prefix="pdf2ctx_ocr_") as temporal:
        imagen = Path(temporal) / "pagina.png"
        imagen.write_bytes(png)
        resultado = _ejecutar("-Imagen", str(imagen))
    return {**resultado, "region": region, "modo": "region" if region else "pagina_completa",
            "local": True, "motor": MOTOR, "metodo": "ocr/windows-media/" + resultado["idioma"],
            "modelo_respuesta": "Windows OCR · " + resultado["idioma"], "request_id": None,
            "truncada": False}
