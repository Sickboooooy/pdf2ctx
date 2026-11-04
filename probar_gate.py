#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probar_gate.py — El gate del gate (pdf2ctx · Sprint 0)

A partir de PDFs REALES genera el .md mínimo (fiel por construcción) y le inyecta un
defecto por caso. El gate debe reprobar, o abstenerse, exactamente como se espera.
No hay datos inventados: cada mutante es un documento real con un defecto controlado.

Casos                                                              Esperado
  LIMPIO                   .md fiel                                  🟢 (🟡 si hay páginas sin oráculo)
  M1_PAGINA_PERDIDA        se borra una página completa              🔴
  M2_CIFRA_ALTERADA        un dígito cambia                          🔴
  M3_CIFRA_OMITIDA         una cifra desaparece                      🔴
  M4_REFERENCIA_ALTERADA   "fracción IV" → "fracción VI" (o artículo) 🔴
  M5_CIFRA_INVENTADA       aparece un registro que no está en el PDF 🔴
  M6_TEXTO_OMITIDO         desaparece un tramo de texto sin cifras   🔴
  M7_RETIRO_DECLARADO      se quita una línea y se declara           igual que LIMPIO
  M8_RETIRO_FALSO          se declara un retiro inexistente          🔴
  M9_SIN_ANCLAS            el .md pierde todas las anclas            🟡
  M10_SIN_ANCLAS_Y_PERDIDA sin anclas y además falta una cifra       🔴 (quitar anclas no lava pérdidas)
  M11_PAGINA_ESCANEADA     una página del PDF se rasteriza           🟡, nunca 🟢

Salida: 0 = todo pasa y cada caso aplicó al menos una vez · 1 = algún caso falló
        3 = sin fallos, pero algún caso nunca aplicó en el corpus (hueco de cobertura)
        2 = error de uso (corpus incompleto, rutas inexistentes)

Precondición del LOOP SPEC: `--sabotaje G2` DEBE salir 1. Si sale 0, esta suite no prueba nada.

Uso
  python probar_gate.py --corpus corpus_sprint0.json
  python probar_gate.py doc1.pdf doc2.pdf [--sabotaje G2] [--salida resultados_sprint0]
"""
import argparse
import json
import math
import re
import sys
from pathlib import Path

import pypdfium2 as pdfium

import gate_fidelidad as gate

RE_DIGITOS = re.compile(r"\d+(?:[.,:/-]\d+)*")
RE_FRACCION = re.compile(r"(fracci[oó]n(?:es)?\s+)([IVXLCDM]+)\b", re.IGNORECASE)
RE_ARTICULO = re.compile(r"(art[ií]culos?\s+)(\d+)", re.IGNORECASE)

CASOS = ["LIMPIO", "M1_PAGINA_PERDIDA", "M2_CIFRA_ALTERADA", "M3_CIFRA_OMITIDA",
         "M4_REFERENCIA_ALTERADA", "M5_CIFRA_INVENTADA", "M6_TEXTO_OMITIDO",
         "M7_RETIRO_DECLARADO", "M8_RETIRO_FALSO", "M9_SIN_ANCLAS",
         "M10_SIN_ANCLAS_Y_PERDIDA", "M11_PAGINA_ESCANEADA"]
ETIQUETA = {0: "🟢", 1: "🔴", 3: "🟡"}


# ───────────────────────── utilidades ─────────────────────────

def componer(paginas: dict) -> str:
    return "\n".join(f"[[p. {n}]]{paginas[n]}" for n in sorted(paginas))


def sin_anclas(paginas: dict) -> str:
    return "\n\n".join(paginas[n].strip("\n") for n in sorted(paginas))


def primera_cifra(paginas: dict, candidatas: list):
    for n in candidatas:
        m = RE_DIGITOS.search(paginas[n])
        if m:
            return n, m
    return None, None


def omitir_primera_cifra(paginas: dict, candidatas: list):
    n, m = primera_cifra(paginas, candidatas)
    if n is None:
        return None, None
    texto = paginas[n]
    return {**paginas, n: texto[:m.start()] + texto[m.end():]}, f"p. {n}: se borró «{m.group(0)}»"


def romano_alterado(r: str) -> str:
    cambio = {"IV": "VI", "VI": "IV", "IX": "XI", "XI": "IX"}.get(r.upper(), r.upper() + "I")
    return cambio if r.isupper() else cambio.lower()


def rasterizar_pagina(ruta_pdf, n: int, ruta_salida, dpi: int = 110) -> None:
    """Copia del PDF donde la página n es solo imagen (como un escaneo sin capa de texto)."""
    origen = pdfium.PdfDocument(str(ruta_pdf))
    destino = pdfium.PdfDocument.new()
    try:
        for i in range(len(origen)):
            if i + 1 == n:
                pagina = origen[i]
                ancho, alto = pagina.get_size()
                mapa = pagina.render(scale=dpi / 72)
                nueva = destino.new_page(ancho, alto)
                imagen = pdfium.PdfImage.new(destino)
                imagen.set_bitmap(mapa)
                imagen.set_matrix(pdfium.PdfMatrix().scale(ancho, alto))
                nueva.insert_obj(imagen)
                nueva.gen_content()
                pagina.close()
            else:
                destino.import_pages(origen, [i])
        destino.save(str(ruta_salida))
    finally:
        destino.close()
        origen.close()


# ───────────────────────── mutantes ─────────────────────────
# Cada mutante devuelve (md, retiros | None, pdf_alterno | None, descripción) o None si no aplica.

def m_limpio(ctx):
    return componer(ctx["paginas"]), None, None, ".md mínimo sin cambios"


def m1(ctx):
    con_texto = [n for n in ctx["cand"] if gate.plano(ctx["paginas"][n])]
    if not con_texto:
        return None
    n = con_texto[len(con_texto) // 2]
    restantes = {k: v for k, v in ctx["paginas"].items() if k != n}
    return componer(restantes), None, None, f"se borró la p. {n}"


def m2(ctx):
    n, m = primera_cifra(ctx["paginas"], ctx["cand"])
    if n is None:
        return None
    s = m.group(0)
    i = max(k for k, ch in enumerate(s) if ch.isdigit())
    nuevo = s[:i] + str((int(s[i]) + 1) % 10) + s[i + 1:]
    texto = ctx["paginas"][n]
    texto = texto[:m.start()] + nuevo + texto[m.end():]
    return componer({**ctx["paginas"], n: texto}), None, None, f"p. {n}: «{s}» → «{nuevo}»"


def m3(ctx):
    paginas, desc = omitir_primera_cifra(ctx["paginas"], ctx["cand"])
    if paginas is None:
        return None
    return componer(paginas), None, None, desc


def m4(ctx):
    for n in ctx["cand"]:
        texto = ctx["paginas"][n]
        m = RE_FRACCION.search(texto)
        if m:
            nuevo = romano_alterado(m.group(2))
            texto = texto[:m.start(2)] + nuevo + texto[m.end(2):]
            return (componer({**ctx["paginas"], n: texto}), None, None,
                    f"p. {n}: fracción {m.group(2)} → {nuevo}")
    for n in ctx["cand"]:
        texto = ctx["paginas"][n]
        m = RE_ARTICULO.search(texto)
        if m:
            nuevo = str(int(m.group(2)) + 1)
            texto = texto[:m.start(2)] + nuevo + texto[m.end(2):]
            return (componer({**ctx["paginas"], n: texto}), None, None,
                    f"p. {n}: artículo {m.group(2)} → {nuevo}")
    return None


def m5(ctx):
    if not ctx["cand"]:
        return None
    n = ctx["cand"][0]
    presentes = gate.invariantes(gate.plano(ctx["oraculo_por_pag"][n]))
    numero = next(x for x in ("8675309", "7754321", "6543217") if "#" + x not in presentes)
    texto = ctx["paginas"][n].rstrip("\n") + f"\nRegistro digital: {numero}\n\n"
    return componer({**ctx["paginas"], n: texto}), None, None, f"p. {n}: se insertó registro {numero}"


def m6(ctx):
    for n in ctx["cand"]:
        texto = ctx["paginas"][n]
        if sum(gate.palabras(gate.plano(texto)).values()) < 40:
            continue
        tokens = list(re.finditer(r"\S+", texto))
        k = max(6, math.ceil(0.08 * len(tokens)))
        for i in range(0, len(tokens) - k):
            ventana = tokens[i:i + k]
            fragmento = texto[ventana[0].start():ventana[-1].end()]
            if re.search(r"\d|fracci|inciso|\[\[p\.", fragmento, re.IGNORECASE):
                continue
            nuevo = texto[:ventana[0].start()] + texto[ventana[-1].end():]
            return (componer({**ctx["paginas"], n: nuevo}), None, None,
                    f"p. {n}: se borraron {k} tokens sin cifras")
    return None


def m7(ctx):
    for n in ctx["cand"]:
        lineas = ctx["paginas"][n].split("\n")
        for idx, linea in enumerate(lineas):
            previa = lineas[idx - 1] if idx > 0 else ""
            if previa.rstrip().endswith("-") or linea.rstrip().endswith("-"):
                continue
            if sum(gate.palabras(gate.plano(linea)).values()) >= 3:
                nuevo = "\n".join(lineas[:idx] + lineas[idx + 1:])
                retiros = [{"pagina": n, "texto": linea.strip(), "motivo": "prueba: retiro declarado"}]
                return (componer({**ctx["paginas"], n: nuevo}), retiros, None,
                        f"p. {n}: retiro declarado «{linea.strip()[:40]}»")
    return None


def m8(ctx):
    if not ctx["cand"]:
        return None
    retiros = [{"pagina": ctx["cand"][0], "texto": "Este párrafo no existe en el original 4242",
                "motivo": "prueba: retiro falso"}]
    return componer(ctx["paginas"]), retiros, None, "retiro declarado que no existe en el original"


def m9(ctx):
    return sin_anclas(ctx["paginas"]), None, None, "se quitaron todas las anclas"


def m10(ctx):
    if not ctx["todas_texto"]:
        return None
    paginas, desc = omitir_primera_cifra(ctx["paginas"], ctx["cand"])
    if paginas is None:
        return None
    return sin_anclas(paginas), None, None, f"sin anclas y {desc}"


def m11(ctx):
    if not ctx["cand"]:
        return None
    n = ctx["cand"][len(ctx["cand"]) // 2]
    ruta = ctx["dir"] / f"M11_p{n}_rasterizada.pdf"
    rasterizar_pagina(ctx["pdf"], n, ruta)
    return componer(ctx["paginas"]), None, ruta, f"p. {n} rasterizada (sin capa de texto)"


GENERADORES = dict(zip(CASOS, [m_limpio, m1, m2, m3, m4, m5, m6, m7, m8, m9, m10, m11]))


def esperado(caso: str, base: int) -> int:
    if caso in ("LIMPIO", "M7_RETIRO_DECLARADO"):
        return base
    if caso in ("M9_SIN_ANCLAS", "M11_PAGINA_ESCANEADA"):
        return 3
    return 1


# ───────────────────────── corrida ─────────────────────────

def probar_pdf(ruta_pdf: Path, ident: str, salida: Path, sabotaje: frozenset) -> list:
    directorio = salida / "mutantes" / ident
    directorio.mkdir(parents=True, exist_ok=True)
    md_minimo = directorio / "LIMPIO.md"
    gate.exportar_minimo(ruta_pdf, md_minimo)

    oraculo = gate.leer_oraculo(ruta_pdf)
    paginas, _, _ = gate.leer_md(md_minimo.read_text(encoding="utf-8"))
    ctx = {
        "pdf": ruta_pdf, "dir": directorio, "paginas": paginas,
        "cand": [p["n"] for p in oraculo if p["clase"] == "texto" and p["n"] in paginas],
        "oraculo_por_pag": {p["n"]: p["texto"] for p in oraculo},
        "todas_texto": all(p["clase"] == "texto" for p in oraculo),
    }
    base = 0 if ctx["todas_texto"] else 3

    resultados = []
    for caso in CASOS:
        mutante = GENERADORES[caso](ctx)
        if mutante is None:
            resultados.append({"doc": ident, "caso": caso, "estado": "NO APLICA"})
            continue
        md, retiros, pdf_alterno, descripcion = mutante
        ruta_md = directorio / f"{caso}.md"
        ruta_md.write_text(md, encoding="utf-8")
        ruta_ret = None
        if retiros:
            ruta_ret = directorio / f"{caso}.retiros.json"
            ruta_ret.write_text(json.dumps({"retiros": retiros}, ensure_ascii=False, indent=2), encoding="utf-8")
        informe = gate.verificar(pdf_alterno or ruta_pdf, ruta_md, ruta_ret, desactivar=sabotaje)
        (directorio / f"{caso}.informe.json").write_text(
            json.dumps(informe, ensure_ascii=False, indent=2), encoding="utf-8")
        obtenido, esperado_ = informe["codigo_salida"], esperado(caso, base)
        resultados.append({"doc": ident, "caso": caso, "descripcion": descripcion,
                           "esperado": esperado_, "obtenido": obtenido,
                           "estado": "PASA" if obtenido == esperado_ else "FALLA"})
    return resultados


def cargar_corpus(ruta_json: Path) -> list:
    datos = json.loads(ruta_json.read_text(encoding="utf-8"))
    docs = []
    for d in datos["documentos"]:
        ruta = (ruta_json.parent / d["pdf"]).resolve()
        docs.append((d["id"], ruta))
    return docs


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Mutantes de PDFs reales que el gate debe cazar.")
    ap.add_argument("pdfs", nargs="*", help="PDFs reales (alternativa a --corpus)")
    ap.add_argument("--corpus", help="corpus_sprint0.json")
    ap.add_argument("--salida", default="resultados_sprint0")
    ap.add_argument("--sabotaje", default="", help="Desactiva chequeos del gate (p. ej. G2): la suite DEBE fallar")
    args = ap.parse_args()

    docs = cargar_corpus(Path(args.corpus)) if args.corpus else [(Path(p).stem, Path(p)) for p in args.pdfs]
    faltantes = [str(r) for _, r in docs if not r.is_file()]
    if not docs or faltantes:
        print("Corpus vacío o con rutas inexistentes:\n  " + "\n  ".join(faltantes or ["(sin documentos)"]))
        return 2

    sabotaje = frozenset(x.strip().upper() for x in args.sabotaje.split(",") if x.strip())
    if sabotaje:
        print(f"⚠️  MODO SABOTAJE {sorted(sabotaje)}: si la suite sale 0, la suite no prueba nada.\n")

    salida = Path(args.salida)
    todos = []
    for ident, ruta in docs:
        todos.extend(probar_pdf(ruta, ident, salida, sabotaje))

    print(f"{'documento':<20} {'caso':<26} {'esp':>4} {'obt':>4}  estado   detalle")
    for r in todos:
        if r["estado"] == "NO APLICA":
            print(f"{r['doc']:<20} {r['caso']:<26} {'':>4} {'':>4}  NO APLICA")
        else:
            print(f"{r['doc']:<20} {r['caso']:<26} {ETIQUETA[r['esperado']]:>4} {ETIQUETA[r['obtenido']]:>4}  "
                  f"{r['estado']:<8} {r['descripcion']}")

    fallas = [r for r in todos if r["estado"] == "FALLA"]
    nunca = [c for c in CASOS if all(r["estado"] == "NO APLICA" for r in todos if r["caso"] == c)]
    codigo = 1 if fallas else (3 if nunca else 0)
    resumen = {"codigo_salida": codigo, "sabotaje": sorted(sabotaje), "fallas": len(fallas),
               "casos_sin_cobertura": nunca, "resultados": todos}
    salida.mkdir(parents=True, exist_ok=True)
    (salida / "probar_gate.json").write_text(json.dumps(resumen, ensure_ascii=False, indent=2), encoding="utf-8")

    pasan = sum(r["estado"] == "PASA" for r in todos)
    no_aplica = sum(r["estado"] == "NO APLICA" for r in todos)
    def n(k, uno, varios):
        return f"{k} {uno if k == 1 else varios}"
    print(f"\n{n(pasan, 'pasa', 'pasan')} · {n(no_aplica, 'no aplica', 'no aplican')} · "
          f"{n(len(fallas), 'falla', 'fallan')}"
          + (f" · casos que nunca aplicaron: {', '.join(nunca)}" if nunca else "")
          + f" · código {codigo}")
    return codigo


if __name__ == "__main__":
    sys.exit(main())
