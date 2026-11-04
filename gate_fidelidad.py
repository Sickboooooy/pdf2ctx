#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gate_fidelidad.py — Gate de fidelidad VVCA para PDF → Markdown (pdf2ctx · Sprint 0)

ORÁCULO      Capa de texto del PDF original, leída con PDFium (pypdfium2, BSD-3/Apache-2.0).
BAJO PRUEBA  Un .md con anclas de página [[p. N]], producido por cualquier convertidor.

Veredicto por página
  🟢 VERIFICADO          cifras, referencias y cobertura coinciden con el oráculo
  🟡 NO VERIFICABLE      no hay oráculo confiable (sin capa de texto, OCR previo, sin anclas)
  🔴 PÉRDIDA/ALTERACIÓN  algo del original falta, cambió, o apareció sin estar en el original

Chequeos
  G1  páginas: cada página del original tiene su ancla [[p. N]], en orden y sin duplicados
  G2  invariantes: cifras (montos, fechas, registros, folios) y referencias (artículo,
      fracción, inciso) del original aparecen en el .md, y no aparecen otras nuevas
  G3  residuo: líneas repetidas entre páginas (métrica de eficiencia; no bloquea en Sprint 0)
  G4  cobertura: fracción de palabras del original presentes en el .md
  G5  retiros: toda eliminación se declara; lo que desaparece sin declararse es pérdida

Códigos de salida: 0 = todo 🟢 · 1 = algún 🔴 · 3 = sin 🔴 pero con 🟡 · 2 = error de uso

Uso
  python gate_fidelidad.py minimo    doc.pdf -o doc.md
  python gate_fidelidad.py verificar doc.pdf doc.md [--retiros retiros.json] [--informe informe.json]
"""
import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

VERSION = "0.1.0-sprint0"

ANCLA = re.compile(r"^\[\[p\. (\d+)\]\][ \t]*$", re.MULTILINE)

# Invariantes: lo que, si cambia, convierte un error de conversión en un error jurídico.
# Se buscan sobre texto "plano" (minúsculas, sin acentos, espacios colapsados).
RE_CIFRA = re.compile(r"\d+(?:[.,:/-]\d+)*")
RE_REFERENCIA = re.compile(
    r"\b(?:art(?:iculo)?s?\.?\s*\d+[a-z]?(?:\s+(?:bis|ter|quater))?"
    r"|fraccion(?:es)?\s+[ivxlcdm]+\b"
    r"|inciso\s+[a-z]\))"
)
RE_PALABRA = re.compile(r"[a-z0-9]+")

UMBRAL_CHARS_TEXTO = 20          # menos caracteres útiles = página sin capa de texto
UMBRAL_COBERTURA_IMAGEN = 0.85   # imagen que cubre la página + texto = OCR preexistente
COBERTURA_MIN = 0.97             # fracción mínima de palabras del original presentes
RETIRO_MAX = 0.20                # retiros declarados por encima de esto exigen revisión humana

VERDE, AMARILLO, ROJO = "🟢", "🟡", "🔴"


# ───────────────────────── normalización ─────────────────────────

def sin_sustitutos(texto: str) -> str:
    """Quita sustitutos UTF-16 sueltos que a veces devuelve una capa de texto dañada."""
    return re.sub(r"[\ud800-\udfff]", "", texto)


def normalizar(texto: str) -> str:
    """Normalización sin pérdida semántica, idéntica para oráculo y .md."""
    t = unicodedata.normalize("NFKC", sin_sustitutos(texto))
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = t.replace("\u00ad", "")                                   # guion suave
    t = re.sub(r"[\u2010-\u2015\u2212]", "-", t)                  # guiones tipográficos
    t = re.sub(r"[\x00-\x08\x0b-\x1f\x7f\ufeff\ufffe]", "", t)     # controles (p. ej. \x02 de PDFium)
    t = re.sub(r"([^\W\d_])-\n\s*([^\W\d_])", r"\1\2", t)         # palabra cortada al final de línea
    return t


def plano(texto: str) -> str:
    """Minúsculas, sin acentos ni diacríticos, espacios colapsados: base de comparación."""
    t = unicodedata.normalize("NFKD", normalizar(texto).lower())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", t).strip()


def clave_referencia(m: str) -> str:
    m = re.sub(r"\s+", " ", m.strip())
    m = re.sub(r"^art(?:iculo)?s?\.?\s*", "art ", m)
    m = re.sub(r"^fracciones?", "fraccion", m)
    return m


def invariantes(texto_plano: str) -> Counter:
    c = Counter("#" + m for m in RE_CIFRA.findall(texto_plano))
    c.update("§" + clave_referencia(m) for m in RE_REFERENCIA.findall(texto_plano))
    return c


def palabras(texto_plano: str) -> Counter:
    return Counter(RE_PALABRA.findall(texto_plano))


def muestra(contador: Counter, n: int = 12) -> str:
    items = [f"{k[1:]}×{v}" if v > 1 else k[1:] for k, v in contador.most_common(n)]
    resto = len(contador) - n
    return ", ".join(items) + (f" … (+{resto})" if resto > 0 else "")


def sha256(ruta) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


# ───────────────────────── oráculo ─────────────────────────

def _limites(obj):
    """(izq, abajo, der, arriba) de un objeto de página; compatible con pypdfium2 v4 y v5."""
    if hasattr(obj, "get_bounds"):
        return obj.get_bounds()
    return obj.get_pos()


def leer_oraculo(ruta_pdf) -> list:
    """Texto por página + clase de confiabilidad del oráculo."""
    pdf = pdfium.PdfDocument(str(ruta_pdf))
    paginas = []
    try:
        for i in range(len(pdf)):
            pagina = pdf[i]
            tp = pagina.get_textpage()
            texto = tp.get_text_range()
            ancho, alto = pagina.get_size()
            area = max(ancho * alto, 1.0)
            cubierto = 0.0
            for obj in pagina.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE]):
                izq, abajo, der, arriba = _limites(obj)
                izq, abajo = max(izq, 0.0), max(abajo, 0.0)
                der, arriba = min(der, ancho), min(arriba, alto)
                if der > izq and arriba > abajo:
                    cubierto += (der - izq) * (arriba - abajo)
            utiles = len(re.sub(r"\s", "", normalizar(texto)))
            cobertura_img = min(cubierto / area, 1.0)
            if utiles < UMBRAL_CHARS_TEXTO:
                clase = "sin_texto"
            elif cobertura_img >= UMBRAL_COBERTURA_IMAGEN:
                clase = "ocr_previo"
            else:
                clase = "texto"
            paginas.append({"n": i + 1, "texto": texto, "clase": clase,
                            "caracteres": utiles, "cobertura_imagen": round(cobertura_img, 3)})
            tp.close()
            pagina.close()
    finally:
        pdf.close()
    return paginas


def exportar_minimo(ruta_pdf, ruta_md) -> int:
    """PDF → .md mínimo: capa de texto + anclas. Mide tokens; NO prueba fidelidad."""
    oraculo = leer_oraculo(ruta_pdf)
    partes = []
    for p in oraculo:
        texto = sin_sustitutos(p["texto"]).replace("\r\n", "\n").replace("\r", "\n").strip()
        partes.append(f"[[p. {p['n']}]]\n{texto}\n")
    Path(ruta_md).write_text("\n".join(partes), encoding="utf-8")
    return len(oraculo)


# ───────────────────────── lectura del .md ─────────────────────────

def leer_md(texto_md: str):
    """Devuelve (páginas {n: texto} | None si no hay anclas, preámbulo, errores de anclas)."""
    marcas = list(ANCLA.finditer(texto_md))
    if not marcas:
        return None, texto_md, []
    paginas, errores = {}, []
    numeros = [int(m.group(1)) for m in marcas]
    if numeros != sorted(numeros):
        errores.append("G1 anclas fuera de orden")
    for j, m in enumerate(marcas):
        n = int(m.group(1))
        fin = marcas[j + 1].start() if j + 1 < len(marcas) else len(texto_md)
        if n in paginas:
            errores.append(f"G1 ancla duplicada [[p. {n}]]")
        paginas[n] = paginas.get(n, "") + texto_md[m.end():fin]
    return paginas, texto_md[:marcas[0].start()], errores


def cargar_retiros(ruta) -> list:
    if not ruta:
        return []
    datos = json.loads(Path(ruta).read_text(encoding="utf-8"))
    lista = datos.get("retiros", []) if isinstance(datos, dict) else datos
    for r in lista:
        if not {"pagina", "texto", "motivo"} <= set(r):
            raise ValueError(f"Retiro mal formado (requiere pagina, texto, motivo): {r}")
    return lista


# ───────────────────────── evaluación ─────────────────────────

def evaluar_pagina(p: dict, texto_md, retiros_pag: list, cobertura_min: float,
                   desactivar: frozenset) -> dict:
    hallazgos_rojos, hallazgos_amarillos, metricas = [], [], {}

    if texto_md is None and "G1" not in desactivar:
        return {"pagina": p["n"], "clase": p["clase"], "veredicto": ROJO,
                "hallazgos": [f"G1 página perdida: no existe [[p. {p['n']}]] en el .md"],
                "metricas": metricas}

    plano_or, plano_md = plano(p["texto"]), plano(texto_md or "")
    inv_or, inv_md = invariantes(plano_or), invariantes(plano_md)
    pal_or, pal_md = palabras(plano_or), palabras(plano_md)

    # G5 — retiros declarados: deben existir en el original; se descuentan de lo esperado
    inv_ret, pal_ret = Counter(), Counter()
    for r in retiros_pag:
        pr = plano(r["texto"])
        if "G5" not in desactivar and pr and pr not in plano_or:
            hallazgos_rojos.append(f"G5 retiro declarado que no existe en el original: «{r['texto'][:60]}»")
        inv_ret.update(invariantes(pr))
        pal_ret.update(palabras(pr))
    esperado_inv, esperado_pal = inv_or - inv_ret, pal_or - pal_ret

    if p["clase"] == "sin_texto":
        cifras = sorted({k[1:] for k in inv_md})
        metricas["invariantes_por_cotejar"] = cifras[:60]
        estructurales = [h for h in hallazgos_rojos if h.startswith("G5")]
        if estructurales:
            return {"pagina": p["n"], "clase": p["clase"], "veredicto": ROJO,
                    "hallazgos": estructurales, "metricas": metricas}
        return {"pagina": p["n"], "clase": p["clase"], "veredicto": AMARILLO,
                "hallazgos": ["NO VERIFICABLE: página sin capa de texto. Cotejar contra la imagen."],
                "metricas": metricas}

    # G2 — invariantes
    if "G2" not in desactivar:
        faltan = esperado_inv - inv_md
        sobran = inv_md - inv_or
        if faltan:
            hallazgos_rojos.append(f"G2 faltan en el .md ({sum(faltan.values())}): {muestra(faltan)}")
        if sobran:
            hallazgos_rojos.append(f"G2 no están en el original ({sum(sobran.values())}): {muestra(sobran)}")

    # G4 — cobertura de palabras
    total = sum(esperado_pal.values())
    if total:
        cobertura = sum((esperado_pal & pal_md).values()) / total
        metricas["cobertura_palabras"] = round(cobertura, 4)
        if "G4" not in desactivar and cobertura < cobertura_min:
            hallazgos_rojos.append(f"G4 cobertura {cobertura:.3f} < {cobertura_min}")

    fraccion_retirada = sum(pal_ret.values()) / max(sum(pal_or.values()), 1)
    metricas["fraccion_retirada"] = round(fraccion_retirada, 4)
    if fraccion_retirada > RETIRO_MAX:
        hallazgos_amarillos.append(f"Retiro masivo declarado ({fraccion_retirada:.0%}): revisión humana")

    if p["clase"] == "ocr_previo":
        estructurales = [h for h in hallazgos_rojos if h.startswith("G5")]
        if estructurales:
            return {"pagina": p["n"], "clase": p["clase"], "veredicto": ROJO,
                    "hallazgos": estructurales, "metricas": metricas}
        notas = ["NO VERIFICABLE: oráculo débil (capa OCR preexistente)."]
        notas += [f"Discrepancia contra oráculo débil — {h}" for h in hallazgos_rojos]
        return {"pagina": p["n"], "clase": p["clase"], "veredicto": AMARILLO,
                "hallazgos": notas + hallazgos_amarillos, "metricas": metricas}

    if hallazgos_rojos:
        veredicto = ROJO
    elif hallazgos_amarillos:
        veredicto = AMARILLO
    else:
        veredicto = VERDE
    return {"pagina": p["n"], "clase": p["clase"], "veredicto": veredicto,
            "hallazgos": hallazgos_rojos + hallazgos_amarillos, "metricas": metricas}


def residuo_repetido(paginas_md: dict) -> dict:
    """Métrica de eficiencia (no de fidelidad): líneas que se repiten en muchas páginas."""
    if not paginas_md:
        return {"residuo_pct": None, "lineas_repetidas": []}

    def llave(linea):
        return re.sub(r"\d+", "#", plano(linea))

    conteo = Counter()
    for texto in paginas_md.values():
        conteo.update({k for k in (llave(l) for l in texto.splitlines()) if len(k) >= 4})
    umbral = max(3, round(0.5 * len(paginas_md)))
    repetidas = {k for k, v in conteo.items() if v >= umbral}
    chars_rep = sum(len(l) for t in paginas_md.values() for l in t.splitlines() if llave(l) in repetidas)
    total = sum(len(t) for t in paginas_md.values()) or 1
    return {"residuo_pct": round(100 * chars_rep / total, 2),
            "lineas_repetidas": sorted(repetidas)[:20], "umbral_paginas": umbral}


def verificar(ruta_pdf, ruta_md, ruta_retiros=None, cobertura_min: float = COBERTURA_MIN,
              desactivar=frozenset()) -> dict:
    desactivar = frozenset(desactivar)
    oraculo = leer_oraculo(ruta_pdf)
    texto_md = Path(ruta_md).read_text(encoding="utf-8")
    paginas_md, preambulo, errores_doc = leer_md(texto_md)
    retiros = cargar_retiros(ruta_retiros)
    n_paginas = len(oraculo)

    retiros_por_pag = {}
    for r in retiros:
        retiros_por_pag.setdefault(int(r["pagina"]), []).append(r)
    for n in retiros_por_pag:
        if not 1 <= n <= n_paginas:
            errores_doc.append(f"G5 retiro declarado en página inexistente: {n}")

    avisos = []
    if preambulo.strip() and paginas_md is not None:
        avisos.append(f"Contenido antes de [[p. 1]] ({len(preambulo.strip())} caracteres) no atribuible a una página")

    detalle = []
    if paginas_md is None:
        for p in oraculo:
            detalle.append({"pagina": p["n"], "clase": p["clase"], "veredicto": AMARILLO,
                            "hallazgos": ["NO VERIFICABLE: el .md no tiene anclas [[p. N]]"], "metricas": {}})
        if all(p["clase"] == "texto" for p in oraculo):
            # Sin anclas no se certifica por página, pero una pérdida sigue siendo pérdida.
            todo = {"n": 0, "texto": "\n".join(p["texto"] for p in oraculo), "clase": "texto"}
            global_ = evaluar_pagina(todo, texto_md, retiros, cobertura_min, desactivar)
            if global_["veredicto"] == ROJO:
                errores_doc.extend(f"(documento completo) {h}" for h in global_["hallazgos"])
    else:
        if "G1" not in desactivar:
            for n in sorted(paginas_md):
                if not 1 <= n <= n_paginas:
                    errores_doc.append(f"G1 ancla [[p. {n}]] no existe en el original ({n_paginas} páginas)")
        for p in oraculo:
            detalle.append(evaluar_pagina(p, paginas_md.get(p["n"]), retiros_por_pag.get(p["n"], []),
                                          cobertura_min, desactivar))

    rojos = sum(d["veredicto"] == ROJO for d in detalle) + (1 if errores_doc else 0)
    amarillos = sum(d["veredicto"] == AMARILLO for d in detalle)
    if rojos:
        veredicto, codigo = ROJO, 1
    elif amarillos:
        veredicto, codigo = AMARILLO, 3
    else:
        veredicto, codigo = VERDE, 0

    return {
        "gate": "pdf2ctx/gate_fidelidad", "version": VERSION,
        "pdf": str(ruta_pdf), "sha256_pdf": sha256(ruta_pdf),
        "md": str(ruta_md), "sha256_md": sha256(ruta_md),
        "paginas": n_paginas, "veredicto": veredicto, "codigo_salida": codigo,
        "resumen": {"verde": sum(d["veredicto"] == VERDE for d in detalle),
                    "amarillo": amarillos,
                    "rojo": sum(d["veredicto"] == ROJO for d in detalle)},
        "clases_oraculo": dict(Counter(p["clase"] for p in oraculo)),
        "errores_documento": errores_doc, "avisos": avisos,
        "retiros_declarados": retiros,
        "eficiencia": residuo_repetido(paginas_md),
        "desactivados": sorted(desactivar),
        "detalle": detalle,
    }


# ───────────────────────── CLI ─────────────────────────

def _imprimir(informe: dict) -> None:
    if informe["desactivados"]:
        print(f"⚠️  GATE SABOTEADO: chequeos desactivados {informe['desactivados']} — solo para probar la suite.")
    for d in informe["detalle"]:
        if d["veredicto"] != VERDE:
            print(f"p. {d['pagina']:>4} {d['veredicto']} [{d['clase']}] " + " | ".join(d["hallazgos"]))
    for e in informe["errores_documento"]:
        print(f"doc    {ROJO} {e}")
    for a in informe["avisos"]:
        print(f"aviso  {a}")
    r, ef = informe["resumen"], informe["eficiencia"]
    print(f"\n{informe['veredicto']} {Path(informe['pdf']).name}: {informe['paginas']} págs · "
          f"{r['verde']} 🟢 · {r['amarillo']} 🟡 · {r['rojo']} 🔴 · G3 residuo repetido "
          f"{ef.get('residuo_pct')}% · código {informe['codigo_salida']}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Gate de fidelidad VVCA para PDF → Markdown (pdf2ctx).")
    sub = ap.add_subparsers(dest="comando", required=True)

    m = sub.add_parser("minimo", help="PDF → .md mínimo (capa de texto + anclas [[p. N]])")
    m.add_argument("pdf")
    m.add_argument("-o", "--salida", required=True)

    v = sub.add_parser("verificar", help="Compara un .md contra el PDF original")
    v.add_argument("pdf")
    v.add_argument("md")
    v.add_argument("--retiros", help="JSON con retiros declarados [{pagina, texto, motivo}]")
    v.add_argument("--informe", help="Ruta del informe JSON")
    v.add_argument("--cobertura-min", type=float, default=COBERTURA_MIN)
    v.add_argument("--desactivar", default="", help="Solo para probar la suite: p. ej. G2")

    args = ap.parse_args()
    if args.comando == "minimo":
        n = exportar_minimo(args.pdf, args.salida)
        print(f"{args.salida}: {n} páginas con ancla")
        return 0

    desactivar = frozenset(x.strip().upper() for x in args.desactivar.split(",") if x.strip())
    informe = verificar(args.pdf, args.md, args.retiros, args.cobertura_min, desactivar)
    if args.informe:
        Path(args.informe).write_text(json.dumps(informe, ensure_ascii=False, indent=2), encoding="utf-8")
    _imprimir(informe)
    return informe["codigo_salida"]


if __name__ == "__main__":
    sys.exit(main())
