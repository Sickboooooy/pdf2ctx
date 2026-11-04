#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
medir_tokens.py — Baseline de tokens: PDF nativo vs .md (pdf2ctx · Sprint 0)

Mide con el endpoint oficial count_tokens cuántos tokens cuesta cada documento enviado
como PDF nativo (texto + imagen por página) contra el mismo documento como .md.
Cada cifra queda respaldada por la respuesta JSON de la API (carpeta respuestas/).

Reglas VVCA
  · Un documento que no se pudo medir es NO MEDIDO, nunca 0.
  · Si el PDF tiene páginas sin capa de texto, el .md mínimo no tiene su contenido (falta OCR):
    el ahorro se reporta como NO COMPARABLE en vez de inflarlo.

Uso
  export ANTHROPIC_API_KEY=...
  python medir_tokens.py --corpus corpus_sprint0.json [--modelo claude-sonnet-5-5 --modelo claude-opus-5-5]
  python medir_tokens.py --pdf doc.pdf [--md doc.md] --dry-run

Códigos de salida: 0 = todo medido y comparable · 3 = algo NO MEDIDO o NO COMPARABLE · 2 = error de uso
"""
import argparse
import base64
import csv
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import gate_fidelidad as gate

VERSION_API = "2023-06-01"
MODELO_DEFECTO = "claude-sonnet-5-5"
LIMITE_BYTES = 32 * 1024 * 1024
PROMPT = "Analiza el documento."


def url_conteo() -> str:
    base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    return f"{base}/v1/messages/count_tokens"


def contar(modelo: str, contenido: list, clave: str, intentos: int = 4):
    cuerpo = json.dumps({"model": modelo, "messages": [{"role": "user", "content": contenido}]}).encode("utf-8")
    peticion = urllib.request.Request(url_conteo(), data=cuerpo, method="POST", headers={
        "x-api-key": clave, "anthropic-version": VERSION_API, "content-type": "application/json"})
    for intento in range(intentos):
        try:
            with urllib.request.urlopen(peticion, timeout=180) as r:
                return json.loads(r.read()), r.headers.get("request-id")
        except urllib.error.HTTPError as e:
            detalle = e.read().decode("utf-8", "replace")[:400]
            if e.code in (429, 500, 502, 503, 529) and intento < intentos - 1:
                time.sleep(2 * 2 ** intento)
                continue
            raise RuntimeError(f"HTTP {e.code}: {detalle}") from None
        except urllib.error.URLError as e:
            if intento < intentos - 1:
                time.sleep(2 * 2 ** intento)
                continue
            raise RuntimeError(f"Red: {e.reason}") from None


def guardar(carpeta: Path, nombre: str, modelo: str, respuesta: dict, request_id) -> None:
    carpeta.mkdir(parents=True, exist_ok=True)
    registro = {"modelo": modelo, "fecha_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "request_id": request_id, "endpoint": url_conteo(), "respuesta": respuesta}
    (carpeta / f"{nombre}.json").write_text(json.dumps(registro, ensure_ascii=False, indent=2), encoding="utf-8")


def documentos(args) -> list:
    if args.corpus:
        ruta = Path(args.corpus)
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        salida = []
        for d in datos["documentos"]:
            md = d.get("md")
            salida.append({"id": d["id"], "categoria": d.get("categoria", ""),
                           "pdf": (ruta.parent / d["pdf"]).resolve(),
                           "md": (ruta.parent / md).resolve() if md else None})
        return salida
    return [{"id": Path(args.pdf).stem, "categoria": "", "pdf": Path(args.pdf),
             "md": Path(args.md) if args.md else None}]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Tokens de PDF nativo vs .md con count_tokens.")
    ap.add_argument("--corpus", help="corpus_sprint0.json")
    ap.add_argument("--pdf", help="Un PDF (alternativa a --corpus)")
    ap.add_argument("--md", help=".md del PDF; si falta se genera el .md mínimo")
    ap.add_argument("--modelo", action="append", help=f"Repetible. Por defecto {MODELO_DEFECTO}")
    ap.add_argument("--salida", default="resultados_sprint0")
    ap.add_argument("--dry-run", action="store_true", help="No llama a la API; solo prepara y dimensiona")
    args = ap.parse_args()
    if not (args.corpus or args.pdf):
        ap.error("indica --corpus o --pdf")

    modelos = args.modelo or [MODELO_DEFECTO]
    clave = os.environ.get("ANTHROPIC_API_KEY", "")
    if not args.dry_run and not clave:
        print("Falta ANTHROPIC_API_KEY (o usa --dry-run).")
        return 2

    docs = documentos(args)
    faltantes = [str(d["pdf"]) for d in docs if not d["pdf"].is_file()]
    if faltantes:
        print("PDFs inexistentes:\n  " + "\n  ".join(faltantes))
        return 2

    salida = Path(args.salida)
    carpeta_md, carpeta_resp = salida / "md", salida / "respuestas"
    carpeta_md.mkdir(parents=True, exist_ok=True)

    base_por_modelo = {}
    filas = []
    for d in docs:
        oraculo = gate.leer_oraculo(d["pdf"])
        paginas = len(oraculo)
        sin_texto = sum(p["clase"] == "sin_texto" for p in oraculo)
        ruta_md = d["md"]
        if ruta_md is None:
            ruta_md = carpeta_md / f"{d['id']}.md"
            gate.exportar_minimo(d["pdf"], ruta_md)
        texto_md = Path(ruta_md).read_text(encoding="utf-8")
        bytes_pdf = d["pdf"].stat().st_size
        bytes_b64 = 4 * ((bytes_pdf + 2) // 3)

        for modelo in modelos:
            fila = {"id": d["id"], "categoria": d["categoria"], "modelo": modelo, "paginas": paginas,
                    "paginas_sin_texto": sin_texto, "bytes_pdf": bytes_pdf, "md": str(ruta_md),
                    "tokens_pdf_nativo": "", "tokens_md": "", "ahorro_pct": "",
                    "tokens_pag_pdf": "", "tokens_pag_md": "", "estado": "", "nota": ""}
            notas = []
            if paginas > 100:
                notas.append("más de 100 páginas: puede exceder el límite por solicitud; si falla, divídelo")
            if bytes_b64 > LIMITE_BYTES:
                fila.update(estado="NO MEDIDO", nota="excede 32 MB en base64: dividir el PDF")
                filas.append(fila)
                continue
            if args.dry_run:
                fila.update(estado="DRY-RUN", nota="; ".join(notas + [f"md {len(texto_md)} caracteres"]))
                filas.append(fila)
                continue
            try:
                if modelo not in base_por_modelo:
                    resp, rid = contar(modelo, [{"type": "text", "text": PROMPT}], clave)
                    guardar(carpeta_resp, f"_base.{modelo}", modelo, resp, rid)
                    base_por_modelo[modelo] = resp["input_tokens"]
                b64 = base64.standard_b64encode(d["pdf"].read_bytes()).decode("ascii")
                resp_pdf, rid_pdf = contar(modelo, [
                    {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}},
                    {"type": "text", "text": PROMPT}], clave)
                guardar(carpeta_resp, f"{d['id']}.{modelo}.pdf", modelo, resp_pdf, rid_pdf)
                resp_md, rid_md = contar(modelo, [
                    {"type": "text", "text": texto_md}, {"type": "text", "text": PROMPT}], clave)
                guardar(carpeta_resp, f"{d['id']}.{modelo}.md", modelo, resp_md, rid_md)
            except (RuntimeError, KeyError) as e:
                fila.update(estado="NO MEDIDO", nota="; ".join(notas + [str(e)]))
                filas.append(fila)
                continue

            base = base_por_modelo[modelo]
            t_pdf = resp_pdf["input_tokens"] - base
            t_md = resp_md["input_tokens"] - base
            fila.update(tokens_pdf_nativo=t_pdf, tokens_md=t_md,
                        tokens_pag_pdf=round(t_pdf / max(paginas, 1)),
                        tokens_pag_md=round(t_md / max(paginas, 1)),
                        ahorro_pct=round(100 * (1 - t_md / t_pdf), 1) if t_pdf > 0 else "")
            if sin_texto:
                fila["estado"] = "NO COMPARABLE"
                notas.append(f"{sin_texto} página(s) sin capa de texto: el .md no tiene su contenido (falta OCR)")
            else:
                fila["estado"] = "MEDIDO"
            fila["nota"] = "; ".join(notas)
            filas.append(fila)

    salida.mkdir(parents=True, exist_ok=True)
    with open(salida / "baseline_sprint0.csv", "w", newline="", encoding="utf-8") as f:
        escritor = csv.DictWriter(f, fieldnames=list(filas[0].keys()))
        escritor.writeheader()
        escritor.writerows(filas)

    print(f"{'documento':<20} {'modelo':<20} {'págs':>5} {'PDF nativo':>11} {'.md':>9} {'ahorro':>7}  estado")
    for f_ in filas:
        ahorro = f"{f_['ahorro_pct']}%" if f_["ahorro_pct"] != "" else ""
        print(f"{f_['id']:<20} {f_['modelo']:<20} {f_['paginas']:>5} {str(f_['tokens_pdf_nativo']):>11} "
              f"{str(f_['tokens_md']):>9} {ahorro:>7}  {f_['estado']}" + (f" · {f_['nota']}" if f_["nota"] else ""))

    comparables = [f_ for f_ in filas if f_["estado"] == "MEDIDO"]
    for modelo in modelos:
        grupo = [f_ for f_ in comparables if f_["modelo"] == modelo]
        if grupo:
            tp = sum(f_["tokens_pdf_nativo"] for f_ in grupo)
            tm = sum(f_["tokens_md"] for f_ in grupo)
            print(f"\n{modelo}: {len(grupo)} docs comparables · PDF nativo {tp:,} · .md {tm:,} · "
                  f"ahorro agregado {100 * (1 - tm / tp):.1f}%" if tp else "")
    print(f"\nCSV: {salida / 'baseline_sprint0.csv'} · respuestas: {carpeta_resp}/")
    pendientes = [f_ for f_ in filas if f_["estado"] in ("NO MEDIDO", "NO COMPARABLE")]
    if args.dry_run:
        return 3
    return 3 if pendientes else 0


if __name__ == "__main__":
    sys.exit(main())
