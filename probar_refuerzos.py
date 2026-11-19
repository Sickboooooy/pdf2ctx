#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probar_refuerzos.py — Verifica R3 (tablas por columnas) y R9 (OCR por cotejar) sin corpus.

Usa fixtures sintéticos autocontenidos. Salida 0 si todo pasa.
"""
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / "defectos"))
sys.path.insert(0, str(RAIZ))

import fabricar  # noqa: E402
import gate_reforzado as reforzado  # noqa: E402
from PIL import Image  # noqa: E402


def checar(nombre, condicion, detalle=""):
    print(f"  {'✅' if condicion else '❌'} {nombre}" + (f" — {detalle}" if detalle else ""))
    return condicion


def probar(pdf_bytes, nombre):
    ruta = RAIZ / f"_rfx_{nombre}.pdf"
    ruta.write_bytes(pdf_bytes)
    md = RAIZ / f"_rfx_{nombre}.md"
    reforzado.convertir_visible(ruta, md)
    return reforzado.verificar(ruta, md)


def main() -> int:
    ok = True

    # ── R3: tabla por renglones (no debe señalarse) vs por columnas (debe señalarse 🟡) ──
    renglones, _ = fabricar.sintetico_tabla(False)
    columnas, _ = fabricar.sintetico_tabla(True)
    inf_r = probar(renglones, "tabla_renglones")
    inf_c = probar(columnas, "tabla_columnas")

    ok &= checar("tabla por renglones → 🟢", inf_r["veredicto"] == "🟢",
                 f"{inf_r['veredicto']} código {inf_r['codigo_salida']}")
    r3 = any("R3" in h for d in inf_c["detalle"] for h in d["hallazgos"])
    ok &= checar("tabla por columnas → 🟡 con R3",
                 inf_c["veredicto"] == "🟡" and r3,
                 f"{inf_c['veredicto']} código {inf_c['codigo_salida']}, R3={r3}")
    evidencia = [t for t in inf_c["refuerzos"].get("tablas_columnas", []) if t.get("pagina") == 1]
    ok &= checar("tabla por columnas en evidencia (5×3)", bool(evidencia) and evidencia[0]["filas"] == 5
                 and evidencia[0]["columnas"] == 3, str(evidencia))

    # ── R9: página con capa OCR previa (imagen a página completa + texto invisible) ──
    pdf = fabricar.PdfSintetico()
    pdf.pagina()
    pdf.imagen(0, 0, fabricar.PdfSintetico.ANCHO, fabricar.PdfSintetico.ALTO,
               Image.new("RGB", (612, 792), "white"))
    pdf.texto(40, 400, "Registro digital: 12-11-2012", modo=3)
    pdf.texto(40, 380, "Monto: $1,380.00", modo=3)
    inf_o = probar(pdf.bytes(), "ocr")

    r9 = " ".join(h for d in inf_o["detalle"] for h in d["hallazgos"])
    ok &= checar("OCR previo → 🟡 con R9 localizando la cifra",
                 inf_o["veredicto"] == "🟡" and "R9" in r9 and "12-11-2012" in r9,
                 f"{inf_o['veredicto']}: {r9[:100]}")

    print("\nRESULTADO:", "OK" if ok else "FALLA")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
