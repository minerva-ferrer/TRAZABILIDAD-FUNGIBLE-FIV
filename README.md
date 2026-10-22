# Trazabilidad de material fungible · Laboratorio de FIV

Aplicación web para registrar las recepciones de material fungible del laboratorio y detectar automáticamente **cada cambio de lote, proveedor o caducidad** entre dos recepciones seguidas del mismo producto. Cada cambio queda pendiente hasta que alguien lo marca como revisado, con su nombre, la fecha y una nota.

- **Panel:** lote en uso de cada producto, caducidad y cambios pendientes de revisar.
- **Registrar hoja:** importa la hoja PDF rellenada en el ordenador, lee la foto de una hoja manuscrita (opcional) o permite teclear las filas. Siempre hay un paso de revisión antes de guardar.
- **Ficha de producto:** historia de lotes, todas las recepciones y avisos (posibles duplicados, falta de nº FIV, fechas fuera de orden).
- **Cambios:** registro de cambios con su revisión; exportable a CSV.
- **Usuarios y auditoría:** cada acción queda registrada con usuario y hora. Las recepciones no se borran: se **anulan** con un motivo.

Los históricos de cada producto (por ejemplo el del **327-26**, transcrito de la hoja en papel) se cargan desde la aplicación. **No están en este repositorio**, porque es público: ver `historico/FORMATO.md`.

---

## Instalación en Windows sin Python (recomendado)

GitHub genera el programa listo para usar, `TrazabilidadFIV.exe`. No hace falta instalar nada.

1. En esta página de GitHub, entra en **Releases** (columna derecha) y abre la versión más reciente.
2. En **Assets**, descarga `TrazabilidadFIV-windows.zip` y descomprímelo en el PC que hará de servidor, por ejemplo en `C:\Trazabilidad`.
3. Haz doble clic en `TrazabilidadFIV.exe`. Si Windows muestra «Windows protegió su PC», pulsa **Más información → Ejecutar de todas formas**.
4. Se abre una ventana negra (no la cierres: es el programa en marcha) y el navegador. Crea tu usuario administrador.
5. Carga el histórico en **Usuarios → Cargar histórico de un producto** (archivo `.json`).

Los datos se guardan en la carpeta `datos`, junto al `.exe`. Para actualizar a una versión nueva, sustituye solo el `.exe`.

**Versiones nuevas:** cada cambio en la rama `main` hace que GitHub compile el programa (pestaña **Actions**, unos 5 minutos) y lo publique en **Releases** con el número de `VERSION` de `app.py`. Para sacar una versión nueva, sube ese número (por ejemplo a `1.1.0`).

## Instalación en Windows con Python

1. **Instala Python.** Descárgalo de [python.org/downloads](https://www.python.org/downloads/) (versión 3.9 o posterior). En la primera pantalla del instalador marca **«Add python.exe to PATH»**.
2. **Descarga el programa.** En GitHub, pulsa el botón verde **Code → Download ZIP** y descomprímelo, por ejemplo en `C:\Trazabilidad`.
3. **Arráncalo.** Haz doble clic en `iniciar.bat`. Se abrirá una ventana negra (no la cierres: es el programa en marcha) y el navegador en `http://localhost:8000`.
4. **Crea tu usuario.** La primera vez la página pide crear el administrador.
5. **Da de alta al equipo** desde la pestaña **Usuarios**.

Para que otros PCs del laboratorio entren, usan la dirección del PC donde corre el programa: `http://NOMBRE-DEL-PC:8000` (o su IP). Si Windows pregunta por el cortafuegos, permite el acceso en **redes privadas**.

Para que arranque solo al encender el PC: pulsa `Win + R`, escribe `shell:startup` y coloca ahí un acceso directo a `iniciar.bat`.

### En Linux o macOS

```bash
python3 -m pip install -r requirements.txt   # opcional
python3 app.py
```

### Con Docker (recomendado para un servidor de la clínica)

```bash
docker compose up -d
```

Los datos quedan en la carpeta `datos/` junto al `docker-compose.yml`.

---

## Dónde están los datos y cómo hacer copias

Todo se guarda en un único archivo: `datos/trazabilidad.db`.

- **Copia manual:** `python app.py copia` crea una copia con fecha en `datos/copias/`. Con `--destino` se elige otra carpeta, por ejemplo una unidad de red: `python app.py copia --destino "\\servidor\copias\trazabilidad"`.
- **Copia automática diaria en Windows:** en el Programador de tareas, crea una tarea diaria que ejecute `python C:\Trazabilidad\app.py copia --destino <carpeta de copias>`.
- **Restaurar:** detén el programa y sustituye `datos/trazabilidad.db` por la copia.

La carpeta `datos/` y los históricos están excluidos del repositorio: **los datos reales nunca se suben a GitHub.** Este repositorio es público: no subas a él archivos con datos del laboratorio.

---

## Hojas de recepción

En `plantillas/` está la hoja del 327-26 en PDF (A4 vertical) con dos usos:

- **Rellenarla en el ordenador** con Acrobat Reader, Chrome o Edge. Después se sube en **Registrar hoja → PDF rellenado** y los datos se leen tal cual, sin errores de lectura.
- **Imprimirla** y rellenarla a mano. Para registrarla, se fotografía y se sube como imagen (requiere la lectura automática, ver abajo) o se teclean las filas.

Para crear la hoja de otro producto (necesita `reportlab`):

```bash
python herramientas/generar_hoja.py --codigo 412-10 --nombre "Placas Petri 35 mm" --proveedores "Nunc; Falcon"
python herramientas/generar_hoja.py --todos     # una hoja por cada producto dado de alta
```

---

## Lectura automática de fotos (opcional)

Para leer fotos de hojas manuscritas, el programa usa la API de Anthropic (Claude). Hace falta una clave de API de la clínica, que se crea en [console.anthropic.com](https://console.anthropic.com).

1. Copia `datos/config.ejemplo.json` como `datos/config.json`.
2. Pon tu clave en `anthropic_api_key` y reinicia el programa.

Ten en cuenta que **la foto de la hoja se envía a Anthropic** para leerla. Las hojas no deben contener datos de pacientes. Si preferís que ninguna imagen salga de la clínica, no configures la clave: el PDF rellenable y la entrada manual funcionan igual.

---

## Validación

Para el sistema de calidad (UNE 179007 / ISO 9001) conviene documentar la validación del programa antes de usarlo en real:

1. Ejecuta las pruebas automáticas y guarda el resultado:
   ```bash
   python -m unittest discover -s pruebas -v
   ```
   Comprueban la detección de cambios de lote, proveedor y caducidad, los avisos del registro, la anulación y la carga de históricos (con datos inventados).
2. Haz una prueba real con varias hojas de productos conocidos y compara lo registrado con el papel.
3. Registra quién aprobó la validación, la versión del programa (aparece en `app.py`, `VERSION`) y la fecha.

El registro de auditoría completo se exporta desde **Usuarios → Exportar registro de auditoría**.

---

## Seguridad

- El programa está pensado para la **red interna** del laboratorio. No lo expongas a Internet.
- Las contraseñas se guardan cifradas (PBKDF2) y las sesiones caducan a las 12 horas.
- Si informática quiere HTTPS, puede ponerlo delante con su proxy habitual (IIS, nginx, Caddy).

## Estructura

```
app.py                     servidor (solo librería estándar de Python)
static/                    interfaz web, logo y fuentes
historico/FORMATO.md       formato de los archivos de histórico (los reales no se suben)
plantillas/                hojas PDF rellenables
herramientas/              generador de hojas y fuentes (licencia OFL)
pruebas/                   pruebas automáticas
datos/                     base de datos y configuración (no se sube a GitHub)
```
