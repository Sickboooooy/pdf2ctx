#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chunking.py — Chunking jurídico para pdf2ctx

Trocea el `.md` verificado (con anclas `[[p. N]]`) en chunks semánticos listos para
RAG/LLM, conservando la procedencia: rango de páginas y jerarquía normativa
(título → capítulo → sección → artículo → fracción → inciso → párrafo).

El chunking es una **capa de lectura** sobre el documento ya verificado: NO altera el
`.md` ni el veredicto del gate. Solo importa `gate_fidelidad` para reutilizar la
normalización y la lectura de anclas (nunca se copia ni se edita el kit).

Cada chunk
  { "id": "ley-01#articulo-0007",
    "tipo": "articulo",
    "titulo": "Artículo 7",
    "encabezado": ["TÍTULO PRIMERO", "CAPÍTULO I", "Artículo 7"],
    "paginas": [3, 4],          # primera y última página que toca
    "anclas": [3, 4],           # páginas con ancla dentro del chunk
    "texto": "…",               # el cuerpo (con o sin anclas, según --sin-anclas)
    "caracteres": 1234 }

Niveles: articulo (defecto) · fraccion · inciso · capitulo · seccion · titulo · parrafo · pagina.
Si el documento no tiene la jerarquía pedida (p. ej. una sentencia sin artículos), el nivel
se degrada solo: se trocea por párrafos o páginas, según convenga, y se anota en `nota`.

Uso
  python chunking.py doc.md -o chunks.jsonl --nivel articulo
  python chunking.py doc.md -o chunks.json --formato json --nivel fraccion --max-chars 3000
  python chunking.py doc.md --nivel articulo            # imprime los chunks a stdout
"""
import argparse
import json
import re
import sys
from pathlib import Path

import gate_fidelidad as gate  # noqa: E402

VERSION = "0.1.0"

# ───────────────────────── clasificación de encabezados ─────────────────────────
# Se aplican sobre el texto plano (minúsculas, sin acentos, espacios colapsados).
RE_TITULO = re.compile(
    r"^titulo\s+(?:"
    r"primero|segundo|tercero|cuarto|quinto|sexto|septimo|octavo|noveno|decimo"
    r"|undecimo|duodecimo|unico|[ivxlcdm]+|[a-z]+)\b")
RE_CAPITULO = re.compile(
    r"^capitulo\s+(?:"
    r"primero|segundo|tercero|cuarto|quinto|sexto|septimo|octavo|noveno|decimo"
    r"|unico|[ivxlcdm]+|[a-z]+)\b")
RE_SECCION = re.compile(
    r"^seccion\s+(?:"
    r"primera|segunda|tercera|cuarta|quinta|sexta|septima|octava|novena|decima"
    r"|unica|[ivxlcdm]+|[a-z]+)\b")
RE_ARTICULO = re.compile(
    r"^art(?:iculo)?s?\.?\s+(\d+(?:[-\s]\d+)?)\s*(?:bis|ter|quater)?\b")
RE_FRACCION = re.compile(r"^([ivxlcdm]+)\s*[.)\-\u2014]\s+")
RE_INCISO = re.compile(r"^[a-zñ][)]\s*")

# Encabezados de transitorios y de sentencias que conviene conservar como hitos.
RE_TRANSITORIO = re.compile(r"^(?:articulos\s+)?transitorios?\b")
RE_SENTENCIA = re.compile(
    r"^(?:resultando|considerando|resuelve|resuelven|vistos?|puntos?\s+resolutivos?|"
    r"primero|segundo|tercero|cuarto|quinto|sexto|septimo|octavo|noveno|decimo)"
    r"(?:\s*[.\-\u2013\u2014])?\s*$")

NIVELES = ("documento", "titulo", "capitulo", "seccion", "articulo", "fraccion", "inciso",
           "parrafo", "pagina")
ORDEN_NIVEL = {n: i for i, n in enumerate(NIVELES)}

# Niveles que parten por jerarquía normativa (los demás tienen lógica propia).
NIVELES_JERARQUIA = ("titulo", "capitulo", "seccion", "articulo", "fraccion", "inciso")


# ───────────────────────── lectura por páginas ─────────────────────────

def _segmentos(texto_md: str) -> list:
    """[(página | None, [líneas])] separadas por las anclas [[p. N]] (sin las anclas)."""
    paginas, _, _ = gate.leer_md(texto_md)
    if paginas is None:
        return [(None, texto_md.splitlines())]
    return [(n, paginas[n].splitlines()) for n in sorted(paginas)]


def _items(texto_md: str) -> list:
    """[(página, línea)] aplanadas conservando el orden del documento."""
    items = []
    for pagina, lineas in _segmentos(texto_md):
        for linea in lineas:
            items.append((pagina, linea))
    return items


def _clasificar(linea_plana: str, dentro_articulo: bool):
    """Devuelve (tipo, título) si la línea es un encabezado, o None si es contenido."""
    m = RE_TITULO.match(linea_plana)
    if m:
        return "titulo", linea_plana[:64]
    m = RE_CAPITULO.match(linea_plana)
    if m:
        return "capitulo", linea_plana[:64]
    m = RE_SECCION.match(linea_plana)
    if m:
        return "seccion", linea_plana[:64]
    m = RE_ARTICULO.match(linea_plana)
    if m:
        return "articulo", f"Artículo {m.group(1)}"
    if dentro_articulo:
        m = RE_FRACCION.match(linea_plana)
        if m:
            return "fraccion", f"Fracción {m.group(1).upper()}"
        m = RE_INCISO.match(linea_plana)
        if m:
            return "inciso", f"Inciso {linea_plana[0]})"
    if RE_TRANSITORIO.match(linea_plana):
        return "transitorio", "Transitorios"
    if RE_SENTENCIA.match(linea_plana):
        return "sentencia", linea_plana[:64]
    return None


def _titulo(tipo: str, clave: str, linea: str) -> str:
    """Título legible del encabezado: conserva el texto original donde es un encabezado propio."""
    if tipo in ("titulo", "capitulo", "seccion", "articulo", "sentencia", "transitorio"):
        return linea.strip()
    return clave  # fraccion / inciso ya vienen construidos


# ───────────────────────── render del chunk ─────────────────────────

def _render(items: list, conservar_anclas: bool) -> str:
    """Une [(página, línea)] reinsertando las anclas cuando cambia la página."""
    if not conservar_anclas:
        return "\n".join(linea for _, linea in items)
    partes, actual = [], None
    for pagina, linea in items:
        if pagina is not None and pagina != actual:
            partes.append(f"[[p. {pagina}]]")
            actual = pagina
        partes.append(linea)
    return "\n".join(partes)


def _paginas_de(items: list) -> list:
    return sorted({p for p, _ in items if p is not None})


def _chunk(items: list, tipo: str, titulo: str, encabezado: list, doc_id: str,
           contador: int, conservar_anclas: bool, nota: str = "") -> dict:
    paginas = _paginas_de(items)
    texto = _render(items, conservar_anclas).strip("\n")
    return {
        "id": f"{doc_id}#{tipo}-{contador:04d}",
        "tipo": tipo,
        "titulo": titulo,
        "encabezado": encabezado,
        "paginas": [paginas[0], paginas[-1]] if paginas else None,
        "anclas": paginas,
        "texto": texto,
        "caracteres": len(texto),
        **({"nota": nota} if nota else {}),
    }


# ───────────────────────── chunking por jerarquía ─────────────────────────

def _chunk_por_jerarquia(texto_md: str, nivel: str, doc_id: str, conservar_anclas: bool,
                         max_chars: int) -> list:
    items = _items(texto_md)
    tope = ORDEN_NIVEL[nivel]
    jerarquia = []           # lista de (tipo, título) vigentes, de mayor a menor
    actual, actual_encab = [], []
    chunks, contador = [], 0

    def cerrar(nota=""):
        nonlocal actual, actual_encab, contador
        if any(linea.strip() for _, linea in actual):
            titulo = actual_encab[-1][1] if actual_encab else "Documento"
            tipo = actual_encab[-1][0] if actual_encab else "documento"
            if tipo in ("transitorio", "sentencia"):
                tipo = "parrafo"
            chunks.append(_chunk(actual, tipo, titulo, [t for _, t in actual_encab],
                                 doc_id, contador, conservar_anclas, nota))
            contador += 1
        actual, actual_encab = [], []

    dentro_articulo = False
    for pagina, linea in items:
        plano = gate.plano(linea)
        enc = _clasificar(plano, dentro_articulo)
        if enc is None:
            actual.append((pagina, linea))
            continue
        tipo, titulo = enc
        titulo = _titulo(tipo, titulo, linea)
        if tipo in ("transitorio", "sentencia"):
            # Hito de sentencia/transitorio: inicia un párrafo propio que acumula el
            # contenido siguiente hasta el próximo hito o encabezado normativo.
            cerrar()
            jerarquia = []
            dentro_articulo = False
            actual_encab = [(tipo, titulo)]
            actual = [(pagina, linea)]
            continue
        if ORDEN_NIVEL[tipo] <= tope:
            cerrar()
            # actualizar jerarquía: quitar niveles más profundos que el nuevo
            while jerarquia and ORDEN_NIVEL[jerarquia[-1][0]] >= ORDEN_NIVEL[tipo]:
                jerarquia.pop()
            jerarquia.append((tipo, titulo))
            actual_encab = list(jerarquia)
            dentro_articulo = any(t == "articulo" for t, _ in jerarquia)
            actual = [(pagina, linea)]
        else:
            # Encabezado más profundo que el nivel pedido: queda dentro del chunk actual.
            if not actual_encab:
                actual_encab = [(tipo, titulo)]
            actual.append((pagina, linea))
            dentro_articulo = True
    cerrar()
    if not chunks:
        return _chunk_por_parrafo(texto_md, doc_id, conservar_anclas, max_chars,
                                  nota=f"sin jerarquía «{nivel}»")
    return _recortar(chunks, max_chars)


# ───────────────────────── chunking por párrafo ─────────────────────────

def _chunk_por_parrafo(texto_md: str, doc_id: str, conservar_anclas: bool, max_chars: int,
                       nota: str = "") -> list:
    items = _items(texto_md)
    chunks, actual, contador = [], [], 0
    for pagina, linea in items:
        if not linea.strip():
            if actual:
                chunks.append(_chunk(actual, "parrafo", "Párrafo", [], doc_id, contador,
                                     conservar_anclas))
                contador += 1
                actual = []
            continue
        actual.append((pagina, linea))
        if sum(len(l) for _, l in actual) >= max_chars:
            chunks.append(_chunk(actual, "parrafo", "Párrafo", [], doc_id, contador,
                                 conservar_anclas))
            contador += 1
            actual = []
    if actual:
        chunks.append(_chunk(actual, "parrafo", "Párrafo", [], doc_id, contador,
                             conservar_anclas))
        contador += 1
    return _recortar(chunks, max_chars)


# ───────────────────────── chunking por página ─────────────────────────

def _chunk_por_pagina(texto_md: str, doc_id: str, conservar_anclas: bool) -> list:
    chunks, contador = [], 0
    for pagina, lineas in _segmentos(texto_md):
        if pagina is None or not any(l.strip() for l in lineas):
            continue
        chunks.append(_chunk([(pagina, l) for l in lineas], "pagina", f"Página {pagina}", [],
                             doc_id, contador, conservar_anclas))
        contador += 1
    return chunks


def _recortar(chunks: list, max_chars: int) -> list:
    """Parte en ventanas los chunks que exceden max_chars (por líneas, sin cortar palabras)."""
    salida = []
    for c in chunks:
        if c["caracteres"] <= max_chars:
            salida.append(c)
            continue
        lineas = c["texto"].splitlines()
        parte, n = [], 0
        for linea in lineas:
            if parte and sum(len(l) for l in parte) + len(linea) > max_chars:
                trozo = {**c, "texto": "\n".join(parte), "caracteres": sum(len(l) for l in parte),
                         "id": f"{c['id']}-{n}", "nota": c.get("nota", "") + " (recortado)"}
                salida.append(trozo)
                parte, n = [], n + 1
            parte.append(linea)
        if parte:
            trozo = {**c, "texto": "\n".join(parte), "caracteres": sum(len(l) for l in parte),
                     "id": f"{c['id']}-{n}", "nota": c.get("nota", "") + " (recortado)"}
            salida.append(trozo)
    return salida


# ───────────────────────── API ─────────────────────────

def chunk_texto(texto_md: str, nivel: str = "articulo", doc_id: str = "doc",
                max_chars: int = 6000, conservar_anclas: bool = True) -> list:
    """Trocea un .md (string) en chunks. Devuelve la lista de dicts."""
    nivel = nivel.lower()
    if nivel not in NIVELES:
        raise ValueError(f"nivel desconocido «{nivel}» (válidos: {', '.join(NIVELES)})")
    if nivel == "pagina":
        return _chunk_por_pagina(texto_md, doc_id, conservar_anclas)
    if nivel == "parrafo":
        return _chunk_por_parrafo(texto_md, doc_id, conservar_anclas, max_chars)
    return _chunk_por_jerarquia(texto_md, nivel, doc_id, conservar_anclas, max_chars)


def chunk_documento(ruta_md, nivel: str = "articulo", doc_id: str = None,
                    max_chars: int = 6000, conservar_anclas: bool = True) -> list:
    """Trocea un .md en disco. `doc_id` por defecto sale del nombre del archivo."""
    ruta = Path(ruta_md)
    doc_id = doc_id or ruta.stem
    texto = ruta.read_text(encoding="utf-8")
    return chunk_texto(texto, nivel, doc_id, max_chars, conservar_anclas)


# ───────────────────────── CLI ─────────────────────────

def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Chunking jurídico del .md verificado (pdf2ctx).")
    ap.add_argument("md", help=".md con anclas [[p. N]] (salida del gate/convertidor)")
    ap.add_argument("-o", "--salida", help="Ruta de salida (jsonl o json según extensión)")
    ap.add_argument("--nivel", default="articulo", help=f"Nivel de troceo: {', '.join(NIVELES)}")
    ap.add_argument("--formato", choices=("jsonl", "json"), default="jsonl")
    ap.add_argument("--max-chars", type=int, default=6000, help="Tamaño máximo de chunk")
    ap.add_argument("--sin-anclas", action="store_true", help="No reinsertar [[p. N]] en el texto")
    ap.add_argument("--doc-id", help="Prefijo para los ids (por defecto, el nombre del archivo)")
    args = ap.parse_args()

    chunks = chunk_documento(args.md, args.nivel, args.doc_id, args.max_chars,
                             conservar_anclas=not args.sin_anclas)
    if args.salida:
        ruta = Path(args.salida)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        texto = ("\n".join(json.dumps(c, ensure_ascii=False) for c in chunks) + "\n"
                 if args.formato == "jsonl"
                 else json.dumps(chunks, ensure_ascii=False, indent=2))
        ruta.write_text(texto, encoding="utf-8")
        print(f"{ruta}: {len(chunks)} chunks (nivel {args.nivel})")
    else:
        for c in chunks:
            print(json.dumps(c, ensure_ascii=False))
    print(f"Total: {len(chunks)} chunks", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
