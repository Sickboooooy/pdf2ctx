# pdf2ctx · Sprint 0 — medición + gate en rojo

**Objetivo:** medir en tu corpus cuánto cuesta enviar cada PDF como imagen + texto frente a enviarlo como `.md`, y dejar un gate de fidelidad que **reprueba solo**. Sin limpieza, sin Docling, sin OCR: eso es Sprint 1.

**Estado del kit (4-XI-2026, Python 3.12 · pypdfium2 5.14.0):** sobre un PDF real del proyecto y un PDF interno de patrones jurídicos ficticios, 23 casos pasan, 1 no aplica (el PDF real no cita fracciones ni artículos) y 0 fallan; cada sabotaje (G2, G4, G5) hace reprobar la suite con código 1. `medir_tokens.py` se probó en seco y contra un servidor local que imita `count_tokens`. ⚠️ **No se ha corrido contra la API real**: esa es la primera corrida con tu `ANTHROPIC_API_KEY`.

---

## Regla cero — test de aptitud

| Criterio | ¿Cumple? |
|---|---|
| Se repite al menos cada semana | ✅ cada PDF que entra al IDE |
| Algo reprueba el output por sí solo | ✅ `gate_fidelidad.py` con códigos de salida |
| El agente lo hace de punta a punta | ✅ scripts sin intervención manual |
| "Hecho" es objetivo | ✅ códigos 0/1/3; cada veredicto trazable a su informe JSON |

## Archivos

| Archivo | Función |
|---|---|
| `gate_fidelidad.py` | Oráculo (capa de texto vía PDFium) + gate G1–G5. Subcomandos `minimo` y `verificar` |
| `probar_gate.py` | El gate del gate: 12 casos (limpio + 11 mutantes) derivados de tus PDFs reales |
| `medir_tokens.py` | Baseline con `count_tokens`: PDF nativo vs `.md`; guarda cada respuesta JSON |
| `corpus_sprint0.json` | Manifiesto de los 10 PDFs reales |
| `requirements.txt` | Única dependencia externa: pypdfium2 (BSD-3/Apache-2.0) |

---

## ▸ LOOP SPEC

```
▸ LOOP SPEC — pdf2ctx · Sprint 0
RAMA: feat/pdf2ctx-sprint0   (prohibido commit directo a main)

GOAL (todo a la vez, binario):
  1. probar_gate.py --corpus corpus_sprint0.json sale con código 0:
     cada caso aplicable PASA y cada caso aplicó al menos una vez en el corpus.
  2. Ningún mutante produce 🟢 (cero falsos verdes).
  3. Las páginas sin capa de texto salen 🟡, nunca 🟢 ni 🔴 (scan-01, scan-02).
  4. baseline_sprint0.csv tiene las 10 filas en MEDIDO, NO COMPARABLE o NO MEDIDO
     con causa; ninguna cifra sin su JSON en respuestas/; ninguna fila con 0 por falla.

PRECONDICIÓN (a mano, una vez):
  a. Coloca los 10 PDFs reales en corpus/ y ajusta corpus_sprint0.json.
  b. Corre  python probar_gate.py --corpus corpus_sprint0.json --sabotaje G2
     DEBE salir 1. Repite con G4 y con G5. Si alguno sale 0, la suite no prueba
     nada: corrígela ANTES de iterar.

CADA ITERACIÓN:
  1. corre probar_gate.py y LEE cada FALLA entera
     (resultados_sprint0/mutantes/<doc>/<caso>.informe.json)
  2. elige la ÚNICA falla de mayor impacto (prioriza cualquier 🟢 indebido)
  3. cambio MÍNIMO en gate_fidelidad.py: una regla de normalización, no un refactor
  4. re-corre probar_gate.py completo + las tres corridas de sabotaje

PROHIBIDO: relajar un esperado de probar_gate.py para que pase. Mover un umbral
  (COBERTURA_MIN, UMBRAL_*, RETIRO_MAX) es juicio: requiere aprobación humana y
  se anota en el PR.

VERIFY: los cuatro puntos del GOAL en verde a la vez, cada uno trazable a
        resultados_sprint0/probar_gate.json, a los informes por caso y a respuestas/.
STOP WHEN: VERIFY pasa · O 8 iteraciones · O la misma falla 2 vueltas seguidas
           (diagnóstico equivocado: para y escala a humano).
AL PARAR: resume qué cambió, qué sigue rojo y por qué. NO hagas merge; deja el PR listo.
DISCIPLINA: solo el diff + la salida de probar_gate.py cada vuelta. Un agente, una tarea.
```

## Corpus (10 PDFs reales, no sintéticos)

| id | Categoría | Qué prueba |
|---|---|---|
| ley-01, ley-02 | Ley o reglamento nativo | artículos, fracciones, encabezados repetidos (G3) |
| sent-01, sent-02 | Sentencia en versión pública | firma electrónica, leyendas de datos eliminados |
| scan-01 | Escaneo sin capa de texto | abstención 🟡 |
| scan-02 | Escaneo con OCR previo | oráculo débil 🟡 |
| tabla-01, tabla-02 | Tablas, anexos, tarifas | cifras densas |
| tesis-01 | Tesis o jurisprudencia del SJF | registro digital, clave |
| largo-01 | Más de 100 páginas | límites de la API |

## Comandos

```bash
git checkout -b feat/pdf2ctx-sprint0
pip install -r requirements.txt

# Precondición: con el gate saboteado la suite DEBE salir 1
python probar_gate.py --corpus corpus_sprint0.json --sabotaje G2

# El loop: meta = código 0
python probar_gate.py --corpus corpus_sprint0.json

# Baseline de tokens (mide con el modelo que usarás en producción)
export ANTHROPIC_API_KEY=...
python medir_tokens.py --corpus corpus_sprint0.json --modelo claude-sonnet-5-5

# Un documento suelto
python gate_fidelidad.py minimo doc.pdf -o doc.md
python gate_fidelidad.py verificar doc.pdf doc.md --informe doc.informe.json
```

## Semántica

**Códigos de salida (los tres scripts):** `0` todo 🟢 / medido · `1` algún 🔴 o falla · `3` abstención (🟡, NO MEDIDO, NO COMPARABLE, caso sin cobertura) · `2` error de uso.

**Chequeos del gate**

| Chequeo | Qué verifica | Bloquea |
|---|---|---|
| G1 | Cada página tiene su ancla `[[p. N]]`, en orden y sin duplicados | sí |
| G2 | Cifras y referencias (artículo, fracción, inciso) del original están en el `.md`, y no aparecen otras nuevas | sí |
| G3 | Residuo: líneas repetidas entre páginas | no (métrica de eficiencia) |
| G4 | Cobertura de palabras ≥ 0.97 | sí |
| G5 | Cada retiro declarado existe en el original | sí |

**Clases de oráculo por página:** `texto` (oráculo confiable) · `ocr_previo` (imagen ≥ 85 % de la página con capa OCR: oráculo débil, máximo 🟡) · `sin_texto` (sin oráculo: 🟡 con la lista de cifras por cotejar contra la imagen).

**Contrato del `.md`:** una línea `[[p. N]]` por página del original, en orden. Lo que el convertidor quite (folios, encabezados) se declara en `retiros.json`:

```json
{"retiros": [{"pagina": 3, "texto": "PODER JUDICIAL DE LA FEDERACIÓN", "motivo": "encabezado repetido"}]}
```

Toda eliminación se declara; lo que desaparece sin declararse es pérdida. Un retiro mayor a 20 % de la página obliga revisión humana.

## Límites conocidos

1. Fechas y cifras escritas en letra ("quince de septiembre de dos mil veinticuatro") todavía no son invariantes; solo las que llevan dígitos.
2. El oráculo es la capa de texto: si está corrupta (fuentes sin mapa Unicode), el gate hereda el error. Revisa con `pdffonts` las páginas raras.
3. El `.md` mínimo sale de la misma capa de texto que el oráculo: mide tokens, no prueba fidelidad. La fidelidad real se mide en Sprint 1, cuando el convertidor sea Docling u OpenDataLoader.
4. `count_tokens` es una estimación y depende del modelo; la doc lo dice expresamente.
5. Escaneos sin OCR: el ahorro sale NO COMPARABLE hasta que exista OCR (Sprint 1).
6. Sin anclas y con páginas escaneadas, el gate no puede atribuir pérdidas: todo queda 🟡, que no es un pase.

## Fuera de alcance (Sprint 1 en adelante)

Limpieza por reglas de dominio · adaptador Docling/OpenDataLoader que emita anclas y `retiros.json` · OCR · chunking jurídico · servidor MCP y hook de Claude Code.
