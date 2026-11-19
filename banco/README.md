# pdf2ctx · banco de pruebas local

Banco local para la conversión **PDF → `.md` canónico (anclas `[[p. N]]`) → `.docx` de lectura humana**,
viendo página por página lo que dice el gate del kit Sprint 0.

El veredicto 🟢 🟡 🔴 y su código 0/1/3 salen **solo** del gate: `gate_reforzado.verificar()`, que corre
`gate_fidelidad.verificar()` del kit y le añade los refuerzos R1–R10. Esos refuerzos solo pueden endurecer
el veredicto, nunca relajarlo. El servidor guarda el informe tal cual y la UI solo lo pinta, junto con el
veredicto que dio el kit antes de los refuerzos.

| Archivo | Qué es |
|---|---|
| `servidor_local.py` | Servidor HTTP (biblioteca estándar + el kit + python-docx/Pillow). Solo `127.0.0.1`. |
| `banco.html` | La UI, con el diseño de itosturre.com: un solo archivo, sin CDNs ni recursos externos. |
| `../gate_reforzado.py` | Refuerzos del gate y convertidor `visible` (no es parte del kit: lo importa). |
| `../chunking.py` | Chunking jurídico del `.md` verificado en chunks semánticos (artículos, fracciones…) para RAG/LLM. |
| `../probar_chunking.py` | El gate del chunking (13 comprobaciones sobre un documento normativo sintético). |
| `probar_banco.py` | El gate de esta tarea: arranca el servidor y lo prueba de punta a punta. |
| `trabajo/` | Documentos subidos (en `.gitignore`: puede contener expedientes confidenciales). |

El kit (`gate_fidelidad.py`, `medir_tokens.py`, `probar_gate.py`) se importa desde la carpeta de arriba
vía `sys.path`; nunca se copia ni se edita.

## Arranque

```bash
cd pdf2ctx
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt "python-docx>=1.1,<2" "Pillow>=11" "anthropic>=1,<2"

.venv/bin/python banco/servidor_local.py              # → http://127.0.0.1:8787/
.venv/bin/python banco/servidor_local.py --puerto 9000
```

Abre **http://127.0.0.1:8787/** (o `http://localhost:8787/`). Ctrl+C lo detiene.

### OCR local en Windows

En Windows puedes leer imágenes sin clave de API, con el motor `Windows.Media.Ocr` y un idioma español
ya instalado. En **Completar y revisar**, selecciona **OCR local · esta PC** y pulsa **Leer con OCR local**.
Las imágenes se procesan en esta computadora; no se llama a Anthropic ni se descargan modelos.

```powershell
.\.venv\Scripts\python.exe banco\servidor_local.py
# Prueba con el motor OCR real, documentos sintéticos y sin API externa:
.\.venv\Scripts\python.exe banco\probar_ocr_local.py
```

- En una página escaneada o con OCR previo, lee la página completa; en páginas con texto nativo, solo
  los recortes de imágenes grandes que detectó R8. Usa hasta 3600 px en el lado mayor, sujeto al límite
  del motor y a un máximo de 24 millones de píxeles por render.
- `ocr_local.py` llama a `ocr_windows.ps1` con Windows PowerShell, sin cambiar políticas de ejecución.
  Si Windows o el idioma español no están disponibles, la interfaz muestra la causa y permite elegir
  la vía opcional de Claude, que requiere su propia clave y confirmación de envío.
- El texto recuperado se guarda en `trabajo/<id>/transcripciones/<n>.json`: incluye idioma, fecha,
  SHA-256 de la imagen y del PDF, y palabras con sus posiciones en píxeles. El motor no proporciona
  puntuaciones de confianza por palabra; no se inventan.
- La curaduría y las exportaciones identifican la procedencia como **OCR local**, en ámbar y
  **NO VERIFICADO**. El PDF, el Markdown base y el veredicto del gate conservan su función de referencia.
  Las lecturas locales ya guardadas no se sobrescriben al repetir el OCR.
- Coteja cifras y campos contra la imagen: el OCR puede confundir letras y dígitos. Importes dudosos
  como `45,600.OO` y cifras contradictorias bajo etiquetas como `Fecha` quedan en abstención.
- Para recuperar una línea en abstención, abre **Corregir esta línea contra la imagen**, escribe el
  texto exacto y pulsa **Guardar corrección cotejada**. El texto corregido se incorpora en ámbar a las
  exportaciones; el OCR original sigue guardado. **Cotejar lo incorporado** solo revisa lo que ya entró:
  no incorpora automáticamente cifras inciertas.
- Las correcciones se guardan en `curaduria_revision.json`, vinculadas al texto original y su SHA-256.
  `curaduria_historial.jsonl` registra cada corrección y decisión con su actor y fecha. Rechazar la región
  excluye sus correcciones; **Deshacer revisión y correcciones** restaura la clasificación del OCR y
  conserva el historial. Si cambia la transcripción, una corrección anterior queda en abstención.
- Regresión de esta revisión manual: `.\.venv\Scripts\python.exe banco\probar_correcciones.py`
  (OCR simulado con el error de ceros y letras; sin servicios externos).

### Llave de la API de Claude («Medir tokens»)

La llave vive en el **llavero de macOS**: ni en un archivo, ni en el historial de la terminal, ni en el repo.

1. Guárdala una sola vez **en tu Terminal**, no en un chat. El comando la pide sin mostrarla en pantalla:

   ```bash
   security add-generic-password -a "$USER" -s pdf2ctx-anthropic -U -w
   ```

2. Arranca el banco con `--llavero`. El servidor la lee al arrancar, solo en su memoria:

   ```bash
   .venv/bin/python banco/servidor_local.py --llavero
   ```

   La consola dirá `Medir tokens: disponible (llave del llavero «pdf2ctx-anthropic»)`. Sin `--llavero`, el banco no
   lee el llavero y «Medir tokens» queda deshabilitado.

3. Para el kit por línea de comandos, la llave pasa solo a ese proceso:

   ```bash
   ANTHROPIC_API_KEY="$(security find-generic-password -s pdf2ctx-anthropic -w)" \
     .venv/bin/python medir_tokens.py --pdf corpus/ley-iva-2026.pdf --modelo claude-sonnet-5-5
   ```

Para cambiarla, repite el paso 1 (`-U` la actualiza). Para borrarla:
`security delete-generic-password -s pdf2ctx-anthropic`. Fuera de macOS, define `ANTHROPIC_API_KEY` en el
entorno del proceso del servidor.

`count_tokens` **no cobra**: solo tiene un límite de solicitudes por minuto, según la documentación oficial. Aun
así, cada medición **envía el PDF a Anthropic**: úsalo solo con documentos públicos. La UI pide confirmación antes
de cada envío, y el servidor exige `confirmado: true`.

### Transcripción asistida con Claude (visión) · NO verificada

Las páginas que el gate marca porque su contenido está en imágenes (sin capa de texto, capa OCR previa o R8) se
pueden mandar a Claude para leer ese texto. Lo hace `banco/transcripcion.py` con el SDK oficial `anthropic` (MIT):

- **Capa aparte**: la transcripción nunca entra al `.md` verificado ni cambia el veredicto. En la UI aparece junto a
  la imagen como «Transcripción asistida · no verificada», y en el `.docx` con la etiqueta «NO VERIFICADA».
- **Modelo** `claude-sonnet-5-5`, esfuerzo `low`, página entera renderizada con su lado mayor a 1568 px. El
  fallback del servidor está activado (`fallbacks: "default"`): si el modelo declina, la API reintenta con otro
  dentro de la misma llamada y queda registrado qué modelo respondió.
- **El texto de la imagen es un dato**: si trae instrucciones, se transcriben, no se obedecen. Lo ilegible se
  marca como `[ilegible]`; las cifras nunca se adivinan.
- **Cuesta dinero real** (API de mensajes, no `count_tokens`) y **envía las imágenes a Anthropic**: la UI pide una
  confirmación por lote, y el servidor exige `confirmado: true` en cada página.
- **Evidencia**: `trabajo/<id>/transcripciones/<n>.json`, con texto, modelo, `request_id`, uso real de tokens
  (entrada y salida), fecha y SHA-256 de la imagen enviada.

Requiere la llave (`--llavero`) y la dependencia: `.venv/bin/python -m pip install "anthropic>=1,<2"`.

En páginas con capa de texto solo se manda **el recorte de cada imagen grande** (R8), así Claude no ve ni copia
el texto que ya está en la página. Sin capa de texto, o con una capa OCR previa, se manda la página completa.

### Curaduría documental antes de exportar (`banco/curaduria.py`)

No modifica el PDF, el `.md` base ni las transcripciones guardadas. Cada línea transcrita se compara contra el
texto de su página:

| Clase | Decisión |
|---|---|
| nuevo, cotejable contra la imagen | incorporar al consolidado, **en ámbar, pendiente de cotejo** |
| duplicado (ya está en la página o se repite) | excluir |
| decorativo (notas y descripciones del modelo) | excluir |
| incierto (`[ilegible]`, demasiado corto, o con cifras que contradicen la capa de texto u OCR) | abstenerse (ámbar) |

- **`consolidado.md`** es independiente del `.md` base: lleva bloques `<!-- pdf2ctx:asistido p=… region=…
  metodo=… request=… estado=ambar cotejo=… -->`. Al quitarlos queda, **byte por byte**, el `.md` base; si el
  `.md` base ya trae ese marcador, no se consolida.
- **`curaduria.json`** registra cada incorporación, exclusión y abstención con su página, región, método,
  `request_id`, motivo, estado y actor.
- **Revisión humana** por región: «Cotejado contra la imagen», «Rechazar» o «Deshacer». Se guarda aparte
  (`curaduria_revision.json`). El cotejo humano **no da verde**: queda «ámbar · cotejado, no verificado por el
  gate», y el veredicto de la página no cambia.
- El `.docx` incluye solo lo incorporado, etiquetado «NO VERIFICADO».
- Regresión: `.venv/bin/python banco/probar_curaduria.py` (21 casos, sin red).

Dependencias nuevas: python-docx 1.2.0 (MIT, trae lxml BSD-3 y typing_extensions PSF), Pillow 13.0.0
(MIT-CMU) y anthropic 1.17.0 (MIT; su árbol completo es MIT, BSD-3, Apache-2.0 o PSF). Nada GPL ni AGPL.

## Qué hace

1. **Subir PDF** → convertidor (`visible` por defecto, o `minimo` del kit) → resumen (veredicto del gate y
   del kit, código, páginas, clases de oráculo, residuo G3, sha256 del PDF y del `.md`, retiros declarados,
   avisos) y una fila por página: imagen renderizada en el servidor con pypdfium2 | texto `.md` editable |
   hallazgos del gate (G1–G5 del kit y R1–R10 del refuerzo).
   - `visible`: extrae sin giro y retira el texto que no se ve (modo invisible, blanco sobre blanco,
     microtexto, fuera de la página, tapado). Lo retirado se declara en `retiros.json` y el G5 del kit lo verifica.
   - `minimo`: la capa de texto tal cual. El gate reforzado marca 🔴 si ese `.md` trae texto no visible.
2. **Re-verificar**: tras editar el texto (por ejemplo, cambiar un dígito) el gate corre sobre el `.md`
   completo y la UI se repinta. Mientras haya cambios sin verificar, la UI lo avisa.
3. **Subir `.md` externo** (Docling u otro) y verificarlo contra el mismo PDF.
4. **Descargar** `.md`, `informe.json` y `.docx`. El `.docx` conserva la paginación (salto de página y marca
   «p. N» por página) y guarda el sha256 del PDF en sus propiedades (`identifier`, `keywords`, `comments`).
   - 🔴 → la exportación `.docx` se bloquea (HTTP 409).
   - 🟡 → el `.docx` lleva el encabezado «PÁGINAS NO VERIFICADAS: …» con esas páginas.
   - `.md` sin anclas → también se bloquea: sin anclas no hay paginación que conservar.
5. **Medir tokens** (opcional) con `medir_tokens.contar` (endpoint `count_tokens`). Antes de cada medición
   la UI pide confirmar que el PDF se enviará a la API de Anthropic, y el servidor exige `confirmado: true`.
   Sin `ANTHROPIC_API_KEY`, el botón queda deshabilitado con su causa y el servidor responde 412 sin tocar
   la red. No hay estimaciones heurísticas: solo cifras de la API; cada respuesta se guarda en
   `trabajo/<id>/tokens/`.
6. **Punto de extensión**: `CONVERTIDORES = {"visible": …, "minimo": …}` en `servidor_local.py`.
   Un convertidor recibe `(ruta_pdf, ruta_md)`, escribe un `.md` con una línea `[[p. N]]` por página y
   devuelve los retiros que declara (lista vacía si no quitó nada). Docling no está integrado todavía.
   Un `.md` externo no trae retiros: al subirlo se limpian los del convertidor.

## Diseño

Tomado de www.itosturre.com: la paleta (tinta `#050B14`, noche, papel, acero, cian `#3CC8F0`), el sello y el
logotipo «itosturre», los chips con punto, los botones píldora y el radio de 14 px. Los estados usan tu
vocabulario y tus colores: **Verificado** (verde) = 🟢, **Requiere confirmación** (ámbar) = 🟡 y
**No pasa** (lacre) = 🔴. Las tipografías de la marca (Gloock, IBM Plex Sans/Mono) **no se descargan**,
porque el banco no carga recursos externos: se usan si están instaladas en la máquina y, si no, sus respaldos
(Georgia y la tipografía del sistema).

## Seguridad

- Escucha solo en `127.0.0.1` (puerto 8787, configurable con `--puerto`). Un `Host` distinto de
  `localhost` o `127.0.0.1` recibe **403** (frena el DNS rebinding).
- Todo POST exige el token aleatorio que el servidor inyecta en el HTML (cabecera `X-Banco-Token`);
  sin él, **403**. El token cambia en cada arranque.
- CSP estricta (`default-src 'none'`, script con nonce, `connect-src 'self'`): la página no puede hablar
  con otro origen.
- Entradas: el PDF debe empezar con `%PDF-` y pesar ≤ 50 MB; el `.md` debe ser UTF-8 y pesar ≤ 20 MB;
  los ids son 32 caracteres hex (`../` → **400**); las páginas, `<número>.png`. Nada usa `shell=True`: el
  servidor solo llama a `security` (lista de argumentos) cuando se arranca con `--llavero`.
- Sin red saliente ni telemetría, salvo `count_tokens` tras un clic y una confirmación explícitos.
  La llave nunca llega al navegador, a las respuestas ni al registro.
- Si en el futuro se corre en WSL2 y el navegador de Windows no lo alcanza: **no** abrir `0.0.0.0`.

## El gate: `probar_banco.py`

```bash
.venv/bin/python banco/probar_banco.py corpus/<pdf real con cifras>.pdf [--conservar]
```

Sale `0` si todo pasa, `1` si algo falla y `2` si hay un error de uso. Arranca el servidor en un puerto
libre y comprueba:

- tantas anclas como páginas (contadas aparte con pypdfium2);
- re-verificar sin cambios da el mismo código y los mismos veredictos por página;
- alterar un dígito da código 1, la página 🔴 con hallazgo G2 y el `.docx` bloqueado (409);
- la imagen de página es PNG; una página inexistente da 404;
- el `.docx` abre con python-docx, trae una marca «p. N» por página con salto de página y el sha256 del
  PDF en sus propiedades; con 🟡 (mutante M11 del kit: una página rasterizada) lleva el encabezado
  «PÁGINAS NO VERIFICADAS» con exactamente esas páginas;
- un `Host` ajeno da 403; un POST sin token o con token falso da 403;
- un cuerpo que no es PDF da 400; un id con `../` da 400; un `Content-Length` mayor que el tope da 413;
- un `.md` externo se verifica contra el mismo PDF (sin anclas → 3 y `.docx` bloqueado);
- medir tokens sin llave da 412 **sin ninguna petición de red**, comprobado con un imitador local de
  `count_tokens` (`ANTHROPIC_BASE_URL` → `127.0.0.1`); con una llave de prueba exige confirmación y entrega
  las cifras exactas del imitador, y la llave no aparece en el HTML, la config, las respuestas, el registro
  ni en `trabajo/`. **Nunca llama a la API real.**

Deja su resumen en `trabajo/probar_banco.json`. Si algo falla, conserva la evidencia en
`trabajo/_probar_banco_<fecha>/`.

## Qué quedó verificado (4-XI-2026)

Entorno: macOS (Darwin 24.6), Python 3.14.2, pypdfium2 5.14.0, python-docx 1.2.0, Pillow 13.0.0.
PDF real: Ley del Impuesto al Valor Agregado 2026 (pública), 128 páginas, todas con capa de texto,
sha256 `f2b5b177…4a8e91a6`, copiada a `corpus/ley-iva-2026.pdf`.

| Verificación | Resultado |
|---|---|
| Precondición del kit: `probar_gate.py <pdf> --sabotaje G2` | sale **1** |
| Precondición del kit: `probar_gate.py <pdf>` | sale **0** (12 pasan) |
| `probar_banco.py corpus/ley-iva-2026.pdf` | **56 pasan · 0 fallan · código 0** |
| El gate del gate: 7 sabotajes del servidor (Host abierto, token ignorado, 🔴 sin bloquear el `.docx`, tokens sin revisar la llave, id sin validar, `.docx` sin saltos, re-verificar sin guardar) | los 7 hacen salir **1** a `probar_banco.py` |
| `git diff main` sobre los archivos del kit | vacío |
| UI en Chrome real (DevTools): subir, ver las 3 columnas, cambiar «12-11-2021» → «12-11-2022» en la p. 1, re-verificar → 🔴 código 1 con G2 y `.docx` bloqueado; revertir → 🟢; descargar `.docx` e `informe.json` desde los botones | funciona; sin errores de consola ni violaciones de CSP |

En el navegador aparecieron dos defectos que la API no muestra; ambos quedaron corregidos: listas vacías
que se pintaban como «null», y el repintado tras re-verificar, que pedía las 128 imágenes a la vez en lugar
de cargarlas al desplazarse.

## Correcciones verificadas (rama `feat/pdf2ctx-correcciones`, 4-XI-2026)

- `probar_banco.py` con el gate reforzado y el convertidor `visible` por defecto: **56 pasan · 0 fallan**.
- `defectos/probar_defectos.py`: corregidas **V1–V10** (R1–R10). La localización de cifras por cotejar en OCR (V9)
  está en R9; leer la imagen para corregir el valor queda fuera (exige un motor OCR).
  Ver `defectos/INFORME_COBERTURA.md`.
- Ley del IVA limpia: 🟢 con los dos convertidores. El convertidor `visible` sobre la ley con la instrucción
  oculta inyectada da exactamente el mismo `.md` que la ley limpia (mismo SHA-256) y declara el retiro.
- UI rediseñada, probada en Chrome: el veredicto del kit y el del gate se ven lado a lado (p. ej., kit
  «Verificado» → gate «No pasa» por R1), sin errores de consola ni violaciones de CSP.

## Qué NO quedó verificado

- **La API real de Anthropic**: nunca se llamó; la medición solo se probó contra el imitador local.
- **Un escaneo real** (`sin_texto`) o con OCR previo (`ocr_previo`): el 🟡 se probó con el mutante M11 del
  kit (una página del PDF real rasterizada), no con un escaneo verdadero.
- **Otros navegadores** (Firefox, Safari) y el diálogo de confirmación de tokens con llave real en la UI.
- **Windows/WSL2**: todo corrió en macOS.
- **Python 3.12** (con el que se probó el kit): aquí se usó 3.14.2.
- **`retiros.json` de terceros**: el banco usa los retiros que declara su convertidor, pero no permite subir
  los de un convertidor externo junto con su `.md`.
- **Tipografías exactas de la marca**: se ven solo si Gloock e IBM Plex están instaladas en la máquina.
- **Costo de los refuerzos**: R1 renderiza cada página a 144 dpi (unos 3 s para las 128 páginas de la ley).
- **PDFs muy grandes**: el render y el gate se serializan con un candado (PDFium no es seguro entre
  hilos); con cientos de páginas, una re-verificación espera a los renders en curso.
- El `.docx` es de lectura: una línea del `.md` = un párrafo; no reconstruye tablas ni formato.
- `trabajo/` crece sin límite: se borra a mano.
