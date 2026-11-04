#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probar_banco.py — El gate del banco de pruebas (pdf2ctx)

Arranca servidor_local.py en un puerto libre y lo prueba de punta a punta con un PDF REAL
que tenga cifras: anclas, re-verificación, dígito alterado, imagen PNG, .docx, seguridad
(Host, token, cuerpo no PDF, ids con "../") y medición de tokens.

Nunca llama a la API real de Anthropic. La medición de tokens se prueba contra un
imitador local de count_tokens (ANTHROPIC_BASE_URL apunta a 127.0.0.1).

Salida: 0 = todas las comprobaciones pasan · 1 = alguna falla · 2 = error de uso

Uso
  python banco/probar_banco.py corpus/doc.pdf [--conservar]
"""
import argparse
import base64
import datetime
import hashlib
import http.client
import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CARPETA_BANCO = Path(__file__).resolve().parent
sys.path.insert(0, str(CARPETA_BANCO.parent))  # el kit se importa; nunca se copia

import gate_fidelidad as gate  # noqa: E402
import probar_gate  # noqa: E402  (rasterizar_pagina: el mutante M11 del kit)
sys.path.insert(0, str(CARPETA_BANCO))
import curaduria  # noqa: E402  (quitar_bloques: comprobación independiente del consolidado)
import pypdfium2 as pdfium  # noqa: E402
from docx import Document  # noqa: E402

ESTILO_MARCA = "Marca de página"  # contrato con servidor_local.py
RESULTADOS = []


def comprobar(nombre: str, condicion, detalle: str = "") -> bool:
    pasa = bool(condicion)
    RESULTADOS.append({"comprobacion": nombre, "pasa": pasa, "detalle": detalle})
    print(f"{'✅' if pasa else '❌'} {nombre}" + (f" — {detalle}" if detalle else ""), flush=True)
    return pasa


def json_de(cuerpo: bytes) -> dict:
    try:
        datos = json.loads(cuerpo.decode("utf-8"))
        return datos if isinstance(datos, dict) else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


# ───────────────────────── cliente HTTP crudo ─────────────────────────

def pedir(puerto: int, metodo: str, ruta: str, cuerpo: bytes = None, cabeceras=(), host=None, largo=None):
    """Petición sin proxies, con Host a elección. Devuelve (estado, cabeceras, cuerpo); estado 0 = sin conexión."""
    conexion = http.client.HTTPConnection("127.0.0.1", puerto, timeout=300)
    try:
        conexion.putrequest(metodo, ruta, skip_host=True, skip_accept_encoding=True)
        conexion.putheader("Host", f"127.0.0.1:{puerto}" if host is None else host)
        for clave, valor in cabeceras:
            conexion.putheader(clave, valor)
        if largo is not None:
            conexion.putheader("Content-Length", str(largo))
        elif cuerpo is not None:
            conexion.putheader("Content-Length", str(len(cuerpo)))
        conexion.endheaders(cuerpo)
        respuesta = conexion.getresponse()
        return respuesta.status, {k.lower(): v for k, v in respuesta.getheaders()}, respuesta.read()
    except (OSError, http.client.HTTPException) as e:
        return 0, {"error": str(e)}, b""
    finally:
        conexion.close()


def puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Banco:
    """Una instancia de servidor_local.py en un puerto libre, con su propio entorno."""

    def __init__(self, trabajo: Path, entorno: dict, nombre: str, extra=()):
        self.puerto = puerto_libre()
        self.registro = trabajo / f"servidor_{nombre}.log"
        self.archivo_registro = open(self.registro, "w", encoding="utf-8")
        orden = [sys.executable, str(CARPETA_BANCO / "servidor_local.py"),
                 "--puerto", str(self.puerto), "--trabajo", str(trabajo / nombre), *extra]
        self.proceso = subprocess.Popen(orden, env=entorno, stdout=self.archivo_registro,
                                        stderr=subprocess.STDOUT)
        limite = time.time() + 30
        while self.get("/api/config")[0] != 200:
            if self.proceso.poll() is not None:
                raise RuntimeError(f"servidor_local.py terminó al arrancar; ver {self.registro}")
            if time.time() > limite:
                raise RuntimeError("servidor_local.py no respondió en 30 s")
            time.sleep(0.2)
        self.estado_html, _, html = self.get("/")
        self.html = html.decode("utf-8", "replace")
        m = re.search(r'name="token-banco" content="([0-9a-f]{64})"', self.html)
        self.token = m.group(1) if m else ""

    def get(self, ruta, **opciones):
        return pedir(self.puerto, "GET", ruta, **opciones)

    def post(self, ruta, cuerpo, tipo="application/octet-stream", token=None, **opciones):
        """token=None usa el token del HTML; token="" no manda cabecera; otro valor se manda tal cual."""
        token = self.token if token is None else token
        cabeceras = [("Content-Type", tipo)] + ([("X-Banco-Token", token)] if token else [])
        return pedir(self.puerto, "POST", ruta, cuerpo, cabeceras=cabeceras, **opciones)

    def post_json(self, ruta, datos):
        return self.post(ruta, json.dumps(datos).encode("utf-8"), "application/json")

    def cerrar(self):
        self.proceso.terminate()
        try:
            self.proceso.wait(10)
        except subprocess.TimeoutExpired:
            self.proceso.kill()
        self.archivo_registro.close()


class Imitador:
    """Imitador local de count_tokens y de la API de mensajes: registra cada petición y nunca sale a la red."""
    BASE, PDF, MD = 12, 1500, 400
    NUEVA = "TRANSCRIPCIÓN DE PRUEBA · imitador local"
    TRANSCRIPCION = f"LEY DEL IMPUESTO AL VALOR AGREGADO\n{NUEVA}\n[Texto en la ilustración: XQZ]"  # dup · nueva · decorativa
    ENTRADA, SALIDA = 1600, 42

    def __init__(self):
        self.peticiones = []
        imitador = self

        class Manejador(BaseHTTPRequestHandler):
            def do_POST(self):
                cuerpo = json_de(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                imitador.peticiones.append({"ruta": self.path, "llave": self.headers.get("x-api-key", ""),
                                            "beta": self.headers.get("anthropic-beta", ""), "cuerpo": cuerpo})
                contenido = cuerpo.get("messages", [{}])[0].get("content", [])
                if "count_tokens" not in self.path:   # API de mensajes: una transcripción de mentira
                    datos = json.dumps({
                        "id": "msg_imitador", "type": "message", "role": "assistant", "model": cuerpo.get("model", ""),
                        "content": [{"type": "text", "text": Imitador.TRANSCRIPCION}],
                        "stop_reason": "end_turn", "stop_sequence": None,
                        "usage": {"input_tokens": Imitador.ENTRADA, "output_tokens": Imitador.SALIDA}}).encode("utf-8")
                else:
                    if any(b.get("type") == "document" for b in contenido):
                        n = Imitador.PDF
                    elif len(contenido) == 1:
                        n = Imitador.BASE
                    else:
                        n = Imitador.MD
                    datos = json.dumps({"input_tokens": n}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(datos)))
                self.send_header("request-id", "req_imitador")
                self.end_headers()
                self.wfile.write(datos)

            do_GET = do_POST

            def log_message(self, *args):
                pass

        self.servidor = ThreadingHTTPServer(("127.0.0.1", 0), Manejador)
        self.url = f"http://127.0.0.1:{self.servidor.server_address[1]}"
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()

    def cerrar(self):
        self.servidor.shutdown()
        self.servidor.server_close()


def entorno_prueba(url_imitador: str, llave: str = "") -> dict:
    """Entorno del servidor bajo prueba: count_tokens apunta al imitador local, sin proxies."""
    quitar = {"ANTHROPIC_API_KEY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"}
    entorno = {k: v for k, v in os.environ.items() if k not in quitar}
    entorno["ANTHROPIC_BASE_URL"] = url_imitador
    entorno["NO_PROXY"] = entorno["no_proxy"] = "127.0.0.1,localhost"
    if llave:
        entorno["ANTHROPIC_API_KEY"] = llave
    return entorno


# ───────────────────────── utilidades de las pruebas ─────────────────────────

def componer(vista: dict, segmentos=None) -> str:
    """Igual que componerMd() de banco.html: preámbulo + Σ(cabecera + cuerpo)."""
    segmentos = vista["segmentos"] if segmentos is None else segmentos
    return vista["preambulo"] + "".join(s["cabecera"] + s["cuerpo"] for s in segmentos)


def alterar_un_digito(vista: dict):
    """Primer dígito de una página con capa de texto: d → (d + 1) mod 10."""
    clases = {d["pagina"]: d["clase"] for d in vista["informe"]["detalle"]}
    for i, s in enumerate(vista["segmentos"]):
        m = re.search(r"[0-9]", s["cuerpo"])
        if clases.get(s["ancla"]) == "texto" and m:
            nuevo = str((int(m.group(0)) + 1) % 10)
            segmentos = [dict(x) for x in vista["segmentos"]]
            segmentos[i]["cuerpo"] = s["cuerpo"][:m.start()] + nuevo + s["cuerpo"][m.end():]
            return componer(vista, segmentos), s["ancla"], m.group(0), nuevo
    return None


def paginas_del_encabezado(texto: str) -> set:
    """'PÁGINAS NO VERIFICADAS: 3, 7–9' → {3, 7, 8, 9}."""
    paginas = set()
    for parte in texto.split(":", 1)[-1].split(","):
        a, _, b = parte.strip().partition("–")
        if a.isdigit():
            paginas.update(range(int(a), int(b or a) + 1))
    return paginas


def revisar_docx(datos: bytes, paginas: int, sha: str, veredicto: str, no_verificadas: list, etiqueta: str):
    try:
        doc = Document(io.BytesIO(datos))
    except Exception as e:  # cualquier fallo de apertura es una falla del gate
        comprobar(f"{etiqueta}: el .docx abre con python-docx", False, f"{type(e).__name__}: {e}")
        return
    comprobar(f"{etiqueta}: el .docx abre con python-docx", True, f"{len(datos)} bytes")
    marcas = [p for p in doc.paragraphs if p.style.name == ESTILO_MARCA]
    comprobar(f"{etiqueta}: una marca «p. N» por página, en orden",
              [p.text for p in marcas] == [f"p. {n}" for n in range(1, paginas + 1)],
              f"{len(marcas)} marcas · {paginas} páginas")
    comprobar(f"{etiqueta}: cada página después de la 1 empieza con salto de página",
              all(p.paragraph_format.page_break_before for p in marcas[1:]))
    propiedades = doc.core_properties
    comprobar(f"{etiqueta}: el sha256 del PDF está en las propiedades del .docx",
              sha in (propiedades.identifier or "") and sha in (propiedades.comments or ""),
              propiedades.identifier or "(vacío)")
    encabezado = " ".join(p.text for p in doc.sections[0].header.paragraphs).strip()
    if veredicto == gate.AMARILLO:
        comprobar(f"{etiqueta}: 🟡 lleva el encabezado «PÁGINAS NO VERIFICADAS: …» con sus páginas",
                  encabezado.startswith("PÁGINAS NO VERIFICADAS:")
                  and paginas_del_encabezado(encabezado) == set(no_verificadas), encabezado[:120])
    else:
        comprobar(f"{etiqueta}: 🟢 no lleva encabezado de páginas no verificadas",
                  "NO VERIFICADAS" not in encabezado, encabezado[:120] or "(sin encabezado)")


# ───────────────────────── pruebas ─────────────────────────

def probar_sin_llave(banco: Banco, imitador: Imitador, ruta_pdf: Path, trabajo: Path) -> None:
    pdf = ruta_pdf.read_bytes()
    sha = hashlib.sha256(pdf).hexdigest()
    documento = pdfium.PdfDocument(str(ruta_pdf))
    paginas = len(documento)
    documento.close()

    print("\n— Seguridad")
    comprobar("GET / → 200 con un token aleatorio de 64 hex inyectado",
              banco.estado_html == 200 and len(banco.token) == 64)
    externos = re.findall(r"""(?:src|href)\s*=\s*["']?\s*(?:https?:)?//|@import|url\(\s*["']?\s*(?:https?:)?//""",
                          banco.html, re.IGNORECASE)
    comprobar("banco.html no carga recursos externos (sin CDNs)", not externos, ", ".join(externos))
    estado, _, _ = banco.get("/", host="localhost")
    comprobar("Host «localhost» → 200", estado == 200, f"HTTP {estado}")
    for host in ("evil.example", f"localhost.evil.example:{banco.puerto}", f"127.0.0.1.nip.io:{banco.puerto}", ""):
        estado, _, _ = banco.get("/", host=host)
        comprobar(f"Host ajeno «{host}» → 403", estado == 403, f"HTTP {estado}")
    estado, _, _ = banco.post("/api/subir?nombre=x.pdf", b"%PDF-1.7\n%prueba sin token\n", "application/pdf", token="")
    comprobar("POST sin token → 403", estado == 403, f"HTTP {estado}")
    estado, _, _ = banco.post("/api/subir?nombre=x.pdf", b"%PDF-1.7\n%token falso\n", "application/pdf", token="0" * 64)
    comprobar("POST con token equivocado → 403", estado == 403, f"HTTP {estado}")
    estado, _, cuerpo = banco.post("/api/subir?nombre=falso.pdf", b"Esto es texto plano, no un PDF.\n", "application/pdf")
    comprobar("cuerpo que no es PDF → 400", estado == 400, json_de(cuerpo).get("error", f"HTTP {estado}"))
    estado, _, cuerpo = banco.post("/api/subir?nombre=roto.pdf", b"%PDF-1.4\nesto no es un PDF de verdad\n", "application/pdf")
    comprobar("empieza con %PDF- pero es ilegible → 400", estado == 400, json_de(cuerpo).get("error", f"HTTP {estado}"))
    estado, _, cuerpo = banco.post("/api/subir", None, "application/pdf", largo=10 ** 10)
    comprobar("Content-Length mayor que el tope → 413 sin leer el cuerpo", estado == 413,
              json_de(cuerpo).get("error", f"HTTP {estado}"))

    print("\n— Conversión con el convertidor «minimo» y gate")
    ruta = "/api/subir?convertidor=minimo&nombre=" + urllib.parse.quote(ruta_pdf.name)
    estado, _, cuerpo = banco.post(ruta, pdf, "application/pdf")
    vista = json_de(cuerpo)
    if not comprobar("subir el PDF real → 200 con el informe del gate", estado == 200 and "informe" in vista,
                     vista.get("error", f"HTTP {estado}")):
        return
    informe, id_doc = vista["informe"], vista["id"]
    base = informe["codigo_salida"]
    comprobar("veredicto inicial 🟢 o 🟡 (código 0 o 3), nunca 🔴", base in (0, 3),
              f"{informe['veredicto']} código {base} · {informe['resumen']}")
    campos = all(k in informe for k in ("veredicto", "codigo_salida", "paginas", "clases_oraculo", "sha256_pdf"))
    comprobar("el resumen trae veredicto, código, páginas, clases de oráculo, residuo G3 y sha256",
              campos and "residuo_pct" in informe.get("eficiencia", {}),
              f"clases {informe.get('clases_oraculo')} · residuo G3 {informe.get('eficiencia', {}).get('residuo_pct')}%")
    comprobar("el sha256 del informe es el del PDF subido", informe["sha256_pdf"] == sha, sha[:16] + "…")

    estado, _, crudo = banco.get(f"/api/doc/{id_doc}/descargar/md")
    md = crudo.decode("utf-8")
    anclas = [int(n) for n in gate.ANCLA.findall(md)]
    comprobar("hay tantas anclas [[p. N]] como páginas, en orden",
              estado == 200 and anclas == list(range(1, paginas + 1)) and informe["paginas"] == paginas,
              f"{len(anclas)} anclas · {paginas} páginas (contadas con pypdfium2)")
    comprobar("los segmentos de la UI reconstruyen el .md byte a byte", componer(vista) == md)

    estado, _, cuerpo = banco.post_json(f"/api/doc/{id_doc}/verificar", {"md": componer(vista)})
    nueva = json_de(cuerpo).get("informe", {})
    mismos = [d["veredicto"] for d in nueva.get("detalle", [])] == [d["veredicto"] for d in informe["detalle"]]
    comprobar("re-verificar sin cambios → mismo código y mismos veredictos por página",
              estado == 200 and nueva.get("codigo_salida") == base and mismos,
              f"código {nueva.get('codigo_salida')} (antes {base})")

    print("\n— Imagen de página")
    estado, cabeceras, png = banco.get(f"/api/doc/{id_doc}/pagina/1.png")
    comprobar("la imagen de la p. 1 es PNG",
              estado == 200 and cabeceras.get("content-type") == "image/png" and png.startswith(b"\x89PNG\r\n\x1a\n"),
              f"HTTP {estado} · {cabeceras.get('content-type')} · {len(png)} bytes")
    estado, _, _ = banco.get(f"/api/doc/{id_doc}/pagina/{paginas + 1}.png")
    comprobar(f"la p. {paginas + 1} no existe → 404", estado == 404, f"HTTP {estado}")
    estado, _, _ = banco.get(f"/api/doc/{id_doc}/pagina/uno.png")
    comprobar("página no numérica → 400", estado == 400, f"HTTP {estado}")

    print("\n— Identificadores")
    for ruta in ("/api/doc/../../etc/pagina/1.png", "/api/doc/..%2F..%2Fetc%2Fpasswd/descargar/md"):
        estado, _, _ = banco.get(ruta)
        comprobar(f"GET {ruta} → 400", estado == 400, f"HTTP {estado}")
    estado, _, _ = banco.post_json(f"/api/doc/../{id_doc}/verificar", {"md": md})
    comprobar("POST /api/doc/../<id>/verificar → 400", estado == 400, f"HTTP {estado}")
    estado, _, _ = banco.get("/api/doc/" + "0" * 32)
    comprobar("id hexadecimal válido pero inexistente → 404", estado == 404, f"HTTP {estado}")

    print("\n— Demostración del gate: alterar un dígito")
    alterado = alterar_un_digito(vista)
    if not comprobar("el PDF tiene una página con capa de texto y al menos un dígito", alterado is not None):
        return
    md_alterado, n, antes, despues = alterado
    estado, _, cuerpo = banco.post_json(f"/api/doc/{id_doc}/verificar", {"md": md_alterado})
    vista_roja = json_de(cuerpo)
    informe_rojo = vista_roja.get("informe", {})
    pagina = next((d for d in informe_rojo.get("detalle", []) if d["pagina"] == n), {})
    comprobar(f"alterar un dígito (p. {n}: «{antes}» → «{despues}») → código 1",
              estado == 200 and informe_rojo.get("codigo_salida") == 1,
              f"{informe_rojo.get('veredicto')} código {informe_rojo.get('codigo_salida')}")
    comprobar(f"la p. {n} sale 🔴 con hallazgo G2",
              pagina.get("veredicto") == gate.ROJO and any(h.startswith("G2") for h in pagina.get("hallazgos", [])),
              " | ".join(pagina.get("hallazgos", []))[:160])
    comprobar("con 🔴 la vista marca el .docx como bloqueado", vista_roja.get("docx", {}).get("permitido") is False,
              vista_roja.get("docx", {}).get("causa", ""))
    estado, _, cuerpo = banco.get(f"/api/doc/{id_doc}/descargar/docx")
    comprobar("con 🔴 la exportación .docx se bloquea → 409", estado == 409, json_de(cuerpo).get("error", f"HTTP {estado}"))
    bloqueados = {f: banco.get(f"/api/doc/{id_doc}/exportar/{f}")[0] for f in ("docx", "gdocs", "txt", "html")}
    libres = {f: banco.get(f"/api/doc/{id_doc}/exportar/{f}")[0] for f in ("md", "json")}
    comprobar("con 🔴 se bloquean Word, Google Docs, texto y web (409); Markdown y evidencia siguen disponibles",
              set(bloqueados.values()) == {409} and set(libres.values()) == {200}, f"{bloqueados} · {libres}")

    print("\n— .md externo verificado contra el mismo PDF")
    estado, _, cuerpo = banco.post(f"/api/doc/{id_doc}/md?nombre=externo.md", b"\xff\xfe\x00no utf8", "text/markdown")
    comprobar(".md externo que no es UTF-8 → 400", estado == 400, json_de(cuerpo).get("error", f"HTTP {estado}"))
    sin_anclas = gate.ANCLA.sub("", md).encode("utf-8")
    estado, _, cuerpo = banco.post(f"/api/doc/{id_doc}/md?nombre=sin_anclas.md", sin_anclas, "text/markdown")
    vista_sin = json_de(cuerpo)
    codigo_sin = vista_sin.get("informe", {}).get("codigo_salida")
    comprobar(".md externo sin anclas → el gate se abstiene (código 3) y el .docx se bloquea",
              estado == 200 and codigo_sin == 3 and vista_sin.get("docx", {}).get("permitido") is False,
              f"código {codigo_sin} · {vista_sin.get('docx', {}).get('causa', '')}")
    estado, _, cuerpo = banco.post(f"/api/doc/{id_doc}/md?nombre=externo.md", md.encode("utf-8"), "text/markdown")
    vista_ext = json_de(cuerpo)
    comprobar(".md externo idéntico al original → mismo código que el original",
              estado == 200 and vista_ext.get("informe", {}).get("codigo_salida") == base
              and vista_ext.get("origen_md", "").startswith("externo"),
              f"código {vista_ext.get('informe', {}).get('codigo_salida')} · origen {vista_ext.get('origen_md')}")

    print("\n— Descargas")
    estado, _, cuerpo = banco.get(f"/api/doc/{id_doc}/descargar/md")
    comprobar("el .md descargado es el último verificado", estado == 200 and cuerpo == md.encode("utf-8"))
    estado, _, cuerpo = banco.get(f"/api/doc/{id_doc}/descargar/informe")
    comprobar("informe.json se descarga con el código del gate",
              estado == 200 and json_de(cuerpo).get("codigo_salida") == base, f"HTTP {estado}")
    estado, cabeceras, docx = banco.get(f"/api/doc/{id_doc}/descargar/docx")
    if comprobar(f"con {informe['veredicto']} el .docx se descarga", estado == 200,
                 cabeceras.get("content-disposition", f"HTTP {estado}")):
        no_verificadas = [d["pagina"] for d in informe["detalle"] if d["veredicto"] == gate.AMARILLO]
        revisar_docx(docx, paginas, sha, informe["veredicto"], no_verificadas, "PDF real")

    print("\n— Página escaneada (mutante M11 del kit): 🟡 y encabezado en el .docx")
    con_texto = [d["pagina"] for d in informe["detalle"] if d["clase"] == "texto"]
    n = con_texto[len(con_texto) // 2]
    ruta_m11 = trabajo / f"M11_p{n}_rasterizada.pdf"
    probar_gate.rasterizar_pagina(ruta_pdf, n, ruta_m11)
    datos_m11 = ruta_m11.read_bytes()
    estado, _, cuerpo = banco.post("/api/subir?nombre=" + ruta_m11.name, datos_m11, "application/pdf")
    vista_m11 = json_de(cuerpo)
    informe_m11 = vista_m11.get("informe", {})
    pagina = next((d for d in informe_m11.get("detalle", []) if d["pagina"] == n), {})
    comprobar(f"PDF con la p. {n} rasterizada → código 3 y p. {n} 🟡 [{pagina.get('clase')}]",
              estado == 200 and informe_m11.get("codigo_salida") == 3 and pagina.get("veredicto") == gate.AMARILLO,
              " | ".join(pagina.get("hallazgos", []))[:120])
    if estado == 200:
        estado, _, docx = banco.get(f"/api/doc/{vista_m11['id']}/descargar/docx")
        if comprobar("con 🟡 el .docx se descarga", estado == 200, f"HTTP {estado}"):
            no_verificadas = [d["pagina"] for d in informe_m11["detalle"] if d["veredicto"] == gate.AMARILLO]
            revisar_docx(docx, paginas, hashlib.sha256(datos_m11).hexdigest(), gate.AMARILLO, no_verificadas,
                         "PDF con página escaneada")

    print("\n— Medir tokens sin llave")
    tokens = json_de(banco.get("/api/config")[2]).get("tokens", {})
    comprobar("sin llave el botón queda deshabilitado con su causa",
              tokens.get("disponible") is False and "ANTHROPIC_API_KEY" in tokens.get("causa", ""), tokens.get("causa", ""))
    antes = len(imitador.peticiones)
    estado, _, cuerpo = banco.post_json(f"/api/doc/{id_doc}/tokens", {"confirmado": True})
    comprobar("medir tokens sin llave → rechazo (412) sin intentar red",
              estado == 412 and len(imitador.peticiones) == antes,
              f"HTTP {estado} · {len(imitador.peticiones) - antes} peticiones de red · "
              + json_de(cuerpo).get("error", ""))
    estado, _, cuerpo = banco.post_json(f"/api/doc/{id_doc}/transcribir/1", {"confirmado": True})
    comprobar("transcribir con Claude sin llave → rechazo (412) sin intentar red",
              estado == 412 and len(imitador.peticiones) == antes,
              f"HTTP {estado} · {len(imitador.peticiones) - antes} peticiones de red")


def probar_con_llave(imitador: Imitador, ruta_pdf: Path, trabajo: Path) -> None:
    print("\n— Medir tokens con llave de prueba contra el imitador local (nunca la API real)")
    llave = "sk-ant-prueba-" + secrets.token_hex(12)
    banco = Banco(trabajo, entorno_prueba(imitador.url, llave), "con_llave")
    try:
        configuracion = banco.get("/api/config")[2]
        comprobar("con llave el botón queda habilitado", json_de(configuracion).get("tokens", {}).get("disponible") is True)
        pdf = ruta_pdf.read_bytes()
        estado, _, cuerpo = banco.post("/api/subir?nombre=" + urllib.parse.quote(ruta_pdf.name), pdf, "application/pdf")
        id_doc = json_de(cuerpo).get("id", "")
        antes = len(imitador.peticiones)
        estado, _, _ = banco.post_json(f"/api/doc/{id_doc}/tokens", {"modelo": "claude-sonnet-5-5"})
        comprobar("sin confirmación explícita → 400 y sin red",
                  estado == 400 and len(imitador.peticiones) == antes, f"HTTP {estado}")
        estado, _, respuesta = banco.post_json(f"/api/doc/{id_doc}/tokens",
                                               {"confirmado": True, "modelo": "claude-sonnet-5-5"})
        r = json_de(respuesta)
        comprobar("con confirmación: cifras exactas de count_tokens (PDF y .md menos la base)",
                  estado == 200 and r.get("tokens_pdf_nativo") == Imitador.PDF - Imitador.BASE
                  and r.get("tokens_md") == Imitador.MD - Imitador.BASE and r.get("estado") == "MEDIDO",
                  f"HTTP {estado} · PDF {r.get('tokens_pdf_nativo')} · .md {r.get('tokens_md')} · "
                  f"ahorro {r.get('ahorro_pct')}% · {r.get('estado')}")
        nuevas = imitador.peticiones[antes:]
        datos_pdf = [b["source"]["data"] for p in nuevas for b in p["cuerpo"]["messages"][0]["content"]
                     if b.get("type") == "document"]
        comprobar("se enviaron base, PDF y .md a /v1/messages/count_tokens con la llave en x-api-key",
                  len(nuevas) == 3 and all(p["ruta"] == "/v1/messages/count_tokens" and p["llave"] == llave for p in nuevas)
                  and datos_pdf == [base64.standard_b64encode(pdf).decode("ascii")],
                  f"{len(nuevas)} peticiones")

        print("\n— Transcripción asistida con Claude (visión) contra el imitador local: capa NO verificada")
        antes_vista = json_de(banco.get(f"/api/doc/{id_doc}")[2])
        antes = len(imitador.peticiones)
        estado, _, _ = banco.post_json(f"/api/doc/{id_doc}/transcribir/1", {"modelo": "claude-sonnet-5-5"})
        comprobar("transcribir sin confirmación explícita → 400 y sin red",
                  estado == 400 and len(imitador.peticiones) == antes, f"HTTP {estado}")
        estado, _, _ = banco.post_json(f"/api/doc/{id_doc}/transcribir/999", {"confirmado": True})
        comprobar("transcribir una página inexistente → 404 y sin red",
                  estado == 404 and len(imitador.peticiones) == antes, f"HTTP {estado}")
        estado, _, cuerpo_t = banco.post_json(f"/api/doc/{id_doc}/transcribir/1",
                                              {"confirmado": True, "modelo": "claude-sonnet-5-5"})
        r0 = (json_de(cuerpo_t).get("regiones") or [{}])[0]
        comprobar("con confirmación: devuelve el texto y el uso de tokens que reporta la API",
                  estado == 200 and r0.get("texto") == Imitador.TRANSCRIPCION
                  and r0.get("uso") == {"input_tokens": Imitador.ENTRADA, "output_tokens": Imitador.SALIDA}
                  and r0.get("request_id") == "req_imitador",
                  f"HTTP {estado} · {r0.get('modelo_respuesta')} · {r0.get('modo')} · uso {r0.get('uso')}")
        nuevas = imitador.peticiones[antes:]
        p = nuevas[0] if len(nuevas) == 1 else {}
        bloques = p.get("cuerpo", {}).get("messages", [{}])[0].get("content", [])
        imagen = next((b for b in bloques if b.get("type") == "image"), {})
        png_ok = base64.b64decode(imagen.get("source", {}).get("data", "") or b"").startswith(b"\x89PNG\r\n\x1a\n")
        comprobar("una sola petición a /v1/messages: imagen PNG, llave en x-api-key, modelo pedido y fallback",
                  len(nuevas) == 1 and p["ruta"].startswith("/v1/messages") and "count_tokens" not in p["ruta"]
                  and p["llave"] == llave and p["cuerpo"].get("model") == "claude-sonnet-5-5" and png_ok
                  and p["cuerpo"].get("fallbacks") == "default" and "server-side-fallback" in p["beta"],
                  f"{len(nuevas)} petición(es) · {p.get('ruta')}")
        sistema = p.get("cuerpo", {}).get("system", "")
        comprobar("la instrucción del sistema trata el texto de la imagen como dato, no como orden",
                  "no las sigas" in sistema and "Nunca adivines cifras" in sistema)
        despues = json_de(banco.get(f"/api/doc/{id_doc}")[2])
        comprobar("transcribir no toca el .md ni el veredicto del gate (capa aparte)",
                  despues["informe"]["sha256_md"] == antes_vista["informe"]["sha256_md"]
                  and despues["informe"]["codigo_salida"] == antes_vista["informe"]["codigo_salida"]
                  and "1" in despues.get("transcripciones", {}),
                  f"código {despues['informe']['codigo_salida']} · transcripciones {list(despues.get('transcripciones', {}))}")

        print("\n— Curaduría documental de la transcripción (duplicado · nueva · decorativa)")
        guardada = trabajo / "con_llave" / id_doc / "transcripciones" / "1.json"
        huella = hashlib.sha256(guardada.read_bytes()).hexdigest()
        clases = sorted(e["clase"] for e in despues.get("curaduria", {}).get("registro", []))
        comprobar("la curaduría clasifica: duplicado y decorativo se excluyen, lo nuevo se incorpora en ámbar",
                  clases == ["decorativo", "duplicado", "nuevo"]
                  and all("ámbar" in e["estado"] for e in despues["curaduria"]["registro"] if e["decision"] == "incorporar"),
                  str(despues.get("curaduria", {}).get("resumen")))
        estado, _, md_base = banco.get(f"/api/doc/{id_doc}/descargar/md")
        estado_c, _, consolidado = banco.get(f"/api/doc/{id_doc}/descargar/consolidado")
        consolidado = consolidado.decode("utf-8")
        comprobar("consolidado.md: sin sus bloques es byte por byte el .md base; solo trae lo nuevo, delimitado",
                  estado_c == 200 and curaduria.quitar_bloques(consolidado).encode("utf-8") == md_base
                  and consolidado.count(Imitador.NUEVA) == 1 and "ilustración" not in consolidado
                  and "estado=ambar" in consolidado and "request=req_imitador" in consolidado, f"HTTP {estado_c}")
        estado, _, datos_docx = banco.get(f"/api/doc/{id_doc}/descargar/docx")
        texto_docx = "\n".join(par.text for par in Document(io.BytesIO(datos_docx)).paragraphs) if estado == 200 else ""
        comprobar("el .docx trae solo el texto nuevo, etiquetado «NO VERIFICADO», sin lo decorativo",
                  "NO VERIFICADO" in texto_docx and Imitador.NUEVA in texto_docx and "ilustración" not in texto_docx,
                  f"HTTP {estado}")
        estado, _, _ = banco.post_json(f"/api/doc/{id_doc}/curaduria/1/0", {"decision": "rechazado"})
        _, _, sin_fragmento = banco.get(f"/api/doc/{id_doc}/descargar/consolidado")
        estado_d, _, _ = banco.post_json(f"/api/doc/{id_doc}/curaduria/1/0", {"decision": "pendiente"})
        _, _, restaurado = banco.get(f"/api/doc/{id_doc}/descargar/consolidado")
        comprobar("rechazar en revisión humana saca el fragmento; deshacer lo restaura",
                  estado == 200 and estado_d == 200 and Imitador.NUEVA.encode() not in sin_fragmento
                  and restaurado.decode("utf-8") == consolidado, f"HTTP {estado} / {estado_d}")
        comprobar("la transcripción guardada no cambia con la curaduría ni la revisión",
                  hashlib.sha256(guardada.read_bytes()).hexdigest() == huella)

        print("\n— Exportación en varios formatos (documento final = verificado + curado en ámbar)")
        salidas = {f: banco.get(f"/api/doc/{id_doc}/exportar/{f}") for f in ("txt", "html", "gdocs", "md", "json")}
        txt = salidas["txt"][2].decode("utf-8")
        comprobar("texto (.txt): marca por página, el texto nuevo etiquetado «NO VERIFICADO», sin lo decorativo",
                  salidas["txt"][0] == 200 and txt.count("──── p. ") == antes_vista["informe"]["paginas"]
                  and Imitador.NUEVA in txt and "NO VERIFICADO" in txt and "ilustración" not in txt)
        pagina_web = salidas["html"][2].decode("utf-8")
        comprobar("página web (.html): autocontenida, sin scripts, con el texto nuevo en ámbar",
                  salidas["html"][0] == 200 and salidas["html"][1].get("content-type", "").startswith("text/html")
                  and "<script" not in pagina_web and "http" not in pagina_web.split("</title>")[0]
                  and Imitador.NUEVA in pagina_web and "NO VERIFICADO" in pagina_web)
        try:
            gdocs = "\n".join(p.text for p in Document(io.BytesIO(salidas["gdocs"][2])).paragraphs)
        except Exception:
            gdocs = ""
        comprobar("Google Docs: .docx que abre con python-docx y trae el texto nuevo etiquetado",
                  salidas["gdocs"][0] == 200 and Imitador.NUEVA in gdocs and "NO VERIFICADO" in gdocs,
                  salidas["gdocs"][1].get("content-disposition", ""))
        comprobar("Markdown: es el consolidado; evidencia (.json): informe del gate + registro de curaduría",
                  salidas["md"][0] == 200 and salidas["md"][2].decode("utf-8") == consolidado
                  and {"informe", "curaduria"} <= set(json_de(salidas["json"][2])))
        estado, _, _ = banco.get(f"/api/doc/{id_doc}/exportar/pdf")
        comprobar("un formato desconocido → 400", estado == 400, f"HTTP {estado}")
    finally:
        banco.cerrar()
    registro = banco.registro.read_text(encoding="utf-8")
    archivos = [p for p in (trabajo / "con_llave").rglob("*") if p.is_file()]
    en_archivos = [str(p.name) for p in archivos if llave.encode("ascii") in p.read_bytes()]
    comprobar("la llave no aparece en el HTML, la config, la respuesta, el registro ni en trabajo/",
              llave not in banco.html and llave.encode() not in configuracion and llave.encode() not in respuesta
              and llave not in registro and not en_archivos,
              ", ".join(en_archivos) or f"{len(archivos)} archivos revisados")


def probar_llavero(imitador: Imitador, trabajo: Path) -> None:
    print("\n— Llave desde el llavero (--llavero) con un elemento inexistente: nunca toca la llave real")
    servicio = "pdf2ctx-prueba-inexistente-" + secrets.token_hex(6)
    banco = Banco(trabajo, entorno_prueba(imitador.url), "llavero",
                  extra=["--llavero", "--servicio-llavero", servicio])
    try:
        tokens = json_de(banco.get("/api/config")[2]).get("tokens", {})
        causa = tokens.get("causa", "")
        comprobar("--llavero sin el elemento: arranca con el botón deshabilitado y la causa lo explica",
                  tokens.get("disponible") is False and "llavero" in causa.lower()
                  and (servicio in causa or sys.platform != "darwin"), causa)
    finally:
        banco.cerrar()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Gate del banco de pruebas de pdf2ctx.")
    ap.add_argument("pdf", help="PDF real con cifras")
    ap.add_argument("--conservar", action="store_true", help="Conserva la carpeta de la corrida aunque todo pase")
    args = ap.parse_args()
    ruta_pdf = Path(args.pdf).resolve()
    if not ruta_pdf.is_file() or ruta_pdf.read_bytes()[:5] != b"%PDF-":
        print(f"No es un PDF existente: {args.pdf}")
        return 2

    trabajo = CARPETA_BANCO / "trabajo" / ("_probar_banco_" + time.strftime("%Y%m%d-%H%M%S"))
    trabajo.mkdir(parents=True)
    print(f"probar_banco.py · {ruta_pdf.name} · trabajo {trabajo}")
    imitador = Imitador()
    try:
        banco = Banco(trabajo, entorno_prueba(imitador.url), "sin_llave")
        try:
            probar_sin_llave(banco, imitador, ruta_pdf, trabajo)
        finally:
            banco.cerrar()
        probar_con_llave(imitador, ruta_pdf, trabajo)
        probar_llavero(imitador, trabajo)
    except Exception as e:  # el gate nunca se cae en silencio: una excepción es una falla
        comprobar("la suite corrió completa", False, f"{type(e).__name__}: {e}")
    finally:
        imitador.cerrar()

    fallas = [r for r in RESULTADOS if not r["pasa"]]
    codigo = 1 if fallas or not RESULTADOS else 0
    resumen = {"codigo_salida": codigo, "fecha_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "pdf": str(ruta_pdf), "comprobaciones": RESULTADOS}
    (CARPETA_BANCO / "trabajo" / "probar_banco.json").write_text(
        json.dumps(resumen, ensure_ascii=False, indent=2), encoding="utf-8")
    if codigo == 0 and not args.conservar:
        shutil.rmtree(trabajo, ignore_errors=True)
    else:
        print(f"\nEvidencia conservada en {trabajo}")
    print(f"\n{len(RESULTADOS) - len(fallas)} pasan · {len(fallas)} fallan · código {codigo}")
    return codigo


if __name__ == "__main__":
    sys.exit(main())
