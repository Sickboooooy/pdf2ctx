# -*- coding: utf-8 -*-
"""Regresión de OCR REAL en Windows, sin API: escaneo, imagen insertada y OCR contradictorio."""
import hashlib
import io
import json
import re
import sys
import time
from pathlib import Path

CARPETA = Path(__file__).resolve().parent
sys.path.insert(0, str(CARPETA))
sys.path.insert(0, str(CARPETA.parent / "defectos"))
import curaduria
import fabricar
import ocr_local
from probar_banco import Banco, Imitador, entorno_prueba, json_de
from docx import Document
from PIL import Image, ImageDraw, ImageFont

RESULTADOS = []


def comprobar(nombre, condicion):
    RESULTADOS.append({"comprobacion": nombre, "pasa": bool(condicion)})
    print(f"{'✅' if condicion else '❌'} {nombre}", flush=True)


def fixture() -> bytes:
    fuente = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 52)
    imagen = Image.new("RGB", (1800, 2400), "white")
    dibujo = ImageDraw.Draw(imagen)
    for i, texto in enumerate(["DOCUMENTO SINTETICO DE PRUEBA", "LECTURA OCR LOCAL", "Expediente 1234/2026",
                               "Fecha 19/11/2026", "Monto $45,600.00", "Artículo 17, fracción II."]):
        dibujo.text((120, 150 + 130 * i), texto, font=fuente, fill="black")
    pdf = fabricar.PdfSintetico()
    pdf.pagina()
    pdf.imagen(0, 0, 612, 792, imagen)
    pdf.pagina()
    pdf.texto(40, 740, "TEXTO NATIVO QUE NO DEBE REPETIR EL OCR", tamano=12)
    recorte = Image.new("RGB", (1400, 650), "white")
    d = ImageDraw.Draw(recorte)
    d.rectangle((8, 8, 1390, 640), outline="black", width=4)
    d.text((80, 120), "FOLIO 4471", font=fuente, fill="black")
    d.text((80, 280), "MONTO $1,380.00", font=fuente, fill="black")
    d.text((80, 440), "Texto dentro de imagen.", font=fuente, fill="black")
    pdf.imagen(70, 320, 470, 240, recorte)
    pdf.pagina()
    pdf.imagen(0, 0, 612, 792, imagen)
    pdf.texto(40, 700, "Fecha 19/11/2012", modo=3)
    return pdf.bytes()


def probar(banco, imitador, trabajo):
    comprobar("una confusión de O y cero en un importe queda en abstención",
              curaduria.clasificar("Monto $45,600.OO", "")[0]["decision"] == "abstener")
    comprobar("una fecha distinta con etiqueta de una palabra queda en abstención",
              curaduria.clasificar("Fecha 19/11/2026", "Fecha 19/11/2012")[0]["decision"] == "abstener")
    comprobar("el ordinal jurídico 1o. no se confunde con una cifra defectuosa",
              curaduria.clasificar("Artículo 1o.", "")[0]["decision"] == "incorporar")
    config = json_de(banco.get("/api/config")[2])
    comprobar("OCR español disponible sin clave de API", config["ocr_local"]["disponible"]
              and config["ocr_local"]["idioma"].startswith("es-") and not config["transcripcion"]["disponible"])
    pdf = fixture()
    (trabajo / "demo-ocr-local.pdf").write_bytes(pdf)
    estado, _, cuerpo = banco.post("/api/subir?nombre=demo-ocr-local.pdf", pdf, "application/pdf")
    antes = json_de(cuerpo)
    comprobar("PDF con escaneo, imagen insertada y OCR previo se carga", estado == 200 and antes["informe"]["paginas"] == 3)
    ident = antes["id"]
    ruta = f"/api/doc/{ident}"
    base = banco.get(ruta + "/descargar/md")[2]
    informe_base = banco.get(ruta + "/descargar/informe")[2]
    comprobar("OCR local sin token se rechaza", banco.post(ruta + "/ocr/1", b"{}", token="")[0] == 403)
    comprobar("página inválida se rechaza", banco.post_json(ruta + "/ocr/xyz", {})[0] == 400)
    comprobar("página inexistente se rechaza", banco.post_json(ruta + "/ocr/4", {})[0] == 404)
    for n in (1, 2, 3):
        estado, _, cuerpo = banco.post_json(ruta + f"/ocr/{n}", {})
        lectura = json_de(cuerpo)
        comprobar(f"p. {n}: el motor OCR real responde sin confirmación de envío", estado == 200 and lectura.get("local"))
        if estado != 200:
            raise RuntimeError(json_de(cuerpo).get("error", "falló OCR"))
        reg = lectura["regiones"][0]
        comprobar(f"p. {n}: evidencia de imagen y palabras con posición", re.fullmatch("[0-9a-f]{64}", reg["sha256_imagen"])
                  and reg["lineas"] and reg["lineas"][0]["palabras"][0]["rect"] and reg["idioma"].startswith("es-"))
        if n == 1:
            comprobar("escaneo: recupera expediente y fecha visibles", "1234/2026" in reg["texto"] and "19/11/2026" in reg["texto"])
        if n == 2:
            comprobar("imagen insertada: lee solo su recorte", reg["region"] is not None and "4471" in reg["texto"]
                      and "TEXTO NATIVO" not in reg["texto"])
    despues = json_de(banco.get(ruta)[2])
    comprobar("el Markdown base permanece idéntico", banco.get(ruta + "/descargar/md")[2] == base)
    comprobar("el informe y el veredicto permanecen idénticos", banco.get(ruta + "/descargar/informe")[2] == informe_base
              and despues["informe"]["codigo_salida"] == 3)
    comprobar("el PDF original conserva su hash", hashlib.sha256((trabajo / "servidor" / ident / "original.pdf").read_bytes()).hexdigest()
              == hashlib.sha256(pdf).hexdigest())
    registro = despues["curaduria"]["registro"]
    comprobar("la fecha que contradice el OCR previo queda en abstención", any(e["pagina"] == 3 and "19/11/2026" in e["linea"]
              and e["decision"] == "abstener" for e in registro))
    comprobar("lo incorporado conserva origen OCR y estado ámbar", all(e["metodo"].startswith("ocr/") and "ámbar" in e["estado"]
              for e in registro if e["decision"] == "incorporar"))
    guardada = trabajo / "servidor" / ident / "transcripciones" / "2.json"
    copia = guardada.read_bytes()
    comprobar("repetir OCR no sobrescribe una lectura guardada", banco.post_json(ruta + "/ocr/2", {})[0] == 409 and guardada.read_bytes() == copia)
    for formato in ("md", "txt", "html", "docx", "gdocs", "json"):
        estado, _, cuerpo = banco.get(ruta + "/exportar/" + formato)
        if formato in ("docx", "gdocs"):
            texto = "\n".join(p.text for p in Document(io.BytesIO(cuerpo)).paragraphs)
        else:
            texto = cuerpo.decode("utf-8")
        comprobar(f"exportación {formato}: incluye OCR local y procedencia", estado == 200 and "4471" in texto
                  and ("OCR local" in texto or "ocr/windows-media" in texto))
        if formato != "json":
            comprobar(f"exportación {formato}: no presenta la lectura como verificada", "NO VERIFICADO" in texto)
        if formato == "md":
            comprobar("consolidado reversible byte por byte", curaduria.quitar_bloques(texto).encode("utf-8") == base)
    banco.post_json(ruta + "/curaduria/2/0", {"decision": "rechazado"})
    comprobar("rechazo humano excluye el texto del recorte de la exportación", "4471" not in banco.get(ruta + "/exportar/txt")[2].decode("utf-8"))
    banco.post_json(ruta + "/curaduria/2/0", {"decision": "cotejado"})
    cotejada = json_de(banco.get(ruta)[2])
    comprobar("cotejo humano conserva el ámbar y la lectura original", cotejada["informe"]["codigo_salida"] == 3
              and guardada.read_bytes() == copia and any(e["pagina"] == 2 and "cotejado" in e["estado"]
                  for e in cotejada["curaduria"]["registro"]))
    comprobar("OCR y exportación no llaman a la API de Anthropic", not imitador.peticiones)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not ocr_local.estado()["disponible"]:
        print(ocr_local.estado()["causa"])
        return 3
    trabajo = CARPETA / "trabajo" / ("_probar_ocr_local_" + str(time.time_ns()))
    trabajo.mkdir(parents=True)
    imitador = Imitador()
    banco = None
    try:
        banco = Banco(trabajo, entorno_prueba(imitador.url), "servidor")
        probar(banco, imitador, trabajo)
    except Exception as e:
        comprobar(f"la prueba completa terminó: {type(e).__name__}: {e}", False)
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
