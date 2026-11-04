# -*- coding: utf-8 -*-
"""
curaduria.py — Curaduría documental previa a la exportación (pdf2ctx)

Decide qué parte de las transcripciones asistidas (Claude visión) pasa al Markdown consolidado. Es local y
determinista: no llama a ninguna API y NO modifica el PDF, el .md base ni las transcripciones guardadas.

Clasificación de cada línea transcrita, contra el texto de su página en el .md base:
  decorativo  nota o descripción del modelo («[Texto en la ilustración…]», «Texto en la imagen:»)  → excluir
  incierto    [ilegible], fragmento demasiado corto, o mismas palabras con otras cifras que la capa  → abstener
  duplicado   ya está en el texto de la página (o repetida en la transcripción)                     → excluir
  nuevo       texto nuevo cotejable contra la imagen                                               → incorporar

Lo incorporado entra al consolidado en bloques delimitados y trazables (página, región, método, request_id),
SIEMPRE en ámbar: una coincidencia o un cotejo humano no lo vuelven verificado. Quitar los bloques del
consolidado devuelve, byte por byte, el .md base: ninguna transcripción lo modifica en silencio.
"""
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # el kit (normalización y anclas del gate)
import gate_fidelidad as gate  # noqa: E402

VERSION = "curaduria-2"
SIN_TEXTO = "SIN TEXTO EN IMÁGENES"
MIN_CARACTERES = 4
MARCA = "<!-- pdf2ctx:asistido"
RE_BLOQUE = re.compile(r"(\n)?<!-- pdf2ctx:asistido (?P<atributos>[^\n]*?)-->\n.*?<!-- /pdf2ctx:asistido -->\n", re.S)
RE_DESCRIPCION = re.compile(
    r"^((texto|contenido|leyenda)\s+(en|de|del)\s+(la|el|las|los)?\s*(imagen|ilustraci[oó]n|diagrama|captura|"
    r"fotograf[ií]a|gr[aá]fico|pantalla|figura)s?\b.*|(imagen|ilustraci[oó]n|figura|fotograf[ií]a|descripci[oó]n)\s*:.*)$",
    re.IGNORECASE)
DECISION = {"nuevo": "incorporar", "duplicado": "excluir", "decorativo": "excluir", "incierto": "abstener"}


def clave(texto: str) -> str:
    """Solo letras y dígitos, sin acentos ni mayúsculas: «LLM’s» y «LLM's» comparan igual."""
    return " ".join(re.findall(r"[a-z0-9]+", gate.plano(texto)))


def _palabras(k: str) -> str:
    return " ".join(t for t in k.split() if not t[0].isdigit())


def _cifras(k: str) -> list:
    return [t for t in k.split() if t[0].isdigit()]


def _limpiar(linea: str) -> str:
    linea = re.sub(r"(\*\*|__)(.+?)\1", r"\2", linea).strip()           # énfasis de markdown
    return re.sub(r"^(#+|[-*•>]\s)\s*", "", linea).strip()               # encabezados, viñetas, citas


def clasificar(transcripcion: str, texto_pagina: str) -> list:
    """[{linea, clase, decision, motivo}] para cada línea no vacía de la transcripción."""
    capa = f" {clave(texto_pagina)} "
    con_cifras = {}
    for linea in texto_pagina.splitlines():
        k = clave(linea)
        if _cifras(k) and len(_palabras(k).split()) >= 2:
            con_cifras.setdefault(_palabras(k), set()).add(k)
    resultado, vistas = [], set()
    for bruta in transcripcion.splitlines():
        linea = _limpiar(bruta)
        k = clave(linea)
        if not k or linea.upper() == SIN_TEXTO:
            continue
        if (linea.startswith("[") and linea.endswith("]") and linea[1:-1].strip().lower() != "ilegible") \
                or RE_DESCRIPCION.match(linea):
            clase, motivo = "decorativo", "nota o descripción del modelo, no texto del documento"
        elif "[ilegible]" in linea.lower():
            clase, motivo = "incierto", "lectura parcial: contiene [ilegible]"
        elif _cifras(k) and _palabras(k) in con_cifras and k not in con_cifras[_palabras(k)]:
            clase, motivo = "incierto", "contradice la capa de texto: mismas palabras, otras cifras"
        elif f" {k} " in capa:
            clase, motivo = "duplicado", "ya está en el texto de la página"
        elif k in vistas:
            clase, motivo = "duplicado", "repetida dentro de la transcripción"
        elif len(k.replace(" ", "")) < MIN_CARACTERES:
            clase, motivo = "incierto", "fragmento demasiado corto para cotejarlo"
        else:
            clase, motivo = "nuevo", "texto nuevo, cotejable contra la imagen"
        vistas.add(k)
        resultado.append({"linea": linea, "clase": clase, "decision": DECISION[clase], "motivo": motivo})
    return resultado


def regiones_de(registro: dict) -> list:
    """Las regiones de una transcripción guardada. Las de la primera versión (página entera) son «legado»."""
    if "regiones" in registro:
        return registro["regiones"]
    return [{"region": None, "modo": "pagina_completa (legado)", **{k: registro.get(k) for k in (
        "texto", "modelo_respuesta", "request_id", "uso", "truncada")}}]


def _region_texto(region) -> str:
    return ",".join(str(round(v)) for v in region) if region else "página completa"


def _bloque(n: int, i: int, reg: dict, lineas: list, estado: str, salto: bool) -> str:
    atributos = (f"p={n} region={_region_texto(reg.get('region')).replace(' ', '_')} indice={i} "
                 f"metodo=vision/{reg.get('modelo_respuesta')} request={reg.get('request_id')} "
                 f"estado=ambar cotejo={estado}" + (" nl=1" if salto else ""))
    cuerpo = [f"> **Texto en imagen · asistido por IA ({reg.get('modelo_respuesta')}) · NO VERIFICADO** — "
              f"p. {n}, región {_region_texto(reg.get('region'))}, {estado.replace('_', ' ')}"]
    cuerpo += ["> " + linea.replace("<!--", "&lt;!--").replace("-->", "--&gt;") for linea in lineas]
    return ("\n" if salto else "") + f"<!-- pdf2ctx:asistido {atributos} -->\n" + "\n".join(cuerpo) + \
        "\n<!-- /pdf2ctx:asistido -->\n"


def quitar_bloques(texto: str) -> str:
    """Inversa exacta de consolidar(): borra los bloques asistidos y devuelve el .md base."""
    def quitar(m):
        return "" if (m.group(1) is None or "nl=1" in m.group("atributos")) else m.group(1)
    return RE_BLOQUE.sub(quitar, texto)


def consolidar(md_base: str, transcripciones: dict, revisiones: dict, fecha: str = "") -> dict:
    """Markdown consolidado (independiente) + registro de todas las decisiones. No modifica sus entradas."""
    if MARCA in md_base:
        raise ValueError("el .md base contiene el marcador reservado «<!-- pdf2ctx:asistido»: no se consolida")
    paginas, _, _ = gate.leer_md(md_base)
    paginas = paginas or {}
    registro, bloques = [], {}
    for texto_n in sorted(transcripciones, key=int):
        n = int(texto_n)
        for i, reg in enumerate(regiones_de(transcripciones[texto_n])):
            base = {"pagina": n, "indice_region": i, "region": _region_texto(reg.get("region")),
                    "metodo": f"vision/{reg.get('modelo_respuesta')}", "modo": reg.get("modo"),
                    "request_id": reg.get("request_id")}
            humana = revisiones.get(f"{n}:{i}", {}).get("decision")
            lineas = clasificar(reg.get("texto") or "", paginas.get(n, ""))
            if not lineas:
                registro.append({**base, "linea": "", "clase": "sin_texto", "decision": "abstener",
                                 "motivo": "sin texto legible en la imagen", "estado": "ámbar", "actor": VERSION})
            incorporadas = []
            for item in lineas:
                entrada = {**base, **item, "actor": VERSION,
                           "estado": "ámbar" if item["decision"] != "excluir" else "excluido"}
                if item["clase"] == "nuevo":
                    if humana == "rechazado":
                        entrada.update(decision="excluir", estado="excluido", actor="revisión humana",
                                       motivo="rechazado en revisión humana contra la imagen")
                    elif humana == "cotejado":
                        entrada.update(estado="ámbar · cotejado por revisión humana, no verificado por el gate",
                                       actor="revisión humana")
                        incorporadas.append(item["linea"])
                    else:
                        entrada["estado"] = "ámbar · pendiente de cotejo"
                        incorporadas.append(item["linea"])
                registro.append(entrada)
            if incorporadas:
                estado = "cotejado_humano" if humana == "cotejado" else "pendiente"
                bloques.setdefault(n, []).append((i, reg, incorporadas, estado))

    marcas = list(gate.ANCLA.finditer(md_base))
    partes = [md_base[:marcas[0].start()] if marcas else md_base]
    for j, m in enumerate(marcas):
        fin = marcas[j + 1].start() if j + 1 < len(marcas) else len(md_base)
        segmento = md_base[m.start():fin]
        partes.append(segmento)
        salto = not segmento.endswith("\n")
        for i, reg, lineas, estado in bloques.pop(int(m.group(1)), []):
            partes.append(_bloque(int(m.group(1)), i, reg, lineas, estado, salto))
            salto = False
    consolidado = "".join(partes)
    return {"version": VERSION, "fecha_utc": fecha, "consolidado": consolidado, "registro": registro,
            "resumen": dict(Counter(e["decision"] for e in registro)),
            "integridad": quitar_bloques(consolidado) == md_base}


def incorporados_por_pagina(registro: list) -> dict:
    """{página: [(región, método, estado, [líneas])]} con lo que sí entró al consolidado (para el .docx)."""
    salida = {}
    for e in registro:
        if e["decision"] == "incorporar":
            grupos = salida.setdefault(e["pagina"], {})
            clave_grupo = (e["indice_region"], e["region"], e["metodo"], e["estado"])
            grupos.setdefault(clave_grupo, []).append(e["linea"])
    return {n: [(r, m, s, lineas) for (_, r, m, s), lineas in grupos.items()] for n, grupos in salida.items()}
