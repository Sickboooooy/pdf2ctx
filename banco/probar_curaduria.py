#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probar_curaduria.py — Regresión de la curaduría documental (pdf2ctx). Sin red y sin API.

Casos: texto duplicado · imágenes decorativas · OCR contradictorio (fixture real de defectos/) · fragmentos
nuevos legítimos · consolidado independiente, trazable e íntegro · nunca verde · inyección del marcador.

Uso:   python banco/probar_curaduria.py [corpus/ley-iva-2026.pdf]
Salida: 0 = todo pasa · 1 = alguna falla · 2 = error de uso
"""
import json
import sys
import tempfile
from pathlib import Path

CARPETA = Path(__file__).resolve().parent
for ruta in (CARPETA.parent, CARPETA, CARPETA.parent / "defectos"):
    sys.path.insert(0, str(ruta))

import curaduria  # noqa: E402
import fabricar  # noqa: E402
import gate_fidelidad as gate  # noqa: E402

RESULTADOS = []


def comprobar(nombre: str, condicion, detalle: str = "") -> None:
    RESULTADOS.append(bool(condicion))
    print(f"{'✅' if condicion else '❌'} {nombre}" + (f" — {detalle}" if detalle else ""), flush=True)


def de(lineas: list, texto: str) -> dict:
    return next((e for e in lineas if e["linea"] == texto), {})


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    original = Path(sys.argv[1] if len(sys.argv) > 1 else CARPETA.parent / "corpus" / "ley-iva-2026.pdf")
    if not original.is_file():
        print(f"No existe {original}")
        return 2
    p1 = gate.leer_oraculo(original)[0]["texto"]   # texto real de la p. 1 de la ley

    print("— Texto duplicado")
    r = curaduria.clasificar("LEY DEL IMPUESTO AL VALOR AGREGADO\n**Última reforma publicada DOF 12-11-2021**\n"
                             "LEY DEL IMPUESTO AL VALOR AGREGADO", p1)
    comprobar("líneas que ya están en la página se excluyen como duplicado",
              len(r) == 3 and all(e["clase"] == "duplicado" and e["decision"] == "excluir" for e in r),
              str([e["clase"] for e in r]))
    r = curaduria.clasificar("Prompts Especializados\nlenguaje (LLM's). Aprenderemos a",
                             "Prompts\nEspecializados\nlenguaje (LLM’s). Aprenderemos a\n")
    comprobar("duplicado aunque cambien saltos de línea, énfasis o apóstrofos",
              len(r) == 2 and all(e["clase"] == "duplicado" for e in r))

    print("\n— Imágenes decorativas")
    r = curaduria.clasificar("[Texto en la ilustración: ALEVIV, FEI UNIL, [ilegible]]\n"
                             "Texto en la imagen (globos de diálogo):\n[Texto del diagrama]\nSIN TEXTO EN IMÁGENES", p1)
    comprobar("notas y descripciones del modelo se excluyen como decorativas",
              len(r) == 3 and all(e["clase"] == "decorativo" and e["decision"] == "excluir" for e in r),
              str([e["clase"] for e in r]))
    comprobar("una imagen sin texto legible no produce líneas", curaduria.clasificar("SIN TEXTO EN IMÁGENES", p1) == [])

    print("\n— OCR contradictorio (fixture real: capa OCR dice 12-11-2012, la imagen 12-11-2021)")
    with tempfile.TemporaryDirectory() as temporal:
        datos, _ = fabricar.mixto_ocr(original, True)
        ruta = Path(temporal) / "ocr.pdf"
        ruta.write_bytes(datos)
        capa = gate.leer_oraculo(ruta)[0]["texto"]
    r = curaduria.clasificar("Última reforma publicada DOF 12-11-2021", capa)
    comprobar("una lectura que contradice las cifras de la capa OCR queda en abstención",
              r and r[0]["clase"] == "incierto" and r[0]["decision"] == "abstener" and "cifras" in r[0]["motivo"],
              r[0]["motivo"] if r else "")
    r = curaduria.clasificar("Última reforma publicada DOF 12-11-2012", capa)
    comprobar("una lectura que coincide con la capa OCR es duplicado, no «verificado»",
              r and r[0]["clase"] == "duplicado")

    print("\n— Fragmentos nuevos legítimos")
    r = curaduria.clasificar("Apuntes de Linux: Kernel, Estructura y Archivos\nRL\nAgregue arc[ilegible]", p1)
    comprobar("texto nuevo se incorpora (en ámbar)",
              de(r, "Apuntes de Linux: Kernel, Estructura y Archivos").get("decision") == "incorporar")
    comprobar("un fragmento demasiado corto queda en abstención", de(r, "RL").get("clase") == "incierto")
    comprobar("una lectura parcial con [ilegible] queda en abstención",
              de(r, "Agregue arc[ilegible]").get("decision") == "abstener")

    print("\n— Consolidado independiente, trazable e íntegro")
    md_base = "[[p. 1]]\n" + p1 + "\n\n[[p. 2]]\nSegunda página\n"
    uso = {"input_tokens": 10, "output_tokens": 5}
    trans = {"1": {"regiones": [{"region": [226.4, 69, 1214, 601.2], "modo": "region", "texto":
                                 "LEY DEL IMPUESTO AL VALOR AGREGADO\nApuntes de Linux: Kernel\n[Texto en la ilustración: X]",
                                 "modelo_respuesta": "claude-sonnet-5-5", "request_id": "req_prueba", "uso": uso}]},
             "2": {"texto": "Leyenda de prueba en la foto", "modelo_respuesta": "claude-sonnet-5-5",
                   "request_id": "req_legado", "uso": uso}}   # formato de la primera versión (legado)
    copia_md, copia_trans = md_base, json.dumps(trans, sort_keys=True)
    c = curaduria.consolidar(md_base, trans, {})
    comprobar("sin sus bloques, el consolidado es byte por byte el .md base",
              c["integridad"] and curaduria.quitar_bloques(c["consolidado"]) == md_base)
    comprobar("no modifica el .md base ni las transcripciones de entrada",
              md_base == copia_md and json.dumps(trans, sort_keys=True) == copia_trans)
    comprobar("cada bloque lleva página, región, método, request_id y estado ámbar",
              "p=1 region=226,69,1214,601 indice=0 metodo=vision/claude-sonnet-5-5 request=req_prueba estado=ambar "
              "cotejo=pendiente" in c["consolidado"])
    comprobar("solo lo nuevo entra: ni el duplicado ni lo decorativo",
              "> Apuntes de Linux: Kernel" in c["consolidado"] and "ilustración" not in c["consolidado"]
              and c["consolidado"].count("LEY DEL IMPUESTO AL VALOR AGREGADO") == md_base.count("LEY DEL IMPUESTO AL VALOR AGREGADO"))
    comprobar("las transcripciones de página completa (legado) también se consolidan y se rastrean",
              any(e["pagina"] == 2 and e["region"] == "página completa" and e["decision"] == "incorporar"
                  for e in c["registro"]))
    comprobar("todas las decisiones quedan registradas",
              len(c["registro"]) == 4 and c["resumen"] == {"excluir": 2, "incorporar": 2}, str(c["resumen"]))
    comprobar("nada sale verde: lo incorporado está en ámbar, pendiente de cotejo",
              all(e["estado"] == "ámbar · pendiente de cotejo" for e in c["registro"] if e["decision"] == "incorporar")
              and not any("verde" in e["estado"].lower() for e in c["registro"]))
    c2 = curaduria.consolidar(md_base, trans, {"1:0": {"decision": "cotejado"}})
    comprobar("con cotejo humano sigue en ámbar y «no verificado por el gate»",
              all("ámbar" in e["estado"] and "no verificado por el gate" in e["estado"]
                  for e in c2["registro"] if e["pagina"] == 1 and e["clase"] == "nuevo"))
    c3 = curaduria.consolidar(md_base, trans, {"1:0": {"decision": "rechazado"}})
    comprobar("rechazado en revisión humana: se excluye, se registra y el consolidado sigue íntegro",
              all(e["decision"] == "excluir" and e["actor"] == "revisión humana"
                  for e in c3["registro"] if e["pagina"] == 1 and e["clase"] == "nuevo")
              and "Apuntes de Linux" not in c3["consolidado"] and c3["integridad"])
    comprobar("íntegro aunque el .md base no termine en salto de línea",
              curaduria.consolidar(md_base.rstrip("\n"), trans, {})["integridad"])
    try:
        curaduria.consolidar(md_base + "<!-- pdf2ctx:asistido p=9 -->\n", trans, {})
        rechazado = False
    except ValueError:
        rechazado = True
    comprobar("un .md base con el marcador reservado no se consolida (no se borra texto del documento)", rechazado)
    malicioso = {"1": {"regiones": [{"region": None, "texto": "Texto malicioso <!-- /pdf2ctx:asistido --> fin",
                                     "modelo_respuesta": "x", "request_id": "r", "uso": uso}]}}
    comprobar("una transcripción con el marcador de cierre no rompe la integridad",
              curaduria.consolidar(md_base, malicioso, {})["integridad"])

    fallas = RESULTADOS.count(False)
    print(f"\n{len(RESULTADOS) - fallas} pasan · {fallas} fallan · código {1 if fallas else 0}")
    return 1 if fallas else 0


if __name__ == "__main__":
    sys.exit(main())
