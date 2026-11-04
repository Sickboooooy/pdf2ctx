#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
servidor_local.py — Banco de pruebas local de pdf2ctx

Sube un PDF, lo convierte a .md canónico (anclas [[p. N]]), corre el gate del kit y muestra,
página por página, la imagen del original, el texto .md editable y los hallazgos.

El veredicto (🟢 🟡 🔴) y su código (0/1/3) salen SOLO del gate: gate_reforzado.verificar(),
que corre gate_fidelidad.verificar() del kit y solo puede endurecer su veredicto, nunca relajarlo.
Este servidor no calcula veredictos: guarda el informe del gate y lo entrega tal cual.

Seguridad
  · Escucha solo en 127.0.0.1. Un Host distinto de localhost o 127.0.0.1 recibe 403.
  · Todo POST exige el token aleatorio que se inyecta en banco.html (cabecera X-Banco-Token).
  · Sin red saliente, salvo count_tokens cuando el usuario lo pide y lo confirma.
  · La llave ANTHROPIC_API_KEY se lee del entorno o, con --llavero, del llavero de macOS (elemento
    «pdf2ctx-anthropic»). Vive solo en la memoria del servidor; nunca va al navegador ni a los registros.

Uso
  python banco/servidor_local.py [--puerto 8787] [--trabajo banco/trabajo] [--llavero]
"""
import argparse
import base64
import datetime
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CARPETA_BANCO = Path(__file__).resolve().parent
CARPETA_KIT = CARPETA_BANCO.parent
sys.path.insert(0, str(CARPETA_KIT))  # el kit se importa; nunca se copia ni se edita

import gate_fidelidad as gate  # noqa: E402
import gate_reforzado as refuerzo  # noqa: E402
import medir_tokens  # noqa: E402
import pypdfium2 as pdfium  # noqa: E402
import transcripcion  # noqa: E402  (banco/transcripcion.py: Claude visión, capa NO verificada)
from docx import Document  # noqa: E402
from docx.enum.style import WD_STYLE_TYPE  # noqa: E402
from docx.shared import Pt, RGBColor  # noqa: E402

PUERTO_DEFECTO = 8787
HOSTS_PERMITIDOS = {"localhost", "127.0.0.1"}
TOPE_PDF = 50 * 1024 * 1024        # bytes de un PDF subido
TOPE_TEXTO = 20 * 1024 * 1024      # bytes de un .md subido o del JSON de re-verificación
TOPE_DESCARTE = 1024 * 1024        # cuerpo pequeño que se lee y se tira al rechazar una petición
ESCALA_IMAGEN = 1.5                # 1.5 × 72 dpi = 108 dpi
ESTILO_MARCA = "Marca de página"   # estilo del párrafo "p. N" en el .docx
ESTILO_TRANSCRIPCION = "Transcripción asistida"  # texto leído por Claude en imágenes: NO verificado
RE_ID = re.compile(r"[0-9a-f]{32}")
RE_IMAGEN = re.compile(r"(\d{1,5})\.png")
RE_MODELO = re.compile(r"[a-z0-9][a-z0-9.\-]{0,63}")
CARACTERES_NO_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")

SERVICIO_LLAVERO = "pdf2ctx-anthropic"  # nombre del elemento en el llavero de macOS
ORIGEN_LLAVE = "entorno"             # de dónde salió la llave (se muestra; la llave nunca)
CAUSA_LLAVERO = ""                   # por qué no se pudo leer el llavero, si se pidió

TOKEN = secrets.token_hex(32)        # se inyecta en banco.html; cambia en cada arranque
TRABAJO = CARPETA_BANCO / "trabajo"  # se cambia con --trabajo
CANDADO = threading.RLock()          # PDFium no es seguro entre hilos: una operación a la vez


class ErrorBanco(Exception):
    """Error que se responde al navegador como JSON, con su código HTTP."""

    def __init__(self, codigo: int, mensaje: str):
        super().__init__(mensaje)
        self.codigo = codigo
        self.mensaje = mensaje


# ───────────────────────── convertidores ─────────────────────────
# Punto de extensión. Un convertidor recibe (ruta_pdf, ruta_md), escribe un .md con una línea
# [[p. N]] por página del original, en orden, y devuelve la lista de retiros que declara
# ([{pagina, texto, motivo}], vacía si no quitó nada). Docling u otros entran aquí en Sprint 1.

def convertir_visible(ruta_pdf: Path, ruta_md: Path) -> list:
    """Capa de texto sin giro y sin el texto que no se ve; lo retirado se declara (gate_reforzado)."""
    return refuerzo.convertir_visible(ruta_pdf, ruta_md)


def convertir_minimo(ruta_pdf: Path, ruta_md: Path) -> list:
    """Convertidor del kit: capa de texto + anclas, tal cual. Mide tokens; NO prueba fidelidad."""
    gate.exportar_minimo(ruta_pdf, ruta_md)
    return []


CONVERTIDORES = {"visible": convertir_visible, "minimo": convertir_minimo}  # el primero es el de por defecto
CONVERTIDOR_DEFECTO = "visible"


# ───────────────────────── documentos en trabajo/ ─────────────────────────
# trabajo/<id>/  original.pdf · documento.md · informe.json · meta.json · paginas/ · tokens/

def carpeta_de(id_doc: str) -> Path:
    if not RE_ID.fullmatch(id_doc):
        raise ErrorBanco(400, "Identificador inválido: deben ser 32 caracteres hexadecimales")
    carpeta = TRABAJO / id_doc
    if not carpeta.is_dir():
        raise ErrorBanco(404, "Documento inexistente")
    return carpeta


def leer_md(carpeta: Path) -> str:
    # Bytes → texto sin traducir saltos de línea: lo que se muestra es exactamente lo guardado.
    return (carpeta / "documento.md").read_bytes().decode("utf-8")


def leer_json(ruta: Path) -> dict:
    return json.loads(ruta.read_text(encoding="utf-8"))


def escribir_json(ruta: Path, datos) -> None:
    ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")


def guardar_retiros(carpeta: Path, retiros: list) -> None:
    ruta = carpeta / "retiros.json"
    if retiros:
        escribir_json(ruta, {"retiros": retiros})
    elif ruta.exists():
        ruta.unlink()


def leer_retiros(carpeta: Path) -> list:
    ruta = carpeta / "retiros.json"
    return leer_json(ruta)["retiros"] if ruta.is_file() else []


def guardar_y_verificar(carpeta: Path, datos_md: bytes = None) -> dict:
    """Escribe el .md (si viene) y corre el gate sobre el .md completo, con los retiros declarados.
    El informe se guarda tal cual lo devuelve gate_reforzado.verificar()."""
    with CANDADO:
        if datos_md is not None:
            (carpeta / "documento.md").write_bytes(datos_md)
        retiros = carpeta / "retiros.json"
        informe = refuerzo.verificar(carpeta / "original.pdf", carpeta / "documento.md",
                                     retiros if retiros.is_file() else None)
        escribir_json(carpeta / "informe.json", informe)
    return informe


def segmentos_md(texto_md: str):
    """Parte el .md por sus anclas sin perder un carácter:
    preámbulo + Σ(cabecera + cuerpo) == texto_md. Solo sirve para mostrar y editar."""
    marcas = list(gate.ANCLA.finditer(texto_md))
    if not marcas:
        return texto_md, []
    segmentos = []
    for j, m in enumerate(marcas):
        fin = marcas[j + 1].start() if j + 1 < len(marcas) else len(texto_md)
        segmentos.append({"ancla": int(m.group(1)), "cabecera": m.group(0),
                          "cuerpo": texto_md[m.end():fin]})
    return texto_md[:marcas[0].start()], segmentos


def politica_docx(informe: dict, segmentos: list):
    """(permitido, causa). Obedece al veredicto del gate; no lo recalcula."""
    if informe["veredicto"] == gate.ROJO:
        return False, "Exportación .docx bloqueada: el gate dio 🔴 (pérdida o alteración)."
    if not segmentos:
        return False, ("Exportación .docx bloqueada: el .md no tiene anclas [[p. N]] "
                       "y no se puede conservar la paginación.")
    if informe["veredicto"] == gate.AMARILLO:
        return True, "El .docx llevará el encabezado «PÁGINAS NO VERIFICADAS»."
    return True, ""


def vista(carpeta: Path) -> dict:
    """Lo que pinta la UI: el informe del gate tal cual + el .md partido por anclas."""
    with CANDADO:
        meta = leer_json(carpeta / "meta.json")
        informe = leer_json(carpeta / "informe.json")
        preambulo, segmentos = segmentos_md(leer_md(carpeta))
        retiros = leer_retiros(carpeta)
        transcripciones = leer_transcripciones(carpeta)
    permitido, causa = politica_docx(informe, segmentos)
    return {"id": carpeta.name, "nombre_pdf": meta["nombre_pdf"], "origen_md": meta["origen_md"],
            "informe": informe, "preambulo": preambulo, "segmentos": segmentos, "retiros": retiros,
            "transcripciones": transcripciones, "docx": {"permitido": permitido, "causa": causa}}


# ───────────────────────── validación de entradas ─────────────────────────

def parametro(consulta: dict, clave: str, defecto: str) -> str:
    return consulta.get(clave, [defecto])[0]


def nombre_seguro(nombre: str, defecto: str) -> str:
    """Nombre de archivo sin rutas ni caracteres de control (solo para mostrar)."""
    nombre = Path(nombre.replace("\\", "/")).name
    nombre = re.sub(r"[\x00-\x1f\x7f]", "", nombre).strip()[:200]
    return nombre or defecto


def nombre_descarga(nombre_pdf: str, sufijo: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(nombre_pdf).stem).strip("._")[:80]
    return (base or "documento") + sufijo


def json_de(cuerpo: bytes) -> dict:
    try:
        datos = json.loads(cuerpo.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ErrorBanco(400, "Cuerpo JSON inválido") from None
    if not isinstance(datos, dict):
        raise ErrorBanco(400, "Se esperaba un objeto JSON")
    return datos


def a_utf8(texto: str) -> bytes:
    try:
        return texto.encode("utf-8")
    except UnicodeEncodeError:
        raise ErrorBanco(400, "El texto trae caracteres inválidos (sustitutos UTF-16 sueltos)") from None


def host_permitido(host: str) -> bool:
    """Solo localhost o 127.0.0.1, con o sin puerto: frena el DNS rebinding."""
    nombre = host.strip().lower()
    if not re.fullmatch(r"[a-z0-9.]+(:\d{1,5})?", nombre):
        return False
    return nombre.split(":")[0] in HOSTS_PERMITIDOS


def token_valido(recibido: str) -> bool:
    return hmac.compare_digest(recibido.encode("utf-8"), TOKEN.encode("utf-8"))


# ───────────────────────── acciones ─────────────────────────

def subir_pdf(cuerpo: bytes, consulta: dict) -> dict:
    if not cuerpo.startswith(b"%PDF-"):
        raise ErrorBanco(400, "El archivo no es un PDF: no empieza con %PDF-")
    convertidor = parametro(consulta, "convertidor", CONVERTIDOR_DEFECTO)
    if convertidor not in CONVERTIDORES:
        raise ErrorBanco(400, f"Convertidor desconocido: {convertidor}")
    nombre = nombre_seguro(parametro(consulta, "nombre", ""), "documento.pdf")
    carpeta = TRABAJO / secrets.token_hex(16)
    carpeta.mkdir(parents=True)
    try:
        (carpeta / "original.pdf").write_bytes(cuerpo)
        with CANDADO:
            retiros = CONVERTIDORES[convertidor](carpeta / "original.pdf", carpeta / "documento.md")
            guardar_retiros(carpeta, retiros)
        escribir_json(carpeta / "meta.json", {"nombre_pdf": nombre, "convertidor": convertidor,
                                              "origen_md": f"convertidor «{convertidor}»"})
        guardar_y_verificar(carpeta)
    except pdfium.PdfiumError as e:
        shutil.rmtree(carpeta, ignore_errors=True)
        raise ErrorBanco(400, f"PDF ilegible: {e}") from None
    except Exception:
        shutil.rmtree(carpeta, ignore_errors=True)
        raise
    return vista(carpeta)


def reverificar(carpeta: Path, cuerpo: bytes) -> dict:
    """El .md completo, editado en la UI, vuelve a pasar por el gate."""
    md = json_de(cuerpo).get("md")
    if not isinstance(md, str):
        raise ErrorBanco(400, "Falta el campo «md» con el texto completo")
    datos_md = a_utf8(md)
    with CANDADO:
        meta = leer_json(carpeta / "meta.json")
        cambio = datos_md != (carpeta / "documento.md").read_bytes()
        if cambio and "editado" not in meta["origen_md"]:
            meta["origen_md"] += " · editado en el banco"
            escribir_json(carpeta / "meta.json", meta)
        guardar_y_verificar(carpeta, datos_md)
    return vista(carpeta)


def subir_md_externo(carpeta: Path, cuerpo: bytes, consulta: dict) -> dict:
    """Un .md de otro convertidor (Docling u otro) se verifica contra el mismo PDF."""
    if cuerpo.startswith(b"%PDF-"):
        raise ErrorBanco(400, "Eso es un PDF, no un .md")
    try:
        cuerpo.decode("utf-8")
    except UnicodeDecodeError:
        raise ErrorBanco(400, "El .md debe estar en UTF-8") from None
    nombre = nombre_seguro(parametro(consulta, "nombre", ""), "externo.md")
    with CANDADO:
        meta = leer_json(carpeta / "meta.json")
        meta["origen_md"] = f"externo «{nombre}» (sin retiros declarados)"
        escribir_json(carpeta / "meta.json", meta)
        guardar_retiros(carpeta, [])          # los retiros eran del convertidor, no de este .md
        guardar_y_verificar(carpeta, cuerpo)  # bytes tal cual: el banco no toca el .md
    return vista(carpeta)


def imagen_pagina(carpeta: Path, archivo: str) -> bytes:
    """Página renderizada en el servidor con pypdfium2, como PNG (con caché en disco)."""
    m = RE_IMAGEN.fullmatch(archivo)
    if not m:
        raise ErrorBanco(400, "Página inválida: se espera <número>.png")
    n = int(m.group(1))
    cache = carpeta / "paginas" / f"{n}.png"
    if cache.is_file():
        return cache.read_bytes()
    with CANDADO:
        pdf = pdfium.PdfDocument(str(carpeta / "original.pdf"))
        try:
            if not 1 <= n <= len(pdf):
                raise ErrorBanco(404, f"El PDF no tiene página {n}")
            pagina = pdf[n - 1]
            try:
                buffer = io.BytesIO()
                pagina.render(scale=ESCALA_IMAGEN).to_pil().save(buffer, format="PNG")
            finally:
                pagina.close()
        finally:
            pdf.close()
    cache.parent.mkdir(exist_ok=True)
    cache.write_bytes(buffer.getvalue())
    return buffer.getvalue()


# ───────────────────────── transcripción asistida (Claude visión, NO verificada) ─────────────────────────
# trabajo/<id>/transcripciones/<n>.json. Nunca entra al .md ni cambia el veredicto del gate.

def leer_transcripciones(carpeta: Path) -> dict:
    destino = carpeta / "transcripciones"
    if not destino.is_dir():
        return {}
    return {ruta.stem: leer_json(ruta) for ruta in sorted(destino.glob("*.json"), key=lambda r: int(r.stem))}


def imagen_para_transcribir(carpeta: Path, n: int) -> bytes:
    """La página entera como PNG, con su lado mayor a transcripcion.LADO_MAYOR_PX (letra pequeña legible)."""
    with CANDADO:
        pdf = pdfium.PdfDocument(str(carpeta / "original.pdf"))
        try:
            if not 1 <= n <= len(pdf):
                raise ErrorBanco(404, f"El PDF no tiene página {n}")
            pagina = pdf[n - 1]
            try:
                escala = transcripcion.LADO_MAYOR_PX / max(pagina.get_width(), pagina.get_height())
                buffer = io.BytesIO()
                pagina.render(scale=escala).to_pil().save(buffer, format="PNG")
            finally:
                pagina.close()
        finally:
            pdf.close()
    return buffer.getvalue()


def transcribir_pagina(carpeta: Path, texto_n: str, cuerpo: bytes) -> dict:
    disponible, causa = estado_tokens()
    if not disponible:
        raise ErrorBanco(412, causa)  # se rechaza antes de tocar la red
    if not texto_n.isdigit():
        raise ErrorBanco(400, "Página inválida: se espera un número")
    datos = json_de(cuerpo)
    if datos.get("confirmado") is not True:
        raise ErrorBanco(400, "Falta confirmar que la página se enviará a la API de Anthropic y que tiene costo")
    modelo = datos.get("modelo") or transcripcion.MODELO_DEFECTO
    if not isinstance(modelo, str) or not RE_MODELO.fullmatch(modelo):
        raise ErrorBanco(400, "Nombre de modelo inválido")
    n = int(texto_n)
    paginas = leer_json(carpeta / "informe.json")["paginas"]
    png = imagen_para_transcribir(carpeta, n)
    clave = os.environ["ANTHROPIC_API_KEY"]
    try:
        resultado = transcripcion.transcribir(base64.standard_b64encode(png).decode("ascii"), n, paginas, modelo, clave)
    except transcripcion.ErrorTranscripcion as e:
        raise ErrorBanco(e.codigo, ocultar(e.mensaje, clave)) from None
    registro = {"pagina": n, "fecha_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "sha256_imagen": hashlib.sha256(png).hexdigest(), **resultado}
    destino = carpeta / "transcripciones"
    destino.mkdir(exist_ok=True)
    escribir_json(destino / f"{n}.json", registro)
    return registro


# ───────────────────────── .docx de lectura humana ─────────────────────────

def para_docx(texto: str) -> str:
    """Quita lo que XML no admite (p. ej. \\x02 de PDFium). Solo en el .docx; el .md no se toca."""
    return CARACTERES_NO_XML.sub("", gate.sin_sustitutos(texto))


def rangos(numeros: list) -> str:
    """[3, 7, 8, 9] → "3, 7–9"."""
    partes, i = [], 0
    while i < len(numeros):
        j = i
        while j + 1 < len(numeros) and numeros[j + 1] == numeros[j] + 1:
            j += 1
        partes.append(str(numeros[i]) if i == j else f"{numeros[i]}–{numeros[j]}")
        i = j + 1
    return ", ".join(partes)


def agregar_lineas(doc, texto: str) -> None:
    """Una línea del .md = un párrafo; '# Título' = encabezado."""
    for linea in para_docx(texto).splitlines():
        if not linea.strip():
            continue
        titulo = re.match(r"(#{1,6})\s+(.+)", linea)
        if titulo:
            doc.add_heading(titulo.group(2).strip(), level=min(len(titulo.group(1)), 4))
        else:
            doc.add_paragraph(linea.rstrip())


def construir_docx(informe: dict, nombre_pdf: str, preambulo: str, segmentos: list,
                   transcripciones: dict = None) -> bytes:
    transcripciones = transcripciones or {}
    doc = Document()
    marca = doc.styles.add_style(ESTILO_MARCA, WD_STYLE_TYPE.PARAGRAPH)
    marca.base_style = doc.styles["Normal"]
    marca.font.bold = True
    marca.font.size = Pt(9)
    marca.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    asistida = doc.styles.add_style(ESTILO_TRANSCRIPCION, WD_STYLE_TYPE.PARAGRAPH)
    asistida.base_style = doc.styles["Normal"]
    asistida.font.italic = True
    asistida.font.color.rgb = RGBColor(0x8A, 0x5D, 0x00)

    sha = informe["sha256_pdf"]
    propiedades = doc.core_properties
    propiedades.title = para_docx(nombre_pdf)
    propiedades.subject = "Lectura humana generada por pdf2ctx · banco"
    propiedades.identifier = "sha256:" + sha
    propiedades.keywords = "pdf2ctx; sha256_pdf=" + sha
    propiedades.comments = (f"sha256 del PDF original: {sha} · gate {informe['version']} · "
                            f"veredicto {informe['veredicto']} (código {informe['codigo_salida']})")

    if informe["veredicto"] == gate.AMARILLO:
        pendientes = [d["pagina"] for d in informe["detalle"] if d["veredicto"] == gate.AMARILLO]
        corrida = doc.sections[0].header.paragraphs[0].add_run(
            "PÁGINAS NO VERIFICADAS: " + rangos(pendientes))
        corrida.bold = True
        corrida.font.color.rgb = RGBColor(0xB0, 0x00, 0x20)

    por_pagina = {}
    for s in segmentos:
        por_pagina.setdefault(s["ancla"], []).append(s["cuerpo"])

    hay_preambulo = bool(preambulo.strip())
    if hay_preambulo:
        doc.add_paragraph().add_run("Texto antes de [[p. 1]] (no atribuible a una página):").italic = True
        agregar_lineas(doc, preambulo)
    giradas = {d["pagina"]: d["rotacion"] for d in informe["detalle"] if d.get("rotacion")}
    for n in range(1, informe["paginas"] + 1):
        parrafo = doc.add_paragraph(f"p. {n}", style=ESTILO_MARCA)
        parrafo.paragraph_format.page_break_before = n > 1 or hay_preambulo
        if n in giradas:
            doc.add_paragraph().add_run(f"Página girada {giradas[n]}° en el original.").italic = True
        agregar_lineas(doc, "".join(por_pagina.get(n, [])))
        t = transcripciones.get(str(n))
        if t and t.get("texto"):
            doc.add_paragraph(f"Transcripción asistida por IA ({t['modelo_respuesta']}) del texto en imágenes — "
                              "NO VERIFICADA contra el PDF:", style=ESTILO_TRANSCRIPCION).runs[0].bold = True
            for linea in para_docx(t["texto"]).splitlines():
                if linea.strip():
                    doc.add_paragraph(linea.rstrip(), style=ESTILO_TRANSCRIPCION)

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def exportar_docx(carpeta: Path):
    with CANDADO:
        informe = guardar_y_verificar(carpeta)  # veredicto fresco del gate sobre el .md guardado
        preambulo, segmentos = segmentos_md(leer_md(carpeta))
        meta = leer_json(carpeta / "meta.json")
        transcripciones = leer_transcripciones(carpeta)
    permitido, causa = politica_docx(informe, segmentos)
    if not permitido:
        raise ErrorBanco(409, causa)
    datos = construir_docx(informe, meta["nombre_pdf"], preambulo, segmentos, transcripciones)
    return datos, nombre_descarga(meta["nombre_pdf"], ".docx")


# ───────────────────────── medir tokens (count_tokens) ─────────────────────────

def llave_desde_llavero(servicio: str):
    """(llave | None, causa). Lee el llavero de macOS con `security`, sin shell y sin mostrar la llave."""
    if sys.platform != "darwin":
        return None, "el llavero solo existe en macOS; define ANTHROPIC_API_KEY en el entorno"
    try:
        resultado = subprocess.run(["security", "find-generic-password", "-s", servicio, "-w"],
                                   capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"no se pudo consultar el llavero ({type(e).__name__})"
    llave = resultado.stdout.strip()
    if resultado.returncode != 0 or not llave:
        return None, f"no hay un elemento «{servicio}» en el llavero"
    return llave, ""


def estado_tokens():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        causa = "Deshabilitado: el servidor no tiene ANTHROPIC_API_KEY en su entorno."
        return False, causa + (f" Llavero: {CAUSA_LLAVERO}." if CAUSA_LLAVERO else "")
    return True, ""


def ocultar(texto: str, clave: str) -> str:
    return texto.replace(clave, "***") if clave else texto


def medir(carpeta: Path, cuerpo: bytes) -> dict:
    """PDF nativo vs .md con medir_tokens.contar. Sin heurísticas: solo cifras de la API."""
    disponible, causa = estado_tokens()
    if not disponible:
        raise ErrorBanco(412, causa)  # se rechaza antes de tocar la red
    datos = json_de(cuerpo)
    if datos.get("confirmado") is not True:
        raise ErrorBanco(400, "Falta confirmar que el PDF se enviará a la API de Anthropic")
    modelo = datos.get("modelo") or medir_tokens.MODELO_DEFECTO
    if not isinstance(modelo, str) or not RE_MODELO.fullmatch(modelo):
        raise ErrorBanco(400, "Nombre de modelo inválido")

    pdf = (carpeta / "original.pdf").read_bytes()
    if 4 * ((len(pdf) + 2) // 3) > medir_tokens.LIMITE_BYTES:
        raise ErrorBanco(413, "NO MEDIDO: el PDF excede 32 MB en base64; divídelo")
    with CANDADO:
        texto_md = leer_md(carpeta)
        informe = leer_json(carpeta / "informe.json")

    clave = os.environ["ANTHROPIC_API_KEY"]
    prompt = {"type": "text", "text": medir_tokens.PROMPT}
    documento = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                 "data": base64.standard_b64encode(pdf).decode("ascii")}}
    respuestas = carpeta / "tokens"
    try:
        base, rid = medir_tokens.contar(modelo, [prompt], clave)
        medir_tokens.guardar(respuestas, f"_base.{modelo}", modelo, base, rid)
        resp_pdf, rid = medir_tokens.contar(modelo, [documento, prompt], clave)
        medir_tokens.guardar(respuestas, f"pdf.{modelo}", modelo, resp_pdf, rid)
        resp_md, rid = medir_tokens.contar(modelo, [{"type": "text", "text": texto_md}, prompt], clave)
        medir_tokens.guardar(respuestas, f"md.{modelo}", modelo, resp_md, rid)
        t_pdf = resp_pdf["input_tokens"] - base["input_tokens"]
        t_md = resp_md["input_tokens"] - base["input_tokens"]
    except (RuntimeError, KeyError, TypeError) as e:
        raise ErrorBanco(502, "NO MEDIDO: " + ocultar(str(e), clave)) from None

    sin_texto = informe["clases_oraculo"].get("sin_texto", 0)
    return {"modelo": modelo, "paginas": informe["paginas"],
            "estado": "NO COMPARABLE" if sin_texto else "MEDIDO",
            "nota": (f"{sin_texto} página(s) sin capa de texto: el .md no tiene su contenido (falta OCR)"
                     if sin_texto else ""),
            "tokens_pdf_nativo": t_pdf, "tokens_md": t_md,
            "ahorro_pct": round(100 * (1 - t_md / t_pdf), 1) if t_pdf > 0 else None,
            "respuestas": str(respuestas)}


def configuracion() -> dict:
    disponible, causa = estado_tokens()
    return {"version_gate": f"{refuerzo.VERSION} · kit {gate.VERSION}", "convertidores": list(CONVERTIDORES),
            "tokens": {"disponible": disponible, "causa": causa,
                       "modelo_defecto": medir_tokens.MODELO_DEFECTO},
            "transcripcion": {"disponible": disponible, "causa": causa,
                              "modelo_defecto": transcripcion.MODELO_DEFECTO, "esfuerzo": transcripcion.ESFUERZO},
            "tope_pdf_mb": TOPE_PDF // 2 ** 20, "tope_md_mb": TOPE_TEXTO // 2 ** 20}


def pagina_html():
    nonce = secrets.token_urlsafe(16)
    html = (CARPETA_BANCO / "banco.html").read_text(encoding="utf-8")
    html = html.replace("__TOKEN_BANCO__", TOKEN).replace("__NONCE__", nonce)
    csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
           "img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
           "frame-ancestors 'none'")
    return html.encode("utf-8"), csp


# ───────────────────────── HTTP ─────────────────────────

class Manejador(BaseHTTPRequestHandler):
    server_version = "pdf2ctx-banco"
    sys_version = ""

    def do_GET(self):
        self.atender()

    def do_POST(self):
        self.atender()

    def log_message(self, formato, *args):
        # Solo línea de petición y código. Nunca cabeceras ni cuerpos (ahí viaja el token).
        sys.stderr.write("[banco] " + (formato % args) + "\n")

    def atender(self):
        self.cuerpo_leido = False
        try:
            if not host_permitido(self.headers.get("Host", "")):
                raise ErrorBanco(403, "Host no permitido: usa http://127.0.0.1 o http://localhost")
            if self.command == "POST" and not token_valido(self.headers.get("X-Banco-Token", "")):
                raise ErrorBanco(403, "Falta el token del banco o no es válido: recarga la página")
            url = urllib.parse.urlsplit(self.path)
            self.enrutar(url.path.split("/")[1:], urllib.parse.parse_qs(url.query))
        except (BrokenPipeError, ConnectionResetError):
            return
        except ErrorBanco as e:
            self.descartar_cuerpo()
            self.responder_json(e.codigo, {"error": e.mensaje})
        except Exception as e:
            traceback.print_exc()
            self.descartar_cuerpo()
            self.responder_json(500, {"error": f"Error interno ({type(e).__name__}): revisa la consola del servidor"})

    def enrutar(self, partes: list, consulta: dict):
        metodo = self.command
        if metodo == "GET" and partes in ([""], ["index.html"]):
            html, csp = pagina_html()
            return self.responder(200, "text/html; charset=utf-8", html, {"Content-Security-Policy": csp})
        if metodo == "GET" and partes == ["api", "config"]:
            return self.responder_json(200, configuracion())
        if metodo == "POST" and partes == ["api", "subir"]:
            return self.responder_json(200, subir_pdf(self.leer_cuerpo(TOPE_PDF), consulta))

        if len(partes) >= 3 and partes[:2] == ["api", "doc"]:
            carpeta = carpeta_de(partes[2])  # 400 si el id no es hex; 404 si no existe
            accion = partes[3:]
            if metodo == "GET" and accion == []:
                return self.responder_json(200, vista(carpeta))
            if metodo == "GET" and len(accion) == 2 and accion[0] == "pagina":
                return self.responder(200, "image/png", imagen_pagina(carpeta, accion[1]),
                                      {"Cache-Control": "private, max-age=86400"})
            if metodo == "GET" and accion == ["descargar", "md"]:
                meta = leer_json(carpeta / "meta.json")
                return self.descargar((carpeta / "documento.md").read_bytes(), "text/markdown; charset=utf-8",
                                      nombre_descarga(meta["nombre_pdf"], ".md"))
            if metodo == "GET" and accion == ["descargar", "informe"]:
                meta = leer_json(carpeta / "meta.json")
                return self.descargar((carpeta / "informe.json").read_bytes(), "application/json; charset=utf-8",
                                      nombre_descarga(meta["nombre_pdf"], ".informe.json"))
            if metodo == "GET" and accion == ["descargar", "docx"]:
                datos, nombre = exportar_docx(carpeta)
                return self.descargar(datos, "application/vnd.openxmlformats-officedocument."
                                             "wordprocessingml.document", nombre)
            if metodo == "POST" and accion == ["verificar"]:
                return self.responder_json(200, reverificar(carpeta, self.leer_cuerpo(TOPE_TEXTO)))
            if metodo == "POST" and accion == ["md"]:
                return self.responder_json(200, subir_md_externo(carpeta, self.leer_cuerpo(TOPE_TEXTO), consulta))
            if metodo == "POST" and accion == ["tokens"]:
                return self.responder_json(200, medir(carpeta, self.leer_cuerpo(TOPE_TEXTO)))
            if metodo == "POST" and len(accion) == 2 and accion[0] == "transcribir":
                return self.responder_json(200, transcribir_pagina(carpeta, accion[1], self.leer_cuerpo(TOPE_TEXTO)))
        raise ErrorBanco(404, "Ruta inexistente")

    def leer_cuerpo(self, tope: int) -> bytes:
        largo = self.headers.get("Content-Length", "")
        if not largo.isdigit():
            raise ErrorBanco(411, "Falta Content-Length")
        if int(largo) > tope:
            raise ErrorBanco(413, f"El cuerpo excede el tope de {tope // 2 ** 20} MB")
        self.cuerpo_leido = True
        return self.rfile.read(int(largo))

    def descartar_cuerpo(self):
        """Lee y tira un cuerpo pequeño sin leer, para que el cliente alcance a recibir el error."""
        largo = self.headers.get("Content-Length", "")
        if self.command == "POST" and not self.cuerpo_leido and largo.isdigit() and int(largo) <= TOPE_DESCARTE:
            self.cuerpo_leido = True
            self.rfile.read(int(largo))

    def responder(self, codigo: int, tipo: str, cuerpo: bytes, extra: dict = None):
        cabeceras = {"Content-Type": tipo, "Content-Length": str(len(cuerpo)), "Cache-Control": "no-store",
                     "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                     "Cross-Origin-Resource-Policy": "same-origin", "X-Frame-Options": "DENY"}
        cabeceras.update(extra or {})
        self.send_response(codigo)
        for clave, valor in cabeceras.items():
            self.send_header(clave, valor)
        self.end_headers()
        self.wfile.write(cuerpo)

    def responder_json(self, codigo: int, datos: dict):
        self.responder(codigo, "application/json; charset=utf-8",
                       json.dumps(datos, ensure_ascii=False).encode("utf-8"))

    def descargar(self, datos: bytes, tipo: str, nombre: str):
        self.responder(200, tipo, datos, {"Content-Disposition": f'attachment; filename="{nombre}"'})


def main() -> int:
    global TRABAJO, ORIGEN_LLAVE, CAUSA_LLAVERO
    for flujo in (sys.stdout, sys.stderr):
        if hasattr(flujo, "reconfigure"):
            flujo.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Banco de pruebas local de pdf2ctx (solo 127.0.0.1).")
    ap.add_argument("--puerto", type=int, default=PUERTO_DEFECTO, help=f"Por defecto {PUERTO_DEFECTO}")
    ap.add_argument("--trabajo", default=str(TRABAJO), help="Carpeta de trabajo (por defecto banco/trabajo)")
    ap.add_argument("--llavero", action="store_true",
                    help="Si ANTHROPIC_API_KEY no está en el entorno, la toma del llavero de macOS")
    ap.add_argument("--servicio-llavero", default=SERVICIO_LLAVERO,
                    help=f"Nombre del elemento en el llavero (por defecto {SERVICIO_LLAVERO})")
    args = ap.parse_args()
    if not 1 <= args.puerto <= 65535:
        ap.error("--puerto debe estar entre 1 y 65535")
    if args.llavero and not os.environ.get("ANTHROPIC_API_KEY"):
        llave, CAUSA_LLAVERO = llave_desde_llavero(args.servicio_llavero)
        if llave:
            os.environ["ANTHROPIC_API_KEY"] = llave   # solo en la memoria de este proceso
            ORIGEN_LLAVE = f"llavero «{args.servicio_llavero}»"

    TRABAJO = Path(args.trabajo).resolve()
    TRABAJO.mkdir(parents=True, exist_ok=True)
    try:
        servidor = ThreadingHTTPServer(("127.0.0.1", args.puerto), Manejador)
    except OSError as e:
        print(f"No se pudo escuchar en 127.0.0.1:{args.puerto}: {e}")
        return 2
    servidor.daemon_threads = True

    disponible, causa = estado_tokens()
    print(f"Banco pdf2ctx en http://127.0.0.1:{args.puerto}/  (solo esta máquina)")
    print(f"Trabajo: {TRABAJO}")
    print("Medir tokens: " + (f"disponible (llave del {ORIGEN_LLAVE}); pide confirmación en cada medición"
                              if disponible else causa))
    print("Ctrl+C para detener.", flush=True)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        servidor.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
