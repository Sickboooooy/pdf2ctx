#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gate_reforzado.py — Refuerzos al gate de fidelidad del kit (pdf2ctx)

NO es parte del kit: lo importa y nunca lo edita. Corre gate_fidelidad.verificar() y le añade
comprobaciones que solo pueden ENDURECER su veredicto (🟢→🟡/🔴, 🟡→🔴); nunca lo relajan.
El veredicto del kit queda en el informe: bloque «kit» y «veredicto_kit» en cada página.

Refuerzo · vulnerabilidad (ver defectos/INFORME_COBERTURA.md) · qué hace
  R1  V1  texto no visible   Un carácter está oculto si se dibuja en modo invisible sin imagen debajo,
                             no deja tinta en la página renderizada, es microtexto (< 2 pt) o cae fuera
                             de la página. Si el .md contiene una corrida oculta (≥ 6 letras) → 🔴.
                             El convertidor «visible» la retira y la declara como retiro (G5 del kit).
  R2  V2  cifras permutadas  Cada cifra conserva las dos palabras que la preceden en la página. Detecta
                             intercambios que el G2 del kit no ve, porque el G2 solo cuenta cifras.
  R4  V4  archivo dañado     %%EOF, startxref y flujos comprimidos. Si el archivo está dañado, el oráculo
                             (el mismo archivo) tampoco es confiable: toda página 🟢 pasa a 🟡.
  R5  V5  folios             Folios impresos «N de M»: saltos y repetidos → 🟡 en esas páginas.
  R6  V6  orientación        El /Rotate de cada página queda en el informe (aviso).
  R7  V7  anotaciones        Notas, sellos, firmas… (no vínculos) → 🟡 con tipo, rectángulo y contenido.
  R8  V8  imágenes           Imagen ≥ 3 % del área en una página con texto → 🟡: su contenido no está en el .md.
  R3  V3  tabla por columnas Una malla de texto dibujada por columnas → 🟡: la lectura lineal pierde la
                             asociación renglón–columna aunque sea fiel a la capa.
  R9  V9  OCR por cotejar    En páginas con capa OCR previa, lista las cifras concretas por cotejar contra
                             la imagen (localiza qué dato revisar; leer la imagen para corregir el valor
                             exige OCR y queda fuera).
  R10 V10 páginas giradas    El oráculo se calcula sobre una copia sin /Rotate: PDFium altera el texto
                             que extrae de páginas giradas.

Pendientes: leer la imagen para corregir automáticamente el valor que discrepa en V9 (requiere un motor OCR).

Uso
  python gate_reforzado.py visible   doc.pdf -o doc.md [--retiros doc.retiros.json]
  python gate_reforzado.py verificar doc.pdf doc.md [--retiros doc.retiros.json] [--informe doc.informe.json]
"""
import argparse
import ctypes
import json
import re
import sys
import tempfile
import zlib
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

import gate_fidelidad as gate
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

VERSION = "0.2.0-refuerzo"
VERDE, AMARILLO, ROJO = gate.VERDE, gate.AMARILLO, gate.ROJO
NIVEL = {VERDE: 0, AMARILLO: 1, ROJO: 2}
POR_NIVEL = {0: VERDE, 1: AMARILLO, 2: ROJO}

ESCALA_TINTA = 2.0       # 144 dpi; a 72 dpi los puntos y guiones de 1 px dan falsos «sin tinta»
UMBRAL_TINTA = 245       # un píxel más oscuro que esto es tinta (papel blanco = 255)
MICROTEXTO_PT = 2.0
CORRIDA_MIN = 6          # letras o dígitos mínimos para tratar una corrida como texto oculto
IMAGEN_MIN = 0.03        # fracción del área: el escudo de la ley ocupa 1.07 % en cada página
TOPE_FLUJO = 256 * 1024 * 1024

# Tabla dibujada por columnas (R3): malla de filas × columnas alineadas cuyo orden de
# dibujo es por columnas, lo que rompe la asociación renglón–columna en la lectura lineal.
TABLA_FILAS_MIN = 3
TABLA_COLS_MIN = 2
TABLA_TOL_FILA = 14.0    # pt: dos centros con esta separación vertical se agrupan en la misma fila
TABLA_TOL_COL = 16.0     # pt: agrupación de columnas por x

RE_TOKEN = re.compile(r"\d+(?:[.,:/-]\d+)*|[a-z]+")
RE_FOLIO = re.compile(r"^\s*(?:p[aá]gina\s+)?(\d{1,4})\s+de\s+(\d{1,4})\s*$", re.IGNORECASE | re.MULTILINE)
WCHAR = getattr(pdfium_c, "FPDF_WCHAR", ctypes.c_ushort)
ANOTACIONES_IGNORADAS = {pdfium_c.FPDF_ANNOT_LINK, pdfium_c.FPDF_ANNOT_POPUP}
NOMBRE_ANOTACION = {pdfium_c.FPDF_ANNOT_TEXT: "nota", pdfium_c.FPDF_ANNOT_FREETEXT: "texto libre",
                    pdfium_c.FPDF_ANNOT_STAMP: "sello", pdfium_c.FPDF_ANNOT_INK: "trazo a mano",
                    pdfium_c.FPDF_ANNOT_HIGHLIGHT: "resaltado", pdfium_c.FPDF_ANNOT_WIDGET: "campo de formulario"}


# ───────────────────────── análisis de cada página (siempre sin giro) ─────────────────────────

def _cadena_anotacion(anotacion, clave: bytes) -> str:
    largo = pdfium_c.FPDFAnnot_GetStringValue(anotacion, clave, None, 0)
    if largo <= 2:
        return ""
    buffer = ctypes.create_string_buffer(largo)
    pdfium_c.FPDFAnnot_GetStringValue(anotacion, clave, ctypes.cast(buffer, ctypes.POINTER(WCHAR)), largo)
    return buffer.raw[:largo].decode("utf-16-le", "replace").rstrip("\x00")


def _anotaciones(pagina) -> list:
    lista = []
    for k in range(pdfium_c.FPDFPage_GetAnnotCount(pagina.raw)):
        anotacion = pdfium_c.FPDFPage_GetAnnot(pagina.raw, k)
        try:
            tipo = pdfium_c.FPDFAnnot_GetSubtype(anotacion)
            if tipo in ANOTACIONES_IGNORADAS:
                continue
            r = pdfium_c.FS_RECTF()
            pdfium_c.FPDFAnnot_GetRect(anotacion, ctypes.byref(r))
            lista.append({"tipo": NOMBRE_ANOTACION.get(tipo, f"tipo {tipo}"),
                          "rect": [round(min(r.left, r.right)), round(min(r.top, r.bottom)),
                                   round(max(r.left, r.right)), round(max(r.top, r.bottom))],
                          "contenido": _cadena_anotacion(anotacion, b"Contents")})
        finally:
            pdfium_c.FPDFPage_CloseAnnot(anotacion)
    return lista


def _dentro_de(caja, rectangulos) -> bool:
    cx, cy = (caja[0] + caja[2]) / 2, (caja[1] + caja[3]) / 2
    return any(izq <= cx <= der and abajo <= cy <= arriba for izq, abajo, der, arriba in rectangulos)


def _modo_invisible(capa, i, cache) -> bool:
    if not hasattr(pdfium_c, "FPDFText_GetTextObject"):
        return False
    objeto = pdfium_c.FPDFText_GetTextObject(capa.raw, i)
    if not objeto:
        return False
    clave = ctypes.cast(objeto, ctypes.c_void_p).value
    if clave not in cache:
        cache[clave] = pdfium_c.FPDFTextObj_GetTextRenderMode(objeto) == pdfium_c.FPDF_TEXTRENDERMODE_INVISIBLE
    return cache[clave]


def _motivo_oculto(capa, i, gris, ancho, alto, imagenes, cache):
    """Por qué el carácter i no se ve, o None si se ve."""
    izq, abajo, der, arriba = capa.get_charbox(i)
    if der <= 0 or izq >= ancho or arriba <= 0 or abajo >= alto:
        return "fuera de la página"
    if pdfium_c.FPDFText_GetFontSize(capa.raw, i) < MICROTEXTO_PT:
        return "microtexto"
    if _modo_invisible(capa, i, cache):
        if _dentro_de((izq, abajo, der, arriba), imagenes):
            return None   # capa OCR sobre la imagen: lo visible es la imagen (el kit la trata como oráculo débil)
        return "modo de render invisible"
    caja = (max(int(izq * ESCALA_TINTA), 0), max(int((alto - arriba) * ESCALA_TINTA), 0),
            min(int(der * ESCALA_TINTA) + 1, gris.width), min(int((alto - abajo) * ESCALA_TINTA) + 1, gris.height))
    if caja[2] <= caja[0] or caja[3] <= caja[1] or gris.crop(caja).getextrema()[0] >= UMBRAL_TINTA:
        return "sin tinta en la página"
    return None


def _letras(texto: str) -> int:
    return len(re.sub(r"[^a-z0-9]", "", gate.plano(texto)))


# ───────────────────────── tablas dibujadas por columnas (R3) ─────────────────────────

def _agrupar_1d(valores, tol: float) -> list:
    """Agrupa valores 1-D (ordenados) en centros de clúster separados por > tol."""
    valores = sorted(valores)
    grupos = []
    for v in valores:
        if grupos and v - grupos[-1][-1] <= tol:
            grupos[-1].append(v)
        else:
            grupos.append([v])
    return [sum(g) / len(g) for g in grupos]


def _tabla_por_columnas(pagina) -> dict:
    """Detecta una malla de texto dibujada por columnas. Devuelve métricas o None.

    Una tabla dibujada por columnas tiene la capa de texto en orden column-major: el .md
    lineal pierde la asociación renglón–columna aunque sea fiel a la capa. Se detecta cuando
    (a) los objetos de texto forman una malla densa (≥3 filas × ≥2 columnas alineadas) y
    (b) en el orden de dibujo hay una corrida de ≥TABLA_FILAS_MIN objetos consecutivos en la
    misma columna.
    """
    objetos = list(pagina.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_TEXT]))
    if len(objetos) < TABLA_FILAS_MIN * TABLA_COLS_MIN:
        return None
    puntos = []
    for o in objetos:
        izq, abajo, der, arriba = o.get_bounds()
        # columnas por borde izquierdo (los centros varían con el ancho del texto);
        # filas por centro vertical.
        puntos.append((izq, (abajo + arriba) / 2))
    xs = _agrupar_1d([x for x, _ in puntos], TABLA_TOL_COL)
    ys = _agrupar_1d([y for _, y in puntos], TABLA_TOL_FILA)
    if len(xs) < TABLA_COLS_MIN or len(ys) < TABLA_FILAS_MIN:
        return None

    def indice(v, lista):
        return min(range(len(lista)), key=lambda i: abs(lista[i] - v))

    fila_col = [(indice(y, ys), indice(x, xs)) for x, y in puntos]
    cuenta_filas = Counter(f for f, _ in fila_col)
    cuenta_cols = Counter(c for _, c in fila_col)
    malla = [(f, c) for f, c in fila_col if cuenta_filas[f] >= 2 and cuenta_cols[c] >= 2]
    filas = len({f for f, _ in malla})
    columnas = len({c for _, c in malla})
    if filas < TABLA_FILAS_MIN or columnas < TABLA_COLS_MIN:
        return None
    mejor, corrida, anterior = 1, 1, None
    for f, c in malla:
        if anterior is not None and c == anterior[1] and f != anterior[0]:
            corrida += 1
            mejor = max(mejor, corrida)
        else:
            corrida = 1
        anterior = (f, c)
    if mejor < TABLA_FILAS_MIN:
        return None
    return {"filas": filas, "columnas": columnas, "corrida_columna": mejor}


def analizar_pagina(pagina, n: int) -> dict:
    rotacion = pagina.get_rotation()
    if rotacion:
        pagina.set_rotation(0)   # R10: se analiza y se extrae siempre sin giro (solo en memoria)
    ancho, alto = pagina.get_size()
    imagenes = [obj.get_bounds() for obj in pagina.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE])]
    grandes = [{"rect": [round(v) for v in b], "fraccion": round((b[2] - b[0]) * (b[3] - b[1]) / (ancho * alto), 4)}
               for b in imagenes if (b[2] - b[0]) * (b[3] - b[1]) / (ancho * alto) >= IMAGEN_MIN]
    gris = pagina.render(scale=ESCALA_TINTA).to_pil().convert("L")
    capa = pagina.get_textpage()
    try:
        texto = capa.get_text_range()
        corridas, actual, cache = [], None, {}
        if len(texto) == capa.count_chars():   # un carácter por índice (si no, no se analiza)
            for i, caracter in enumerate(texto):
                if not caracter.strip():
                    continue
                motivo = _motivo_oculto(capa, i, gris, ancho, alto, imagenes, cache)
                if motivo:
                    actual = actual or {"inicio": i, "motivos": Counter()}
                    actual["fin"] = i
                    actual["motivos"][motivo] += 1
                elif actual:
                    corridas.append(actual)
                    actual = None
            if actual:
                corridas.append(actual)
    finally:
        capa.close()
    ocultos, visible, cursor = [], [], 0
    for c in corridas:
        fragmento = texto[c["inicio"]:c["fin"] + 1]
        if _letras(fragmento) >= CORRIDA_MIN:
            ocultos.append({"texto": fragmento, "motivo": c["motivos"].most_common(1)[0][0]})
            visible.append(texto[cursor:c["inicio"]])
            cursor = c["fin"] + 1
    visible.append(texto[cursor:])
    return {"n": n, "rotacion": rotacion, "texto": texto, "texto_visible": "".join(visible),
            "ocultos": ocultos, "imagenes_grandes": grandes, "anotaciones": _anotaciones(pagina),
            "tabla_columnas": _tabla_por_columnas(pagina)}


# ───────────────────────── integridad del archivo (R4) ─────────────────────────

def _flujos_danados(datos: bytes) -> list:
    """Objetos cuyo flujo FlateDecode no se descomprime limpio (zlib valida su suma Adler-32)."""
    danados = []
    for m in re.finditer(rb">>\s*stream\r?\n", datos):
        inicio_obj = datos.rfind(b" obj", 0, m.start())
        diccionario = datos[inicio_obj:m.start() + 2]
        if inicio_obj < 0 or b"endobj" in diccionario or not re.search(rb"/Filter\s*\[?\s*/FlateDecode", diccionario):
            continue
        numero = re.search(rb"(\d+)\s+\d+$", datos[max(inicio_obj - 24, 0):inicio_obj])
        largo = re.search(rb"/Length\s+(\d+)(?!\s+\d+\s+R)", diccionario)
        if largo:
            crudo = datos[m.end():m.end() + int(largo.group(1))]
        else:
            fin = datos.find(b"endstream", m.end())
            crudo = (datos[m.end():fin] if fin > 0 else datos[m.end():]).rstrip(b"\r\n")
        descompresor, total = zlib.decompressobj(), 0
        try:
            pendiente = crudo
            while pendiente and total < TOPE_FLUJO:
                total += len(descompresor.decompress(pendiente, 16 * 1024 * 1024))
                pendiente = descompresor.unconsumed_tail
            if total < TOPE_FLUJO and not descompresor.eof:
                danados.append(numero.group(1).decode() if numero else "?")
        except zlib.error:
            danados.append(numero.group(1).decode() if numero else "?")
    return danados


def integridad(ruta_pdf) -> list:
    datos = Path(ruta_pdf).read_bytes()
    problemas, cola = [], datos[-2048:]
    if b"%%EOF" not in cola:
        problemas.append("no termina en %%EOF (archivo truncado)")
    referencias = list(re.finditer(rb"startxref\s+(\d+)", cola))
    if not referencias:
        problemas.append("sin startxref: PDFium tuvo que reconstruir la tabla de referencias")
    else:
        posicion = int(referencias[-1].group(1))
        if not re.match(rb"\s*(xref|\d+\s+\d+\s+obj)", datos[posicion:posicion + 40]):
            problemas.append(f"startxref apunta al byte {posicion}, donde no hay tabla de referencias")
    if re.search(rb"/Encrypt\s", datos):
        return problemas  # cifrado: los flujos no se pueden validar sin la llave
    danados = _flujos_danados(datos)
    if danados:
        problemas.append(f"{len(danados)} flujo(s) comprimido(s) dañado(s) (objetos {', '.join(danados[:8])}"
                         + (" …" if len(danados) > 8 else "") + ")")
    return problemas


@lru_cache(maxsize=8)
def _analizar(ruta: str, huella: str) -> dict:
    pdf = pdfium.PdfDocument(ruta)
    try:
        paginas = []
        for i in range(len(pdf)):
            pagina = pdf[i]
            try:
                paginas.append(analizar_pagina(pagina, i + 1))
            finally:
                pagina.close()
    finally:
        pdf.close()
    return {"paginas": paginas, "integridad": integridad(ruta), "sha256": huella}


def analizar(ruta_pdf) -> dict:
    """Análisis por página y del archivo. Se guarda en memoria por SHA-256: no lo modifiques."""
    return _analizar(str(Path(ruta_pdf).resolve()), gate.sha256(ruta_pdf))


# ───────────────────────── folios impresos (R5) ─────────────────────────

def folios(paginas: list):
    """({página: [hallazgos]}, [avisos]) a partir de folios «N de M». Sin evidencia, nada."""
    vistos = {}
    for p in paginas:
        encontrados = RE_FOLIO.findall(gate.normalizar(p["texto"]))
        if len(encontrados) == 1:
            vistos[p["n"]] = (int(encontrados[0][0]), int(encontrados[0][1]))
    if len(vistos) < max(3, len(paginas) // 2):
        return {}, []
    total = Counter(t for _, t in vistos.values()).most_common(1)[0][0]
    folio = {n: f for n, (f, t) in vistos.items() if t == total}
    paginas_de = defaultdict(list)
    for n, f in folio.items():
        paginas_de[f].append(n)
    hallazgos = defaultdict(list)
    for f, ns in paginas_de.items():
        if len(ns) > 1:
            for n in ns:
                hallazgos[n].append(f"R5 folio «{f} de {total}» repetido en las pp. {', '.join(map(str, ns))}")
    orden = sorted(folio)
    for a, b in zip(orden, orden[1:]):
        fa, fb = folio[a], folio[b]
        if len(paginas_de[fa]) > 1 or len(paginas_de[fb]) > 1 or fb - fa == b - a:
            continue
        esperado = fa + (b - a)
        texto = (f"R5 salto de folio: tras «{fa} de {total}» (p. {a}) sigue «{fb} de {total}» (p. {b}); "
                 + (f"faltan los folios {esperado}–{fb - 1}" if fb - 1 > esperado else f"falta el folio {esperado}")
                 if fb > esperado else f"R5 folio fuera de orden: «{fb} de {total}» (p. {b}) tras «{fa}» (p. {a})")
        hallazgos[a].append(texto)
        hallazgos[b].append(texto)
    avisos = []
    if total != len(paginas):
        avisos.append(f"R5 los folios impresos dicen «de {total}» y el PDF tiene {len(paginas)} páginas")
    return dict(hallazgos), avisos


# ───────────────────────── contexto de las cifras (R2) ─────────────────────────

def _contextos(tokens: list) -> Counter:
    return Counter((tuple(tokens[max(i - 2, 0):i]), t) for i, t in enumerate(tokens) if t[0].isdigit())


def cifras_fuera_de_contexto(oraculo: str, md: str) -> list:
    """Cifras presentes las mismas veces en ambos lados pero precedidas por otras palabras: permutación."""
    tokens_or, tokens_md = RE_TOKEN.findall(oraculo), RE_TOKEN.findall(md)
    cifras_or = Counter(t for t in tokens_or if t[0].isdigit())
    cifras_md = Counter(t for t in tokens_md if t[0].isdigit())
    iguales = {c for c, k in cifras_or.items() if cifras_md.get(c) == k}
    faltan = _contextos(tokens_or) - _contextos(tokens_md)
    return [f"«{cifra}» tras «{' '.join(previas)}»" for (previas, cifra) in faltan if cifra in iguales]


# ───────────────────────── convertidor «visible» ─────────────────────────

def convertir_visible(ruta_pdf, ruta_md) -> list:
    """PDF → .md con anclas, extraído sin giro y sin el texto que no se ve. Devuelve los retiros declarados."""
    partes, retiros = [], []
    for p in analizar(ruta_pdf)["paginas"]:
        texto = gate.sin_sustitutos(p["texto_visible"]).replace("\r\n", "\n").replace("\r", "\n").strip()
        partes.append(f"[[p. {p['n']}]]\n{texto}\n")
        for oculto in p["ocultos"]:
            retiros.append({"pagina": p["n"], "texto": oculto["texto"],
                            "motivo": f"texto no visible en la página ({oculto['motivo']})"})
    Path(ruta_md).write_text("\n".join(partes), encoding="utf-8")
    return retiros


# ───────────────────────── verificación reforzada ─────────────────────────

def _copia_sin_giro(ruta_pdf, analisis: dict, carpeta: Path) -> Path:
    giradas = [p["n"] for p in analisis["paginas"] if p["rotacion"]]
    if not giradas:
        return Path(ruta_pdf)
    pdf = pdfium.PdfDocument(str(ruta_pdf))
    try:
        for n in giradas:
            pdf[n - 1].set_rotation(0)
        destino = carpeta / "oraculo_sin_giro.pdf"
        pdf.save(str(destino))
    finally:
        pdf.close()
    return destino


def verificar(ruta_pdf, ruta_md, ruta_retiros=None, cobertura_min: float = gate.COBERTURA_MIN,
              desactivar=frozenset()) -> dict:
    analisis = analizar(ruta_pdf)
    paginas = analisis["paginas"]
    giradas_para_oraculo = [p["n"] for p in paginas if p["rotacion"]]
    # La copia sin giro (R10) solo es necesaria si hay páginas giradas; en el caso común
    # no se crea ningún directorio temporal.
    if giradas_para_oraculo:
        with tempfile.TemporaryDirectory(prefix="pdf2ctx-") as temporal:
            oraculo = _copia_sin_giro(ruta_pdf, analisis, Path(temporal))
            informe = gate.verificar(oraculo, ruta_md, ruta_retiros, cobertura_min, desactivar)
    else:
        informe = gate.verificar(ruta_pdf, ruta_md, ruta_retiros, cobertura_min, desactivar)
    kit = {k: informe[k] for k in ("gate", "version", "veredicto", "codigo_salida", "resumen")}
    informe["pdf"], informe["sha256_pdf"] = str(ruta_pdf), analisis["sha256"]

    texto_md = Path(ruta_md).read_text(encoding="utf-8")
    paginas_md, _, _ = gate.leer_md(texto_md)
    retiros = gate.cargar_retiros(ruta_retiros)
    extra = defaultdict(list)   # página → [(nivel, hallazgo)]
    avisos = []

    # R4 · integridad del archivo
    if analisis["integridad"]:
        avisos.append("R4 archivo dañado: " + "; ".join(analisis["integridad"]))
        for p in paginas:
            extra[p["n"]].append((1, "R4 NO VERIFICABLE: el archivo está dañado y el oráculo sale del mismo archivo"))

    clases = {d["pagina"]: d["clase"] for d in informe["detalle"]}
    for p in paginas:
        n = p["n"]
        md_pagina = paginas_md.get(n) if paginas_md is not None else None
        # R1 · texto no visible dentro del .md
        md_contiene_oculto = False
        if md_pagina is not None:
            md_plano, visible_plano = gate.plano(md_pagina), gate.plano(p["texto_visible"])
            for oculto in p["ocultos"]:
                fragmento = gate.plano(oculto["texto"])
                if md_plano.count(fragmento) > visible_plano.count(fragmento):
                    md_contiene_oculto = True
                    extra[n].append((2, f"R1 el .md contiene texto que no se ve en la página "
                                        f"({oculto['motivo']}): «{oculto['texto'][:80]}»"))
        # R2 · cifras fuera de su contexto (permutaciones)
        if md_pagina is not None and clases.get(n) == "texto":
            base = gate.plano(p["texto"] if md_contiene_oculto else p["texto_visible"])
            for r in retiros:
                if int(r["pagina"]) == n:
                    base = base.replace(gate.plano(r["texto"]), " ", 1)
            fuera = cifras_fuera_de_contexto(base, gate.plano(md_pagina))
            if fuera:
                extra[n].append((2, f"R2 cifras fuera de su contexto, posible permutación ({len(fuera)}): "
                                    + ", ".join(fuera[:3]) + (" …" if len(fuera) > 3 else "")))
        # R7 · anotaciones
        for a in p["anotaciones"]:
            extra[n].append((1, f"R7 anotación «{a['tipo']}» en {a['rect']} fuera de la capa de texto"
                                + (f": «{a['contenido'][:120]}»" if a["contenido"] else "")))
        # R8 · imágenes grandes en páginas con texto
        if clases.get(n) == "texto":
            for img in p["imagenes_grandes"]:
                extra[n].append((1, f"R8 imagen de {img['fraccion']:.1%} del área en {img['rect']}: "
                                    "su contenido no está en el .md; cotejar contra la página"))
        # R3 · tabla dibujada por columnas
        if p.get("tabla_columnas"):
            t = p["tabla_columnas"]
            extra[n].append((1, f"R3 tabla dibujada por columnas ({t['filas']} filas × {t['columnas']} "
                                "columnas): la lectura lineal puede perder la asociación renglón–columna"))
        # R9 · cifras de la capa OCR por cotejar contra la imagen (localiza qué dato revisar)
        if clases.get(n) == "ocr_previo":
            cifras = [k[1:] for k in gate.invariantes(gate.plano(p["texto"])) if k.startswith("#")]
            if cifras:
                extra[n].append((1, f"R9 capa OCR por cotejar contra la imagen: {', '.join(cifras[:12])}"
                                    + (" …" if len(cifras) > 12 else "")))
            else:
                extra[n].append((1, "R9 capa OCR previa: cotejar contra la imagen"))

    # R1 sin anclas: el .md completo contra todo el texto oculto
    if paginas_md is None:
        md_plano = gate.plano(texto_md)
        for p in paginas:
            for oculto in p["ocultos"]:
                if gate.plano(oculto["texto"]) in md_plano:
                    informe["errores_documento"].append(
                        f"R1 (documento completo) el .md contiene texto que no se ve (p. {p['n']}): «{oculto['texto'][:80]}»")

    # R5 · folios impresos
    hallazgos_folio, avisos_folio = folios(paginas)
    for n, lista in hallazgos_folio.items():
        extra[n] += [(1, h) for h in lista]
    avisos += avisos_folio

    # R6 / R10 · orientación
    giradas = {p["n"]: p["rotacion"] for p in paginas if p["rotacion"]}
    if giradas:
        avisos.append("R6 páginas giradas en el original: " + ", ".join(f"p. {n} ({g}°)" for n, g in giradas.items())
                      + "; R10 el texto y el oráculo se extrajeron sin giro")

    # Aplicar: el veredicto solo sube de severidad, nunca baja.
    for d in informe["detalle"]:
        d["veredicto_kit"] = d["veredicto"]
        if d["pagina"] in giradas:
            d["rotacion"] = giradas[d["pagina"]]
        propios = extra.get(d["pagina"], [])
        if propios:
            d["veredicto"] = POR_NIVEL[max([NIVEL[d["veredicto"]]] + [nivel for nivel, _ in propios])]
            d["hallazgos"] = d["hallazgos"] + [h for _, h in propios]

    detalle = informe["detalle"]
    rojos = sum(d["veredicto"] == ROJO for d in detalle) + (1 if informe["errores_documento"] else 0)
    amarillos = sum(d["veredicto"] == AMARILLO for d in detalle)
    veredicto = ROJO if rojos else AMARILLO if amarillos else VERDE
    if NIVEL[veredicto] < NIVEL[kit["veredicto"]]:   # salvaguarda: nunca relajar
        veredicto = kit["veredicto"]
    informe.update({
        "gate": "pdf2ctx/gate_reforzado", "version": f"{VERSION} (kit {kit['version']})",
        "veredicto": veredicto, "codigo_salida": {VERDE: 0, AMARILLO: 3, ROJO: 1}[veredicto],
        "resumen": {"verde": sum(d["veredicto"] == VERDE for d in detalle), "amarillo": amarillos,
                    "rojo": sum(d["veredicto"] == ROJO for d in detalle)},
        "avisos": informe["avisos"] + avisos, "kit": kit,
        "refuerzos": {"version": VERSION, "integridad": analisis["integridad"],
                      "rotaciones": {str(n): g for n, g in giradas.items()},
                      "oraculo_sin_giro": bool(giradas),
                      "texto_oculto": [{"pagina": p["n"], **o} for p in paginas for o in p["ocultos"]],
                      "anotaciones": [{"pagina": p["n"], **a} for p in paginas for a in p["anotaciones"]],
                      "imagenes_grandes": [{"pagina": p["n"], **i} for p in paginas for i in p["imagenes_grandes"]],
                      "tablas_columnas": [{"pagina": p["n"], **p["tabla_columnas"]}
                                          for p in paginas if p.get("tabla_columnas")],
                      "folios": {"hallazgos": hallazgos_folio, "avisos": avisos_folio}},
    })
    return informe


# ───────────────────────── CLI ─────────────────────────

def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Gate de fidelidad del kit + refuerzos (pdf2ctx).")
    sub = ap.add_subparsers(dest="comando", required=True)
    v = sub.add_parser("visible", help="PDF → .md sin texto oculto, sin giro, con retiros declarados")
    v.add_argument("pdf")
    v.add_argument("-o", "--salida", required=True)
    v.add_argument("--retiros", help="JSON donde declarar el texto retirado")
    r = sub.add_parser("verificar", help="Compara un .md contra el PDF original (kit + refuerzos)")
    r.add_argument("pdf")
    r.add_argument("md")
    r.add_argument("--retiros")
    r.add_argument("--informe")
    args = ap.parse_args()
    if args.comando == "visible":
        retiros = convertir_visible(args.pdf, args.salida)
        if args.retiros:
            Path(args.retiros).write_text(json.dumps({"retiros": retiros}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{args.salida}: {len(analizar(args.pdf)['paginas'])} páginas · {len(retiros)} retiros declarados")
        return 0
    informe = verificar(args.pdf, args.md, args.retiros)
    if args.informe:
        Path(args.informe).write_text(json.dumps(informe, ensure_ascii=False, indent=2), encoding="utf-8")
    for d in informe["detalle"]:
        if d["veredicto"] != VERDE:
            print(f"p. {d['pagina']:>4} {d['veredicto']} (kit {d['veredicto_kit']}) [{d['clase']}] " + " | ".join(d["hallazgos"]))
    for e in informe["errores_documento"]:
        print(f"doc    {ROJO} {e}")
    for a in informe["avisos"]:
        print(f"aviso  {a}")
    print(f"\n{informe['veredicto']} (kit {informe['kit']['veredicto']}) {Path(args.pdf).name}: "
          f"{informe['paginas']} págs · código {informe['codigo_salida']}")
    return informe["codigo_salida"]


if __name__ == "__main__":
    sys.exit(main())
