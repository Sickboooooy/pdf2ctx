# pdf2ctx · suite de PDFs defectuosos (fuzz + regresión)

Comprueba si el sistema (banco: convertidor `visible` por defecto, o `minimo` donde el caso lo pide, +
`gate_reforzado`, que corre el gate del kit, + exportación `.docx`) **detecta errores, conserva evidencia y evita
producir texto jurídico aparentemente válido cuando no lo es**.

**Estado (rama `feat/pdf2ctx-correcciones`):** `gate_reforzado.py` corrigió **V1, V2, V4, V5, V6, V7, V8 y V10**.
Sus comprobaciones quedan como **guardias de regresión** (`CORREGIDAS` en `probar_defectos.py`): si vuelven a
fallar, salen FALLA. Quedan pendientes **V3** (tabla dibujada por columnas: requiere análisis de maquetación) y
**V9** (discrepancia entre capa OCR e imagen: requiere OCR). Hay casos de control contra falsos positivos (A8:
marca de agua gris y texto blanco sobre franja oscura) y casos con el convertidor `minimo` (A1m, A6m, F1m) que
prueban que el gate *detecta* lo que ese convertidor no corrige.

| Archivo | Qué es |
|---|---|
| `fabricar.py` | Crea los fixtures y su verdad de referencia (ground truth). |
| `probar_defectos.py` | Pasa cada caso por el sistema, en un proceso aislado, y lo compara con la verdad. |
| `INFORME_COBERTURA.md` | Informe de la última corrida completa: resultados, brechas y vulnerabilidades pendientes. |
| `resultados/` | Evidencia de cada corrida (en `.gitignore`). |

## Correr

```bash
cd pdf2ctx
.venv/bin/python defectos/probar_defectos.py corpus/ley-iva-2026.pdf     # ~40 s
.venv/bin/python defectos/probar_defectos.py corpus/ley-iva-2026.pdf --solo A,B
```

Salida: `0` = ninguna FALLA ni PASA INESPERADO · `1` = alguna · `2` = original ausente o alterado.
Las **fallas esperadas** no hacen salir 1, pero se cuentan y se reportan siempre: no son pases.

## Fixtures

- **Sintéticos** (texto ficticio, escritos con la biblioteca estándar): se sabe con exactitud qué se ve y qué no.
- **Públicos alterados**: copias de la Ley del IVA 2026 con un defecto controlado (pypdfium2).
- **Mixtos**: imagen de una página pública dentro de un PDF sintético (escaneo borroso, capa OCR).
- **Fuzz**: bytes del original truncados o alterados con semilla fija.

El original **nunca se escribe**: se identifica por SHA-256 (`f2b5b177…4a8e91a6`) y se verifica antes de
fabricar, durante y al terminar. Los 39 fixtures salen **idénticos byte a byte** en cada corrida; la suite lo
comprueba fabricando dos veces. En las copias que guarda pypdfium2 se fija el segundo elemento de `/ID`, que
PDFium vuelve aleatorio, con un valor de la misma longitud: el xref no se mueve.

## Qué prueba

| Grupo | Defecto | Lo deseado |
|---|---|---|
| A · texto oculto | instrucción maliciosa en modo invisible, blanco sobre blanco, microtexto, fuera de página, tapada (sintéticos) e inyectada en la ley pública | no certificar 🟢 texto que no se ve; no exportarlo como `.docx` válido |
| A7 · instrucción visible | instrucción y `<script>` **visibles** | conservarlos literal, como datos del documento |
| B · tablas | columnas o renglones intercambiados en el `.md`; fechas intercambiadas en la ley; tabla dibujada por columnas | detectar la permutación; conservar renglón con renglón |
| C · fuzz | truncados (10–99 %), sin cola, solo cabecera, 64 KiB en ceros, 12 alteraciones al azar | fallar de forma controlada; si se acepta, avisar del daño |
| D · escaneo | página borrosa e inclinada sin capa de texto | 🟡 NO VERIFICABLE, nunca 🟢 |
| E · OCR | capa OCR que contradice la imagen (y un control fiel) | nunca 🟢; señalar la discrepancia |
| F · rotación | páginas a 90°, 180° y 270° | conservar orden, texto y orientación |
| G · páginas | falta o se duplica la p. 10 en el PDF (con folios «N de 128»); y en el `.md` | señalar el salto o la duplicación |
| H · firmas y sellos | anotaciones de firma y sello; sello como imagen con folio | conservarlos o avisar con su localización |

## Dos fidelidades

- **Capa textual**: el `.md` contra la capa de texto del PDF. Es lo que mide el gate.
- **Visual**: el `.md` contra lo que se ve en la página, según la verdad de referencia del fixture.

El informe pone las dos lado a lado y marca la **brecha**: casos en que el sistema certifica 🟢 algo que no
es fiel a lo visible. Como evidencia adicional sin OCR, la *sonda de tinta* mira si cada carácter de la capa
de texto deja tinta en la página renderizada. No lee la imagen ni forma parte del sistema.

## Estados

| Estado | Significa |
|---|---|
| PASA | cumple lo deseado |
| FALLA ESPERADA | no cumple; es una vulnerabilidad conocida y declarada (`V1`…`V10`) |
| FALLA | no cumple y no estaba declarada: regresión o defecto nuevo → sale 1 |
| PASA INESPERADO | cumple aunque se declaró vulnerable → sale 1 hasta que se actualice `CASOS` |
| NO APLICA | la condición no se dio (p. ej., el archivo dañado se rechazó) |

**Cuando se corrija una vulnerabilidad**, sus comprobaciones saldrán PASA INESPERADO: quita la `Vn` de esos
casos en `construir_casos()` (y de `FUZZ_CON_V4` si aplica). Así la mejora queda fijada como regresión.

## Evidencia de cada corrida

`resultados/<AAAAMMDD-HHMMSS>/`:

- `fixtures/`: cada PDF, su `.verdad.json` y `fixtures.json` con su SHA-256.
- `casos/<id>/`: `entrada.json`, `resultado.json`, `documento.md`, `informe.json` (del gate), `docx.txt` y `stderr.txt`.
- `original_minimo.md`: la referencia del original sano para el fuzz.
- `resumen.json`: cada comprobación; su SHA-256 queda en el informe.
- `INFORME_COBERTURA.md`: el informe de esa corrida.

## Sin OCR, sin modelos, sin red

La suite no usa OCR, modelos ni servicios externos. Usa solo pypdfium2, Pillow y python-docx, que ya eran
dependencias del banco. No toca el kit: lo importa.
