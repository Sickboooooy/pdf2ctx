# -*- coding: utf-8 -*-
"""Regresión HTTP de correcciones humanas sobre un OCR simulado que confunde O y cero."""
import io
import json
import sys
import time
from pathlib import Path

CARPETA = Path(__file__).resolve().parent
sys.path.insert(0, str(CARPETA))
import curaduria
from probar_banco import Banco, Imitador, entorno_prueba, json_de
from probar_ocr_local import fixture
from docx import Document

RESULTADOS = []
ORIGINAL = "MONTO $1 ,380.OO"
CORREGIDO = "MONTO $1,380.00"


def comprobar(nombre, condicion):
    RESULTADOS.append({"comprobacion": nombre, "pasa": bool(condicion)})
    print(f"{'✅' if condicion else '❌'} {nombre}", flush=True)


def probar(banco, imitador, trabajo):
    pdf = fixture()
    estado, _, cuerpo = banco.post("/api/subir?nombre=correccion-demo.pdf", pdf, "application/pdf")
    vista = json_de(cuerpo)
    comprobar("se carga el documento sintético", estado == 200)
    ruta = "/api/doc/" + vista["id"]
    doc = trabajo / "servidor" / vista["id"]
    trans = {"pagina": 2, "regiones": [{"region": [70, 320, 540, 560], "modo": "region", "local": True,
             "texto": "FOLIO 4471\n" + ORIGINAL + "\nTexto dentro de imagen.",
             "metodo": "ocr/simulado", "modelo_respuesta": "OCR simulado para regresión"}]}
    (doc / "transcripciones").mkdir()
    archivo = doc / "transcripciones/2.json"
    archivo.write_text(json.dumps(trans, ensure_ascii=False), encoding="utf-8")
    copias = {p: (doc / p).read_bytes() for p in ("original.pdf", "documento.md", "informe.json", "transcripciones/2.json")}
    revision = ruta + "/curaduria/2/0"
    banco.post_json(revision, {"decision": "cotejado"})
    comprobar("cotejar la región no incorpora un importe incierto", CORREGIDO not in banco.get(ruta + "/exportar/txt")[2].decode("utf-8"))
    datos = {"decision": "corregir", "indice_linea": 1, "original": ORIGINAL, "texto": CORREGIDO, "confirmado": True}
    comprobar("corregir requiere confirmación de cotejo", banco.post_json(revision, {**datos, "confirmado": False})[0] == 400)
    comprobar("una línea de origen distinta se rechaza", banco.post_json(revision, {**datos, "original": "otra lectura"})[0] == 409)
    for valor in (-1, True, None):
        comprobar(f"índice inválido {valor!r} se rechaza", banco.post_json(revision, {**datos, "indice_linea": valor})[0] == 400)
    for texto in ("", "Monto\n1380", "Monto\x00"):
        comprobar("texto vacío, multilínea o con control se rechaza", banco.post_json(revision, {**datos, "texto": texto})[0] == 400)
    estado, _, cuerpo = banco.post_json(revision, datos)
    vista = json_de(cuerpo)
    comprobar("la corrección cotejada se guarda", estado == 200)
    linea = next(e for e in vista["curaduria"]["registro"] if e.get("pagina") == 2 and e.get("indice_linea") == 1)
    comprobar("se incorpora exactamente el importe cotejado con su lectura original", linea["linea"] == CORREGIDO
              and linea["linea_original"] == ORIGINAL and linea["decision"] == "incorporar")
    comprobar("la corrección humana sigue en ámbar, sin certificar el gate", "ámbar" in linea["estado"]
              and "no verificado" in linea["estado"] and vista["informe"]["codigo_salida"] == 3)
    comprobar("el PDF, Markdown, informe y OCR original permanecen idénticos", all((doc / p).read_bytes() == v for p, v in copias.items()))
    historial = [json.loads(x) for x in (doc / "curaduria_historial.jsonl").read_text(encoding="utf-8").splitlines()]
    correccion = historial[-1]["despues"]["correcciones"]["1"]
    comprobar("el historial registra origen, valor corregido, actor, fecha y hash", correccion["original"] == ORIGINAL
              and correccion["texto"] == CORREGIDO and correccion["actor"] == "revisión humana"
              and correccion["fecha_utc"] and correccion["sha256_transcripcion"] == curaduria.huella_transcripcion(trans["regiones"][0]))
    for formato in ("md", "txt", "html", "docx", "gdocs", "json"):
        estado, _, cuerpo = banco.get(ruta + "/exportar/" + formato)
        if formato in ("docx", "gdocs"):
            documento = Document(io.BytesIO(cuerpo))
            texto = "\n".join(p.text for p in documento.paragraphs)
            pagina, por_pagina = 0, {}
            for p in documento.paragraphs:
                if p.style.name == "Marca de página":
                    pagina += 1
                por_pagina.setdefault(pagina, []).append(p.text)
            comprobar(f"{formato}: el importe aparece una vez en la página 2", "\n".join(por_pagina[2]).count(CORREGIDO) == 1)
        else:
            texto = cuerpo.decode("utf-8")
        comprobar(f"{formato}: exporta el importe corregido", estado == 200 and CORREGIDO in texto)
        if formato != "json":
            comprobar(f"{formato}: evita el OCR errado y mantiene la etiqueta", ORIGINAL not in texto and "NO VERIFICADO" in texto)
        if formato == "md":
            comprobar("el consolidado conserva su inversa exacta", curaduria.quitar_bloques(texto).encode("utf-8") == copias["documento.md"])
        if formato == "json":
            comprobar("la evidencia conserva ambas versiones", ORIGINAL in texto and '"correccion_humana"' in texto)
    banco.post_json(revision, {"decision": "cotejado"})
    comprobar("cotejar de nuevo conserva la corrección", CORREGIDO in banco.get(ruta + "/exportar/txt")[2].decode("utf-8"))
    banco.post_json(revision, {"decision": "rechazado"})
    comprobar("rechazar la región excluye también la corrección", CORREGIDO not in banco.get(ruta + "/exportar/txt")[2].decode("utf-8"))
    banco.post_json(revision, {"decision": "cotejado"})
    comprobar("reincorporar conserva el valor humano", CORREGIDO in banco.get(ruta + "/exportar/txt")[2].decode("utf-8"))
    banco.post_json(revision, {"decision": "pendiente"})
    comprobar("deshacer devuelve la abstención y conserva el historial", CORREGIDO not in banco.get(ruta + "/exportar/txt")[2].decode("utf-8")
              and '"corregir"' in (doc / "curaduria_historial.jsonl").read_text(encoding="utf-8"))
    banco.post_json(revision, datos)
    trans["regiones"][0]["texto"] += "\nLa transcripción cambió."
    archivo.write_text(json.dumps(trans), encoding="utf-8")
    vista = json_de(banco.get(ruta)[2])
    comprobar("una corrección no se aplica a otra versión del OCR", any(e.get("correccion_obsoleta")
              and e["decision"] == "abstener" for e in vista["curaduria"]["registro"])
              and CORREGIDO not in banco.get(ruta + "/exportar/txt")[2].decode("utf-8"))
    comprobar("no hubo llamadas a Anthropic", not imitador.peticiones)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    trabajo = CARPETA / "trabajo" / ("_probar_correcciones_" + str(time.time_ns()))
    trabajo.mkdir(parents=True)
    imitador, banco = Imitador(), None
    try:
        banco = Banco(trabajo, entorno_prueba(imitador.url), "servidor")
        probar(banco, imitador, trabajo)
    except Exception as e:
        comprobar(f"la suite terminó: {type(e).__name__}: {e}", False)
    finally:
        if banco:
            banco.cerrar()
        imitador.cerrar()
    fallas = sum(not r["pasa"] for r in RESULTADOS)
    (trabajo / "resumen.json").write_text(json.dumps({"comprobaciones": RESULTADOS, "fallas": fallas}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{len(RESULTADOS) - fallas} pasan · {fallas} fallan · evidencia: {trabajo}")
    return int(bool(fallas))


if __name__ == "__main__":
    sys.exit(main())
