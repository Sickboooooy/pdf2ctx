#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probar_defectos.py — Suite de PDFs defectuosos de pdf2ctx (fuzz + regresión)

Pasa cada fixture por el sistema real (banco: convertidor «minimo» + gate_fidelidad + .docx),
cada caso en un proceso hijo con tiempo límite, y lo compara contra su verdad de referencia.

Dos fidelidades, siempre por separado
  CAPA TEXTUAL  ¿el .md coincide con la capa de texto del PDF?  → lo que mide el gate (🟢 🟡 🔴)
  VISUAL        ¿el .md dice lo que se VE en la página?          → la verdad de referencia del fixture

Estado de cada comprobación
  PASA              cumple lo deseado
  FALLA ESPERADA    no cumple y es una vulnerabilidad conocida y declarada (Vn). NO cuenta como pase.
  FALLA             no cumple y no estaba declarada: regresión o defecto nuevo
  PASA INESPERADO   cumple aunque se declaró vulnerable: la vulnerabilidad cambió; actualiza CASOS
  NO APLICA         la condición no se dio (p. ej., el archivo dañado fue rechazado)

Salida: 0 = ninguna FALLA ni PASA INESPERADO (las fallas esperadas se reportan, no se ocultan)
        1 = alguna FALLA o PASA INESPERADO · 2 = error de uso (original ausente o alterado)

Uso
  python defectos/probar_defectos.py [corpus/ley-iva-2026.pdf] [--solo A1,B2]
"""
import argparse
import datetime
import hashlib
import importlib.metadata
import io
import json
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

CARPETA = Path(__file__).resolve().parent
RAIZ = CARPETA.parent
sys.path.insert(0, str(RAIZ))                # el kit
sys.path.insert(0, str(RAIZ / "banco"))      # el banco (sistema bajo prueba)
sys.path.insert(0, str(CARPETA))             # fabricar.py

import fabricar  # noqa: E402
import gate_fidelidad as gate  # noqa: E402
import pypdfium2 as pdfium  # noqa: E402

VERDE, AMARILLO, ROJO = gate.VERDE, gate.AMARILLO, gate.ROJO
TIEMPO_LIMITE = 120  # segundos por caso

# ───────────────────────── vulnerabilidades conocidas ─────────────────────────

VULNERABILIDADES = {
    "V1": ("visual", "Texto no visible certificado como 🟢",
           "El gate mide contra la capa de texto. Texto invisible (modo 3, blanco sobre blanco, microtexto, "
           "fuera de la página o tapado) entra al .md, sale 🟢 y llega al .docx como texto válido, "
           "incluida una instrucción maliciosa."),
    "V2": ("textual", "Permutación de cifras dentro de una página no detectada",
           "G2 compara las cifras de cada página como multiconjunto: intercambiar celdas, columnas o fechas "
           "dentro de la misma página conserva el conjunto y el .md alterado sale 🟢."),
    "V3": ("visual", "Estructura visual de tablas perdida",
           "Si la capa de texto sigue otro orden que la vista (tabla dibujada por columnas), el .md pierde la "
           "asociación renglón–columna y el gate da 🟢, porque el .md sí es fiel a la capa."),
    "V4": ("robustez", "Daño del archivo sin aviso",
           "Un PDF truncado o alterado que PDFium logra abrir se convierte y se certifica sin avisar del daño, "
           "incluso cuando el texto extraído quedó corrompido (fechas partidas, páginas con la tercera parte de "
           "sus palabras, operadores crudos del flujo de contenido): el oráculo es el mismo archivo dañado."),
    "V5": ("estructura", "Folios impresos no verificados",
           "Páginas faltantes o duplicadas en el propio PDF no se señalan aunque los folios impresos "
           "(«N de 128») lo evidencian: el oráculo es el mismo archivo defectuoso."),
    "V6": ("estructura", "Orientación de página no registrada",
           "La rotación de página (/Rotate) no queda en el .md, el informe ni el .docx."),
    "V7": ("visual", "Anotaciones omitidas sin aviso",
           "Notas de firma y sellos como anotaciones no entran al .md; el sistema no avisa ni registra su "
           "localización."),
    "V8": ("visual", "Contenido en imágenes no señalado",
           "Una imagen con contenido (sello con folio) en una página con texto no se señala; la página sale 🟢."),
    "V9": ("visual", "Discrepancia entre capa OCR e imagen no localizada",
           "Sin OCR propio, ante una capa OCR que contradice la imagen el sistema solo se abstiene (🟡); "
           "no señala qué dato discrepa."),
    "V10": ("textual", "Extracción alterada en páginas giradas",
            "Con /Rotate 90° o 180°, PDFium devuelve el texto de la página en otro orden y parte cifras "
            "(«12-11-2021» → «2021 - 11 - 21», «3 de 128» → «128 de 3»). El oráculo del gate usa la misma "
            "extracción, así que el .md alterado sale 🟢. Caso concreto del límite 2 del kit."),
}
# Todas se previeron en el análisis previo a la suite, salvo las que la suite misma descubrió:
HALLADAS_POR_LA_SUITE = {"V10": "corrida del 4-XI-2026 (F1_paginas_giradas)"}

# Fuzz con V4: los únicos archivos dañados que el sistema acepta y certifica 🟢 sin aviso. Los demás se
# rechazan (400) o salen 🟡. Los fixtures son reproducibles byte a byte, así que el conjunto es estable;
# si otro caso empieza a salir 🟢, aparece como FALLA (regresión).
FUZZ_CON_V4 = {"fuzz_bytes_01", "fuzz_bytes_08", "fuzz_sin_cola", "fuzz_truncado_99"}

# ───────────────────────── mutaciones del .md (el convertidor «se equivoca») ─────────────────────────

RE_FECHA = re.compile(r"\b\d{2}-\d{2}-\d{4}\b")


def intercambiar(texto: str, a: str, b: str) -> str:
    """Intercambia la primera aparición de a con la primera de b."""
    i, j = texto.index(a), texto.index(b)
    if i > j:
        (i, a), (j, b) = (j, b), (i, a)
    return texto[:i] + b + texto[i + len(a):j] + a + texto[j + len(b):]


def tramos(md: str) -> list:
    """[(página, inicio, fin)] de cada ancla [[p. N]] con su cuerpo."""
    marcas = list(gate.ANCLA.finditer(md))
    return [(int(m.group(1)), m.start(), marcas[k + 1].start() if k + 1 < len(marcas) else len(md))
            for k, m in enumerate(marcas)]


def m_intercambiar_columnas(md, verdad):
    for fila in verdad["tabla"][1:]:
        md = intercambiar(md, fila[1], fila[2])
    return md, "se intercambiaron las columnas «Cuota 2025» y «Cuota 2026» en cada renglón"


def m_intercambiar_renglones(md, verdad):
    a, b = verdad["tabla"][1][2], verdad["tabla"][3][2]
    return intercambiar(md, a, b), f"se intercambiaron las cuotas 2026 de dos renglones: «{a}» ↔ «{b}»"


def m_alterar_cifra(md, verdad):
    return md.replace("1,380.00", "1,830.00", 1), "«1,380.00» → «1,830.00» (control: cifra cambiada)"


def m_intercambiar_fechas(md, verdad):
    for n, inicio, fin in tramos(md):
        fechas = list(dict.fromkeys(RE_FECHA.findall(md[inicio:fin])))
        if len(fechas) >= 2:
            cuerpo = intercambiar(md[inicio:fin], fechas[0], fechas[1])
            return md[:inicio] + cuerpo + md[fin:], f"p. {n}: fechas intercambiadas «{fechas[0]}» ↔ «{fechas[1]}»"
    raise ValueError("ninguna página tiene dos fechas distintas")


def m_duplicar_ancla(md, verdad):
    n, inicio, fin = next(t for t in tramos(md) if t[0] == 5)
    return md[:fin] + md[inicio:fin] + md[fin:], "la p. 5 aparece dos veces en el .md"


def m_omitir_pagina(md, verdad):
    n, inicio, fin = next(t for t in tramos(md) if t[0] == 10)
    return md[:inicio] + md[fin:], "se borró la p. 10 del .md (ancla y texto)"


MUTACIONES = {"intercambiar_columnas": m_intercambiar_columnas, "intercambiar_renglones": m_intercambiar_renglones,
              "alterar_cifra": m_alterar_cifra, "intercambiar_fechas": m_intercambiar_fechas,
              "duplicar_ancla": m_duplicar_ancla, "omitir_pagina": m_omitir_pagina}


# ───────────────────────── comprobaciones ─────────────────────────
# Cada una recibe (resultado del sistema, verdad del fixture) y devuelve (True | False | None, detalle).
# None = NO APLICA.

def plano(texto: str) -> str:
    return gate.plano(texto or "")


def aceptado(r) -> bool:
    return r["estado"] == "ok"


def pagina_de(r, n):
    return next((d for d in r["informe"]["detalle"] if d["pagina"] == n), {})


def c_falla_controlada(r, v):
    if r["estado"] in ("ok", "rechazo"):
        return True, "aceptado (200)" if aceptado(r) else f"rechazado ({r['http']}): {r['error'][:90]}"
    return False, f"{r['estado']}: {r.get('error', '')[:160]}"


def c_dano_avisado(r, v):
    if not aceptado(r):
        return None, "rechazado: no hay conversión que avisar"
    inf = r["informe"]
    avisado = inf["veredicto"] != VERDE or inf["avisos"] or inf["errores_documento"]
    texto = ("texto idéntico al original" if r.get("md_sistema_sha256") == v.get("sha256_md_original")
             else "texto DISTINTO del original")
    return bool(avisado), (f"aceptado: {inf['veredicto']} código {inf['codigo_salida']}, "
                           f"{inf['paginas']} de {v['paginas']} págs, sin aviso de daño; {texto}" if not avisado
                           else f"aceptado con {inf['veredicto']} código {inf['codigo_salida']}; {texto}")


def c_verde_si_fiel(r, v):
    if not aceptado(r):
        return False, f"rechazado: {r['error']}"
    inf = r["informe"]
    return inf["veredicto"] == VERDE, f"{inf['veredicto']} código {inf['codigo_salida']}"


def c_alteracion_detectada(r, v):
    if not aceptado(r):
        return False, f"rechazado: {r['error']}"
    inf = r["informe"]
    hallazgos = [h for d in inf["detalle"] for h in d["hallazgos"]] + inf["errores_documento"]
    return inf["codigo_salida"] == 1, f"{r.get('mutacion', '')} → {inf['veredicto']} código {inf['codigo_salida']}" + (
        f" · {hallazgos[0][:90]}" if hallazgos else "")


def c_anclas_en_orden(r, v):
    if not aceptado(r):
        return False, f"rechazado: {r['error']}"
    anclas = [int(n) for n in gate.ANCLA.findall(r["md"])]
    return anclas == list(range(1, v["paginas"] + 1)), f"{len(anclas)} anclas para {v['paginas']} páginas"


def c_texto_rotado_intacto(r, v):
    paginas, _, _ = gate.leer_md(r["md"])
    distintas = []
    for n, texto in v["texto_paginas"].items():
        girado = plano(paginas.get(int(n), ""))
        if girado != texto:
            faltan = gate.invariantes(texto) - gate.invariantes(girado)
            distintas.append(f"p. {n} ({v['rotaciones'][n]}°): cifras que faltan {gate.muestra(faltan, 3) or '—'}")
    return not distintas, ("idéntico al original en " + ", ".join(f"p. {n}" for n in v["texto_paginas"])
                           if not distintas else "; ".join(distintas) + f" · gate {r['informe']['veredicto']}")


def c_orientacion_registrada(r, v):
    rotadas = [int(n) for n in v["rotaciones"]]
    evidencia = plano(json.dumps([pagina_de(r, n) for n in rotadas] + r["informe"]["avisos"], ensure_ascii=False))
    registrada = any(p in evidencia for p in ("rotacion", "orientacion", "girad", "rotate"))
    return registrada, f"pp. {rotadas} giradas {list(v['rotaciones'].values())}°: " + (
        "registrado" if registrada else "ni el .md ni el informe lo registran")


def c_folios_verificados(r, v):
    inf = r["informe"]
    senalado = inf["veredicto"] != VERDE or inf["avisos"] or inf["errores_documento"]
    defecto = v.get("evidencia", "")
    return bool(senalado), f"{defecto} → {inf['veredicto']} código {inf['codigo_salida']}" + (
        "" if senalado else ", sin aviso")


def c_visible_en_md(r, v):
    if not aceptado(r):
        return False, f"rechazado: {r['error']}"
    faltan = [s for s in v["visible"] if plano(s) not in plano(r["md"])]
    return not faltan, f"{len(v['visible']) - len(faltan)}/{len(v['visible'])} textos visibles en el .md" + (
        f"; faltan: {faltan[:2]}" if faltan else "")


def textos_ocultos_en_md(r, v) -> list:
    return [h for h in v["oculto"] if plano(h) in plano(r["md"])]


def c_oculto_no_certificado(r, v):
    presentes = textos_ocultos_en_md(r, v)
    inf = r["informe"]
    if not presentes:
        return True, "el .md no contiene texto oculto"
    if inf["veredicto"] == VERDE:
        return False, f"el .md contiene «{presentes[0][:50]}…» (no visible) y el gate da 🟢"
    return True, f"texto oculto presente, pero el veredicto es {inf['veredicto']}"


def fila_en_md(fila: list, md: str) -> bool:
    celdas = [plano(c) for c in fila]
    for linea in md.splitlines():
        linea, posicion = plano(linea), 0
        for celda in celdas:
            posicion = linea.find(celda, posicion)
            if posicion < 0:
                break
            posicion += len(celda)
        else:
            return True
    return False


def c_filas_preservadas(r, v):
    rotas = [fila[0] for fila in v["tabla"] if not fila_en_md(fila, r["md"])]
    return not rotas, (f"{len(v['tabla'])} renglones juntos y en orden" if not rotas
                       else f"{len(rotas)}/{len(v['tabla'])} renglones con las celdas separadas en el .md "
                            f"(dibujo {v['orden_de_dibujo']}); gate {r['informe']['veredicto']}")


def c_nunca_verde(r, v):
    if not aceptado(r):
        return True, "rechazado"
    inf = r["informe"]
    return inf["veredicto"] != VERDE, f"{inf['veredicto']} código {inf['codigo_salida']} · clases {inf['clases_oraculo']}"


def c_escaneo_no_verificable(r, v):
    d = pagina_de(r, v["pagina_escaneada"])
    return d.get("veredicto") == AMARILLO, f"p. {v['pagina_escaneada']} {d.get('veredicto')} [{d.get('clase')}]: " + \
        " | ".join(d.get("hallazgos", []))[:100]


def c_discrepancia_localizada(r, v):
    ocr = v["ocr"]
    d = pagina_de(r, ocr["pagina"])
    texto = " ".join(d.get("hallazgos", []))
    localizada = ocr["capa"] in texto or ocr["imagen"] in texto
    return localizada, f"capa «{ocr['capa']}» vs imagen «{ocr['imagen']}» → {d.get('veredicto')}: {texto[:90]}"


def c_anotaciones_conservadas(r, v):
    evidencia = plano(r["md"] + json.dumps(r["informe"], ensure_ascii=False))
    perdidas = [a["tipo"] for a in v["anotaciones"] if plano(a["contenido"]) not in evidencia]
    return not perdidas, ("conservadas" if not perdidas else
                          f"se pierden sin aviso: {perdidas} (gate {r['informe']['veredicto']})")


def c_imagen_senalada(r, v):
    sin_senal = []
    for elemento in v["elementos_no_textuales"]:
        d = pagina_de(r, elemento["pagina"])
        if d.get("veredicto") == VERDE and not d.get("hallazgos"):
            sin_senal.append(elemento["contenido_visible"])
    return not sin_senal, ("señalada" if not sin_senal else
                           f"imagen con «{sin_senal[0]}» no señalada; la página sale 🟢")


def c_docx_sin_defecto_como_valido(r, v):
    if not aceptado(r):
        return True, "rechazado: no hay .docx"
    docx = r["docx"]
    if not docx["permitido"]:
        return True, f"bloqueado: {docx['causa'][:80]}"
    if "NO VERIFICADAS" in docx.get("encabezado", ""):
        return True, f"exportado con aviso: {docx['encabezado'][:80]}"
    return False, "el .docx se exporta como lectura válida, sin aviso"


def c_docx_conserva_visible(r, v):
    docx = r.get("docx", {})
    if not docx.get("permitido"):
        return False, f"sin .docx: {docx.get('causa', r.get('error', ''))}"
    faltan = [s for s in v["visible"] if plano(s) not in plano(docx["texto"])]
    return not faltan, "texto visible literal en el .docx (como dato)" if not faltan else f"faltan: {faltan[:2]}"


CHEQUEOS = {
    "falla_controlada": ("robustez", "termina sin caerse, colgarse ni error interno (200 o 4xx)", c_falla_controlada),
    "dano_avisado": ("robustez", "si acepta un archivo dañado, lo avisa o no lo certifica 🟢", c_dano_avisado),
    "verde_si_fiel": ("textual", "un .md fiel a la capa de texto sale 🟢", c_verde_si_fiel),
    "alteracion_detectada": ("textual", "un .md alterado respecto de la capa de texto sale 🔴", c_alteracion_detectada),
    "texto_rotado_intacto": ("textual", "el texto de las páginas giradas es idéntico al original", c_texto_rotado_intacto),
    "anclas_en_orden": ("estructura", "una ancla por página, en orden", c_anclas_en_orden),
    "orientacion_registrada": ("estructura", "la rotación de página queda registrada", c_orientacion_registrada),
    "folios_verificados": ("estructura", "páginas faltantes o duplicadas se señalan (hay folios impresos)",
                           c_folios_verificados),
    "visible_en_md": ("visual", "todo el texto visible está en el .md", c_visible_en_md),
    "oculto_no_certificado": ("visual", "texto no visible no sale 🟢 sin señalarse", c_oculto_no_certificado),
    "filas_preservadas": ("visual", "cada renglón de la tabla conserva sus celdas juntas y en orden",
                          c_filas_preservadas),
    "nunca_verde": ("visual", "sin oráculo confiable no se certifica 🟢", c_nunca_verde),
    "escaneo_no_verificable": ("visual", "la página escaneada sale 🟡 NO VERIFICABLE", c_escaneo_no_verificable),
    "discrepancia_localizada": ("visual", "la discrepancia capa OCR vs imagen se señala en concreto",
                                c_discrepancia_localizada),
    "anotaciones_conservadas": ("visual", "firma y sello anotados se conservan o se avisan con su localización",
                                c_anotaciones_conservadas),
    "imagen_senalada": ("visual", "una imagen con contenido en página con texto se señala", c_imagen_senalada),
    "docx_sin_defecto_como_valido": ("salida", "el .docx no presenta como válido un contenido defectuoso",
                                     c_docx_sin_defecto_como_valido),
    "docx_conserva_visible": ("salida", "el .docx conserva literal el texto visible (dato, no orden)",
                              c_docx_conserva_visible),
}

# ───────────────────────── casos ─────────────────────────

OCULTO = [("visible_en_md", None), ("oculto_no_certificado", "V1"), ("docx_sin_defecto_como_valido", "V1")]
PERMUTACION = [("alteracion_detectada", "V2"), ("docx_sin_defecto_como_valido", "V2")]


def caso(ident, fixture, chequeos, mutacion=None):
    return {"id": ident, "fixture": fixture, "mutacion": mutacion, "chequeos": chequeos}


def construir_casos(manifiesto: dict) -> list:
    casos = [
        # A · texto invisible e instrucciones maliciosas (prioridad)
        caso("A0_control_sentencia", "sintetico_control",
             [("verde_si_fiel", None), ("visible_en_md", None), ("oculto_no_certificado", None)]),
        caso("A1_oculto_modo_invisible", "sintetico_oculto_modo_invisible", OCULTO),
        caso("A2_oculto_blanco_sobre_blanco", "sintetico_oculto_blanco_sobre_blanco", OCULTO),
        caso("A3_oculto_microtexto", "sintetico_oculto_microtexto", OCULTO),
        caso("A4_oculto_fuera_de_pagina", "sintetico_oculto_fuera_de_pagina", OCULTO),
        caso("A5_oculto_tapado", "sintetico_oculto_tapado", OCULTO),
        caso("A6_publico_oculto_modo_invisible", "publico_oculto", OCULTO),
        caso("A7_instruccion_visible_es_dato", "sintetico_instruccion_visible",
             [("verde_si_fiel", None), ("visible_en_md", None), ("docx_conserva_visible", None)]),
        # B · tablas y orden de columnas (prioridad)
        caso("B1_tabla_por_renglones", "sintetico_tabla_renglones",
             [("verde_si_fiel", None), ("filas_preservadas", None)]),
        caso("B2_md_columnas_intercambiadas", "sintetico_tabla_renglones", PERMUTACION, "intercambiar_columnas"),
        caso("B3_tabla_dibujada_por_columnas", "sintetico_tabla_columnas",
             [("verde_si_fiel", None), ("filas_preservadas", "V3"), ("docx_sin_defecto_como_valido", "V3")]),
        caso("B4_md_renglones_intercambiados", "sintetico_tabla_renglones", PERMUTACION, "intercambiar_renglones"),
        caso("B5_md_publico_fechas_intercambiadas", "publico_original", PERMUTACION, "intercambiar_fechas"),
        caso("B6_control_md_cifra_alterada", "sintetico_tabla_renglones",
             [("alteracion_detectada", None), ("docx_sin_defecto_como_valido", None)], "alterar_cifra"),
        # D · escaneo borroso e inclinado
        caso("D1_escaneo_borroso_inclinado", "mixto_escaneo_borroso",
             [("nunca_verde", None), ("escaneo_no_verificable", None)]),
        # E · capa OCR contra imagen
        caso("E1_ocr_contradictorio", "mixto_ocr_contradictorio",
             [("nunca_verde", None), ("discrepancia_localizada", "V9")]),
        caso("E2_control_ocr_consistente", "mixto_ocr_consistente", [("nunca_verde", None)]),
        # F · páginas giradas o invertidas
        caso("F1_paginas_giradas", "publico_rotado",
             [("anclas_en_orden", None), ("texto_rotado_intacto", "V10"), ("orientacion_registrada", "V6")]),
        # G · páginas faltantes o duplicadas
        caso("G1_pdf_sin_p10", "publico_sin_pagina", [("anclas_en_orden", None), ("folios_verificados", "V5")]),
        caso("G2_pdf_p10_duplicada", "publico_pagina_duplicada",
             [("anclas_en_orden", None), ("folios_verificados", "V5")]),
        caso("G3_md_ancla_duplicada", "publico_original", [("alteracion_detectada", None)], "duplicar_ancla"),
        caso("G4_md_sin_p10", "publico_original", [("alteracion_detectada", None)], "omitir_pagina"),
        # H · firmas, sellos y anotaciones
        caso("H1_anotaciones_firma_y_sello", "publico_anotaciones", [("anotaciones_conservadas", "V7")]),
        caso("H2_sello_en_imagen", "sintetico_sello_imagen", [("visible_en_md", None), ("imagen_senalada", "V8")]),
    ]
    # C · fuzz: archivos truncados o corruptos
    for ident in sorted(k for k in manifiesto if k.startswith("fuzz_")):
        casos.append(caso("C_" + ident[len("fuzz_"):], ident,
                          [("falla_controlada", None), ("dano_avisado", "V4" if ident in FUZZ_CON_V4 else None)]))
    return casos


# ───────────────────────── proceso hijo: el sistema bajo prueba ─────────────────────────

def hijo(ruta_entrada: Path) -> int:
    """Corre un caso aislado: banco.subir_pdf (convertidor + gate), mutación opcional, .docx."""
    from docx import Document
    import servidor_local as banco
    entrada = json.loads(ruta_entrada.read_text(encoding="utf-8"))
    banco.TRABAJO = Path(entrada["trabajo"])
    banco.TRABAJO.mkdir(parents=True, exist_ok=True)
    pdf = Path(entrada["pdf"])
    try:
        vista = banco.subir_pdf(pdf.read_bytes(), {"nombre": [pdf.name]})
        carpeta = banco.TRABAJO / vista["id"]
        resultado = {"estado": "ok", "http": 200, "md_sistema_sha256": hashlib.sha256(
            (carpeta / "documento.md").read_bytes()).hexdigest()}
        if entrada["mutacion"]:
            verdad = json.loads(Path(entrada["verdad"]).read_text(encoding="utf-8"))
            md, descripcion = MUTACIONES[entrada["mutacion"]](banco.leer_md(carpeta), verdad)
            resultado["mutacion"] = descripcion
            vista = banco.subir_md_externo(carpeta, md.encode("utf-8"), {"nombre": [entrada["mutacion"] + ".md"]})
        resultado["informe"] = vista["informe"]
        resultado["md"] = banco.leer_md(carpeta)
        try:
            datos, _ = banco.exportar_docx(carpeta)
            doc = Document(io.BytesIO(datos))
            resultado["docx"] = {"permitido": True, "texto": "\n".join(p.text for p in doc.paragraphs),
                                 "encabezado": " ".join(p.text for p in doc.sections[0].header.paragraphs)}
        except banco.ErrorBanco as e:
            resultado["docx"] = {"permitido": False, "causa": e.mensaje}
    except banco.ErrorBanco as e:
        resultado = {"estado": "rechazo", "http": e.codigo, "error": e.mensaje}
    Path(entrada["salida"]).write_text(json.dumps(resultado, ensure_ascii=False), encoding="utf-8")
    return 0


def correr_caso(entrada: dict, carpeta_caso: Path) -> dict:
    """Lanza el hijo con tiempo límite. Una señal, un cuelgue o una excepción no controlada quedan registrados."""
    ruta_entrada = carpeta_caso / "entrada.json"
    ruta_entrada.write_text(json.dumps(entrada, ensure_ascii=False, indent=2), encoding="utf-8")
    inicio = time.time()
    try:
        proceso = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--hijo", str(ruta_entrada)],
                                 capture_output=True, text=True, timeout=TIEMPO_LIMITE)
    except subprocess.TimeoutExpired:
        return {"estado": "colgado", "error": f"más de {TIEMPO_LIMITE} s", "segundos": TIEMPO_LIMITE}
    if proceso.stderr.strip():
        (carpeta_caso / "stderr.txt").write_text(proceso.stderr, encoding="utf-8")
    segundos = round(time.time() - inicio, 2)
    if proceso.returncode < 0:
        return {"estado": "senal", "error": f"terminó por la señal {-proceso.returncode}", "segundos": segundos}
    salida = Path(entrada["salida"])
    if proceso.returncode != 0 or not salida.is_file():
        ultima = (proceso.stderr.strip().splitlines() or ["sin salida"])[-1]
        return {"estado": "excepcion", "error": f"código {proceso.returncode}: {ultima}", "segundos": segundos}
    resultado = json.loads(salida.read_text(encoding="utf-8"))
    resultado["segundos"] = segundos
    return resultado


# ───────────────────────── instrumentos de evidencia ─────────────────────────

def sha256_archivo(ruta: Path) -> str:
    return hashlib.sha256(Path(ruta).read_bytes()).hexdigest()


def sonda_tinta(ruta_pdf: Path, indice: int = 0, escala: float = 2.0, umbral: int = 200):
    """Instrumento sin OCR: caracteres de la capa de texto que no dejan tinta en la página renderizada.
    Solo mide presencia de tinta en la caja de cada carácter; no lee la imagen."""
    pdf = pdfium.PdfDocument(str(ruta_pdf))
    pagina = pdf[indice]
    try:
        gris = pagina.render(scale=escala).to_pil().convert("L")
        alto = pagina.get_height()
        capa = pagina.get_textpage()
        total, sin_tinta = 0, []
        for i in range(capa.count_chars()):
            caracter = capa.get_text_range(i, 1)
            if not caracter.strip():
                continue
            total += 1
            izq, abajo, der, arriba = capa.get_charbox(i)
            caja = (max(int(izq * escala), 0), max(int((alto - arriba) * escala), 0),
                    min(int(der * escala) + 1, gris.width), min(int((alto - abajo) * escala) + 1, gris.height))
            vacia = caja[2] <= caja[0] or caja[3] <= caja[1]
            if vacia or gris.crop(caja).getextrema()[0] >= umbral:
                sin_tinta.append(caracter)
        capa.close()
    finally:
        pagina.close()
        pdf.close()
    return total, "".join(sin_tinta)


def fidelidad_visual(r: dict, v: dict) -> list:
    """Problemas frente a lo VISIBLE según la verdad de referencia (vacío = fiel a lo visible)."""
    if r["estado"] != "ok":
        return [f"no se produjo .md ({r['estado']})"]
    md, problemas = r["md"], []
    ocultos = textos_ocultos_en_md(r, v)
    if ocultos:
        problemas.append("contiene texto no visible")
    faltan = [s for s in v["visible"] if plano(s) not in plano(md)]
    if faltan:
        problemas.append(f"faltan {len(faltan)} textos visibles")
    if "tabla" in v and not all(fila_en_md(f, md) for f in v["tabla"]):
        problemas.append("pierde la asociación renglón–columna")
    if r.get("mutacion"):
        problemas.append("el .md fue alterado: " + r["mutacion"])
    if "ocr" in v and plano(v["ocr"]["capa"]) in plano(md):
        problemas.append(f"dice «{v['ocr']['capa']}» donde la imagen dice «{v['ocr']['imagen']}»")
    if "anotaciones" in v and not all(plano(a["contenido"]) in plano(md) for a in v["anotaciones"]):
        problemas.append("omite las anotaciones (firma, sello)")
    for e in v.get("elementos_no_textuales", []):
        if plano(e["contenido_visible"]) not in plano(md):
            problemas.append(f"omite el contenido de la imagen «{e['contenido_visible']}»")
    if "pagina_escaneada" in v:
        problemas.append(f"la p. {v['pagina_escaneada']} escaneada no tiene texto en el .md")
    if v.get("paginas_faltantes"):
        problemas.append(f"el PDF omite la p. {v['paginas_faltantes']}")
    if v.get("paginas_duplicadas"):
        problemas.append(f"el PDF duplica la p. {v['paginas_duplicadas']}")
    if v.get("rotaciones"):
        problemas.append("no registra la orientación de las páginas giradas")
        paginas, _, _ = gate.leer_md(md)
        alteradas = [n for n, t in v["texto_paginas"].items() if plano(paginas.get(int(n), "")) != t]
        if alteradas:
            problemas.append(f"texto reordenado y cifras partidas en pp. {', '.join(alteradas)}")
    if v.get("dano"):
        problemas.append(f"archivo dañado ({v['dano']})")
        if r.get("md_sistema_sha256") != v.get("sha256_md_original"):
            problemas.append("el texto extraído difiere del original sano")
    return problemas


# ───────────────────────── corrida ─────────────────────────

def clasificar(cumple, vulnerabilidad) -> str:
    if cumple is None:
        return "NO APLICA"
    if cumple:
        return "PASA INESPERADO" if vulnerabilidad else "PASA"
    return "FALLA ESPERADA" if vulnerabilidad else "FALLA"


ICONO = {"PASA": "✅", "FALLA ESPERADA": "🟠", "FALLA": "❌", "PASA INESPERADO": "❗", "NO APLICA": "➖"}


def git(*argumentos) -> str:
    try:
        return subprocess.run(["git", *argumentos], cwd=RAIZ, capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Suite de PDFs defectuosos de pdf2ctx.")
    ap.add_argument("--hijo", help=argparse.SUPPRESS)
    ap.add_argument("original", nargs="?", default=str(RAIZ / "corpus" / "ley-iva-2026.pdf"),
                    help="PDF público original (se verifica por SHA-256; nunca se escribe)")
    ap.add_argument("--salida", default=str(CARPETA / "resultados"))
    ap.add_argument("--solo", default="", help="Prefijos de caso separados por coma (p. ej. A,B2)")
    args = ap.parse_args()
    if args.hijo:
        return hijo(Path(args.hijo))

    original = Path(args.original).resolve()
    if not original.is_file():
        print(f"No existe el original: {original}")
        return 2
    sha_antes = sha256_archivo(original)
    if sha_antes != fabricar.ORIGINAL_SHA256:
        print(f"El original no coincide con su SHA-256 esperado: {sha_antes[:16]}… ≠ {fabricar.ORIGINAL_SHA256[:16]}…")
        return 2

    corrida = time.strftime("%Y%m%d-%H%M%S")
    carpeta = Path(args.salida).resolve() / corrida
    fixtures = carpeta / "fixtures"
    print(f"probar_defectos.py · corrida {corrida} · evidencia en {carpeta}")
    manifiesto = fabricar.fabricar(original, fixtures)
    repeticion = fabricar.fabricar(original, carpeta / "_repeticion")
    distintos = [k for k in manifiesto if manifiesto[k]["sha256"] != repeticion[k]["sha256"]]
    shutil.rmtree(carpeta / "_repeticion", ignore_errors=True)
    # Referencia para el fuzz: el .md mínimo del original sin daño.
    gate.exportar_minimo(original, carpeta / "original_minimo.md")
    sha_md_original = sha256_archivo(carpeta / "original_minimo.md")
    for ident in manifiesto:
        if ident.startswith("fuzz_"):
            manifiesto[ident]["verdad"]["sha256_md_original"] = sha_md_original

    resultados = []

    def registrar(caso_id, nombre, vulnerabilidad, cumple, detalle, eje=None):
        estado = clasificar(cumple, vulnerabilidad)
        eje = eje or CHEQUEOS[nombre][0]
        resultados.append({"caso": caso_id, "comprobacion": nombre, "eje": eje, "vulnerabilidad": vulnerabilidad,
                           "estado": estado, "detalle": detalle})
        etiqueta = f" [{vulnerabilidad}]" if vulnerabilidad else ""
        print(f"  {ICONO[estado]} {estado:<15} {nombre}{etiqueta} — {detalle}", flush=True)

    print("\n— INSTRUMENTO")
    registrar("INSTRUMENTO", "fixtures_reproducibles", None, not distintos,
              f"{len(manifiesto)} fixtures con el mismo SHA-256 en dos fabricaciones" if not distintos
              else f"distintos: {distintos}", eje="instrumento")

    solo = [s.strip() for s in args.solo.split(",") if s.strip()]
    casos = [c for c in construir_casos(manifiesto) if not solo or any(c["id"].startswith(s) for s in solo)]
    visuales = []
    for c in casos:
        f = manifiesto[c["fixture"]]
        ruta_pdf = Path(f["archivo"]) if Path(f["archivo"]).is_absolute() else fixtures / f["archivo"]
        carpeta_caso = carpeta / "casos" / c["id"]
        carpeta_caso.mkdir(parents=True)
        ruta_verdad = carpeta_caso / "verdad.json"
        ruta_verdad.write_text(json.dumps(f["verdad"], ensure_ascii=False, indent=2), encoding="utf-8")
        r = correr_caso({"pdf": str(ruta_pdf), "verdad": str(ruta_verdad), "mutacion": c["mutacion"],
                         "trabajo": str(carpeta_caso / "trabajo"), "salida": str(carpeta_caso / "resultado.json")},
                        carpeta_caso)
        if r["estado"] == "ok":
            (carpeta_caso / "documento.md").write_text(r["md"], encoding="utf-8")
            (carpeta_caso / "informe.json").write_text(json.dumps(r["informe"], ensure_ascii=False, indent=2),
                                                       encoding="utf-8")
            if r["docx"].get("permitido"):
                (carpeta_caso / "docx.txt").write_text(r["docx"]["encabezado"] + "\n\n" + r["docx"]["texto"],
                                                       encoding="utf-8")
        shutil.rmtree(carpeta_caso / "trabajo", ignore_errors=True)  # copia del PDF; la evidencia ya quedó arriba

        textual = (f"{r['informe']['veredicto']} código {r['informe']['codigo_salida']}" if r["estado"] == "ok"
                   else f"{r['estado']} {r.get('http', '')}".strip())
        problemas = fidelidad_visual(r, f["verdad"])
        defecto = f"en el .md: {r['mutacion']}" if r.get("mutacion") else f["defecto"]
        visuales.append({"caso": c["id"], "fixture": c["fixture"], "defecto": defecto, "textual": textual,
                         "verde": r["estado"] == "ok" and r["informe"]["veredicto"] == VERDE,
                         "visual": problemas, "segundos": r.get("segundos")})
        print(f"\n— {c['id']} · {defecto} · capa textual: {textual} · visual: "
              + ("fiel" if not problemas else "; ".join(problemas)))
        for nombre, vulnerabilidad in c["chequeos"]:
            cumple, detalle = CHEQUEOS[nombre][2](r, f["verdad"]) if r["estado"] == "ok" or nombre in (
                "falla_controlada", "dano_avisado", "nunca_verde", "docx_sin_defecto_como_valido") \
                else (False, f"{r['estado']}: {r.get('error', '')[:120]}")
            registrar(c["id"], nombre, vulnerabilidad, cumple, detalle)

    print("\n— SONDA DE TINTA (instrumento sin OCR: caracteres de la capa que no dejan tinta)")
    sondas = []
    for ident in ("publico_original", "sintetico_control", "sintetico_instruccion_visible",
                  "sintetico_oculto_modo_invisible", "sintetico_oculto_blanco_sobre_blanco",
                  "sintetico_oculto_microtexto", "sintetico_oculto_fuera_de_pagina", "sintetico_oculto_tapado",
                  "publico_oculto"):
        f = manifiesto[ident]
        ruta_pdf = Path(f["archivo"]) if Path(f["archivo"]).is_absolute() else fixtures / f["archivo"]
        total, sin_tinta = sonda_tinta(ruta_pdf)
        sondas.append({"fixture": ident, "caracteres": total, "sin_tinta": len(sin_tinta), "muestra": sin_tinta[:60]})
        print(f"  {ident:<38} {len(sin_tinta):>4} de {total:>5} sin tinta  {sin_tinta[:50]}")

    sha_despues = sha256_archivo(original)
    print("\n— INSTRUMENTO")
    registrar("INSTRUMENTO", "original_inmutable", None, sha_antes == sha_despues == fabricar.ORIGINAL_SHA256,
              f"SHA-256 {sha_despues[:16]}… antes y después de la corrida", eje="instrumento")

    conteos = {estado: sum(r["estado"] == estado for r in resultados) for estado in ICONO}
    codigo = 1 if conteos["FALLA"] or conteos["PASA INESPERADO"] else 0
    resumen = {
        "fecha_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "corrida": corrida, "rama": git("rev-parse", "--abbrev-ref", "HEAD"), "commit": git("rev-parse", "HEAD"),
        "cambios_sin_commit": bool(git("status", "--porcelain")),
        "entorno": {"python": platform.python_version(), "sistema": platform.platform(),
                    "pypdfium2": importlib.metadata.version("pypdfium2"), "gate": gate.VERSION},
        "original": {"nombre": fabricar.ORIGINAL_NOMBRE, "ruta": str(original),
                     "sha256_esperado": fabricar.ORIGINAL_SHA256, "antes": sha_antes, "despues": sha_despues},
        "fixtures": {k: {"sha256": f["sha256"], "origen": f["origen"], "defecto": f["defecto"], "bytes": f["bytes"]}
                     for k, f in manifiesto.items()},
        "vulnerabilidades": {k: {"eje": e, "titulo": t, "descripcion": d,
                                 "origen": ("hallada por esta suite: " + HALLADAS_POR_LA_SUITE[k])
                                 if k in HALLADAS_POR_LA_SUITE else "prevista antes de la suite"}
                             for k, (e, t, d) in VULNERABILIDADES.items()},
        "fidelidades": visuales, "sondas": sondas, "resultados": resultados,
        "conteos": conteos, "codigo_salida": codigo,
    }
    ruta_resumen = carpeta / "resumen.json"
    ruta_resumen.write_text(json.dumps(resumen, ensure_ascii=False, indent=2), encoding="utf-8")
    informe = escribir_informe(resumen, sha256_archivo(ruta_resumen), carpeta)
    (carpeta / "INFORME_COBERTURA.md").write_text(informe, encoding="utf-8")
    if not solo:
        (CARPETA / "INFORME_COBERTURA.md").write_text(informe, encoding="utf-8")

    print(f"\n{conteos['PASA']} pasan · {conteos['FALLA ESPERADA']} fallas esperadas · {conteos['FALLA']} fallan · "
          f"{conteos['PASA INESPERADO']} pasan inesperadamente · {conteos['NO APLICA']} no aplican · código {codigo}")
    print(f"Evidencia: {carpeta}")
    return codigo


# ───────────────────────── informe de cobertura ─────────────────────────

def tabla(encabezados: list, filas: list) -> str:
    def celda(x):
        return str(x).replace("|", "\\|").replace("\n", " ")
    lineas = ["| " + " | ".join(encabezados) + " |", "|" + "---|" * len(encabezados)]
    lineas += ["| " + " | ".join(celda(x) for x in fila) + " |" for fila in filas]
    return "\n".join(lineas)


def escribir_informe(resumen: dict, sha_resumen: str, carpeta: Path) -> str:
    c, o = resumen["conteos"], resumen["original"]
    resultados = resumen["resultados"]
    try:
        evidencia = carpeta.relative_to(RAIZ)
    except ValueError:
        evidencia = carpeta
    partes = [
        "# Informe de cobertura · suite de PDFs defectuosos (pdf2ctx)",
        "",
        f"Corrida `{resumen['corrida']}` · {resumen['fecha_utc']} · rama `{resumen['rama']}` @ "
        f"`{resumen['commit'][:10]}`" + (" (con cambios sin commit)" if resumen["cambios_sin_commit"] else ""),
        f"Entorno: Python {resumen['entorno']['python']} · pypdfium2 {resumen['entorno']['pypdfium2']} · "
        f"gate {resumen['entorno']['gate']} · {resumen['entorno']['sistema']}",
        f"Evidencia: `{evidencia}/` · `resumen.json` SHA-256 `{sha_resumen}`",
        "Comando: `python defectos/probar_defectos.py corpus/ley-iva-2026.pdf`",
        "",
        "## Resumen",
        "",
        tabla(["PASA", "FALLA ESPERADA", "FALLA", "PASA INESPERADO", "NO APLICA", "código"],
              [[c["PASA"], c["FALLA ESPERADA"], c["FALLA"], c["PASA INESPERADO"], c["NO APLICA"],
                resumen["codigo_salida"]]]),
        "",
        "Las **fallas esperadas** son vulnerabilidades conocidas y declaradas: no cuentan como pase. "
        "Una FALLA no declarada o un PASA INESPERADO hacen salir 1.",
        "",
        "## Original inmutable",
        "",
        tabla(["documento", "SHA-256 esperado", "antes", "después"],
              [[o["nombre"], f"`{o['sha256_esperado']}`", "✅" if o["antes"] == o["sha256_esperado"] else "❌",
                "✅" if o["despues"] == o["sha256_esperado"] else "❌"]]),
        "",
        "## Dos fidelidades por caso",
        "",
        "**Capa textual** = lo que mide el gate (el `.md` contra la capa de texto del PDF). "
        "**Visual** = el `.md` contra lo que se ve en la página, según la verdad de referencia del fixture. "
        "La columna *brecha* marca los casos en que el sistema certifica 🟢 algo que no es fiel a lo visible.",
        "",
        tabla(["caso", "defecto", "capa textual (gate)", "fidelidad visual (verdad)", "brecha"],
              [[f["caso"], f["defecto"], f["textual"], "fiel" if not f["visual"] else "; ".join(f["visual"]),
                "⚠️ 🟢 no fiel a lo visible" if f["verde"] and f["visual"] else ""]
               for f in resumen["fidelidades"]]),
        "",
        "## Vulnerabilidades pendientes",
        "",
    ]
    filas = []
    for clave, vul in resumen["vulnerabilidades"].items():
        casos = sorted({r["caso"] for r in resultados if r["vulnerabilidad"] == clave and r["estado"] == "FALLA ESPERADA"})
        filas.append([clave, vul["titulo"], vul["eje"], vul["origen"], ", ".join(casos) or "—"])
    partes += [tabla(["id", "vulnerabilidad", "eje", "origen", "casos donde se manifestó"], filas), ""]
    for clave, vul in resumen["vulnerabilidades"].items():
        partes.append(f"- **{clave} · {vul['titulo']}.** {vul['descripcion']}")
    partes += ["", "## Resultados por eje", ""]
    for eje in ("instrumento", "textual", "visual", "estructura", "robustez", "salida"):
        del_eje = [r for r in resultados if r["eje"] == eje]
        if del_eje:
            partes += [f"### {eje}", "", tabla(["caso", "comprobación", "estado", "detalle"], [
                [r["caso"], r["comprobacion"] + (f" [{r['vulnerabilidad']}]" if r["vulnerabilidad"] else ""),
                 f"{ICONO[r['estado']]} {r['estado']}", r["detalle"]] for r in del_eje]), ""]
    partes += [
        "## Sonda de tinta (instrumento sin OCR)",
        "",
        "Para cada carácter de la capa de texto de la p. 1, mira si su caja deja tinta en la página renderizada. "
        "No lee la imagen: solo distingue texto dibujado de texto que no se ve. Es evidencia, no parte del sistema.",
        "",
        tabla(["fixture", "caracteres en la capa", "sin tinta", "muestra"],
              [[s["fixture"], s["caracteres"], s["sin_tinta"], s["muestra"]] for s in resumen["sondas"]]),
        "",
        "## Fixtures",
        "",
        tabla(["id", "origen", "defecto", "SHA-256", "bytes"],
              [[k, f["origen"], f["defecto"], f"`{f['sha256'][:16]}…`", f["bytes"]]
               for k, f in resumen["fixtures"].items()]),
        "",
        "## Fuera de cobertura en esta fase",
        "",
        "- Escaneos reales: se usó una página pública rasterizada, borrosa e inclinada.",
        "- Texto incierto **por palabra**: exige OCR con confianza; aquí solo se mide la abstención por página.",
        "- PDFs cifrados o con contraseña, formularios (AcroForm/XFA), capas opcionales (OCG).",
        "- Fuentes sin mapa Unicode (límite 2 del kit) y letras dibujadas como trazos vectoriales.",
        "- Validez criptográfica de firmas digitales (solo se prueba que su rastro visible se conserve).",
        "- La UI del banco en el navegador: esta suite usa las funciones del servidor, no el HTML.",
        "",
    ]
    return "\n".join(partes)


if __name__ == "__main__":
    sys.exit(main())
