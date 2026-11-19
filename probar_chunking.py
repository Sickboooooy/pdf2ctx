#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probar_chunking.py — El gate del chunking jurídico (pdf2ctx).

Verifica sobre un .md sintético con jerarquía normativa que chunk_texto produce los
chunks esperados: tipos, títulos, páginas y texto. Salida 0 si todo pasa, 1 si algo falla.
"""
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chunking  # noqa: E402

LEY = """[[p. 1]]
TÍTULO PRIMERO
Disposiciones Generales

CAPÍTULO I
Del objeto de la ley

Artículo 1
El objeto de la presente ley es regular el impuesto al valor agregado.

Artículo 2
Para los efectos de esta ley se entiende por:
I. Enajenación, la transmisión de bienes.
II. Servicio, la prestación de actividades.
a) independientes;
b) subordinadas.

[[p. 2]]
Artículo 3
El impuesto se calculará aplicando la tasa del 16% conforme a la fracción II.

TRANSITORIOS

Primero
La presente ley entrará en vigor el 1 de enero de 2026.

Segundo
Se derogan las disposiciones que se opongan a la presente ley.
"""


def checar(nombre, condicion, detalle=""):
    print(f"  {'✅' if condicion else '❌'} {nombre}" + (f" — {detalle}" if detalle else ""))
    return condicion


def main() -> int:
    ok = True

    # ── por artículo ──
    arts = [c for c in chunking.chunk_texto(LEY, nivel="articulo", doc_id="ley")
            if c["tipo"] == "articulo"]
    ok &= checar("3 artículos", len(arts) == 3, f"{len(arts)} artículos")
    ok &= checar("títulos de artículo", [a["titulo"] for a in arts] ==
                 ["Artículo 1", "Artículo 2", "Artículo 3"],
                 str([a["titulo"] for a in arts]))
    a2 = arts[1]
    ok &= checar("fracciones dentro del artículo 2", "I. Enajenación" in a2["texto"]
                 and "a) independientes" in a2["texto"])
    ok &= checar("página del artículo 3", arts[2]["paginas"] == [2, 2], str(arts[2]["paginas"]))
    ok &= checar("jerarquía del artículo 3",
                 arts[2]["encabezado"][:2] == ["TÍTULO PRIMERO", "CAPÍTULO I"],
                 str(arts[2]["encabezado"]))

    # ── por fracción ──
    fracc = [c for c in chunking.chunk_texto(LEY, nivel="fraccion", doc_id="ley")
             if c["tipo"] == "fraccion"]
    ok &= checar("2 fracciones", len(fracc) == 2, f"{len(fracc)} fracciones")
    ok &= checar("fracción I título", fracc[0]["titulo"] == "Fracción I", fracc[0]["titulo"])

    # ── por página ──
    pags = chunking.chunk_texto(LEY, nivel="pagina", doc_id="ley")
    ok &= checar("2 páginas", len(pags) == 2 and all(c["tipo"] == "pagina" for c in pags),
                 f"{len(pags)} chunks")

    # ── sin anclas en el texto ──
    limpio = chunking.chunk_texto(LEY, nivel="articulo", doc_id="ley", conservar_anclas=False)
    ok &= checar("sin anclas inline", all("[[p." not in c["texto"] for c in limpio))

    # ── con anclas inline (por defecto) ──
    con_anclas = chunking.chunk_texto(LEY, nivel="articulo", doc_id="ley")
    a3 = [c for c in con_anclas if c["titulo"] == "Artículo 3"][0]
    ok &= checar("ancla inline en artículo 3", "[[p. 2]]" in a3["texto"])

    # ── degradación: sentencia sin artículos ──
    sentencia = """[[p. 1]]
RESULTANDO
Único. Se inició juicio.

CONSIDERANDO
Primero. La competencia es procedente.

[[p. 2]]
RESUELVE
PRIMERO.- Se concede el amparo.
SEGUNDO.- Se notifica personalmente.
"""
    parrafos = chunking.chunk_texto(sentencia, nivel="articulo", doc_id="sent")
    tipos = {c["tipo"] for c in parrafos}
    ok &= checar("sentencia degrada a párrafos", tipos == {"parrafo"}, str(tipos))
    ok &= checar("sentencia conserva RESUELVE", any("Se concede el amparo" in c["texto"]
                                                    for c in parrafos))

    # ── nivel inválido ──
    try:
        chunking.chunk_texto(LEY, nivel="noexiste")
        ok &= checar("nivel inválido lanza error", False)
    except ValueError:
        ok &= checar("nivel inválido lanza error", True)

    print("\nRESULTADO:", "OK" if ok else "FALLA")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
