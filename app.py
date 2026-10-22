#!/usr/bin/env python3
"""
Trazabilidad de material fungible — Laboratorio de FIV.

Servidor web pequeño que guarda las recepciones de material en una base de
datos SQLite, detecta los cambios de lote, proveedor y caducidad entre
recepciones seguidas de un mismo producto y deja constancia de quién los
revisó.

Solo necesita Python 3.9 o posterior. Dos funciones son opcionales:
  * importar hojas PDF rellenables  -> paquete «pypdf»
  * leer fotos de hojas manuscritas -> clave de la API de Anthropic

Uso:
  python app.py                      arranca el servidor (puerto 8000)
  python app.py crear-usuario        crea un usuario desde la consola
  python app.py cargar-historico     carga los históricos de la carpeta historico/
  python app.py copia                hace una copia de seguridad de la base de datos
"""
from __future__ import annotations

import argparse
import base64
import csv
import getpass
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, date
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

VERSION = "1.1.0"
EMPAQUETADO = getattr(sys, "frozen", False)  # programa .exe generado en GitHub
# Recursos de solo lectura (interfaz, histórico, fuentes): junto al código o dentro del .exe
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
# Carpeta del programa: donde se guardan los datos (al lado del .exe)
CARPETA_PROGRAMA = Path(sys.executable).resolve().parent if EMPAQUETADO else BASE
STATIC = BASE / "static"
HISTORICO = BASE / "historico"
DATOS = Path(os.environ.get("TRAZ_DATOS", CARPETA_PROGRAMA / "datos")).resolve()
DB_PATH = DATOS / "trazabilidad.db"
CONFIG_PATH = DATOS / "config.json"

SESION_HORAS = 12
MAX_CUERPO = 30 * 1024 * 1024  # 30 MB (fotos y PDF en base64)
COOKIE = "traz_sesion"

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS usuarios(
  id INTEGER PRIMARY KEY,
  usuario TEXT UNIQUE NOT NULL,
  nombre TEXT NOT NULL,
  hash TEXT NOT NULL,
  sal TEXT NOT NULL,
  admin INTEGER NOT NULL DEFAULT 0,
  activo INTEGER NOT NULL DEFAULT 1,
  creado TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sesiones(
  token TEXT PRIMARY KEY,
  usuario_id INTEGER NOT NULL,
  caduca REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS productos(
  codigo TEXT PRIMARY KEY,
  nombre TEXT NOT NULL,
  proveedores TEXT NOT NULL DEFAULT '[]',
  creado TEXT NOT NULL,
  creado_por INTEGER
);
CREATE TABLE IF NOT EXISTS recepciones(
  id TEXT PRIMARY KEY,
  producto TEXT NOT NULL REFERENCES productos(codigo),
  fecha TEXT NOT NULL,
  proveedor TEXT NOT NULL DEFAULT '',
  cajas TEXT NOT NULL DEFAULT '',
  lote TEXT NOT NULL,
  caducidad TEXT NOT NULL DEFAULT '',
  fiv TEXT NOT NULL DEFAULT '',
  firma TEXT NOT NULL DEFAULT '',
  notas TEXT NOT NULL DEFAULT '',
  dudosa INTEGER NOT NULL DEFAULT 0,
  origen TEXT NOT NULL DEFAULT 'manual',
  confirmado_por INTEGER,
  confirmado TEXT,
  anulada INTEGER NOT NULL DEFAULT 0,
  anulada_por INTEGER,
  anulada_fecha TEXT,
  motivo_anulacion TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS rec_producto ON recepciones(producto);
CREATE TABLE IF NOT EXISTS revisiones(
  recepcion_id TEXT PRIMARY KEY REFERENCES recepciones(id),
  revisado_por INTEGER,
  fecha TEXT NOT NULL,
  nota TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS auditoria(
  id INTEGER PRIMARY KEY,
  fecha TEXT NOT NULL,
  usuario_id INTEGER,
  accion TEXT NOT NULL,
  detalle TEXT NOT NULL DEFAULT ''
);
"""

CODIGO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
FECHA_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CADUCIDAD_RE = re.compile(r"^(\d{4}-\d{2}(-\d{2})?)?$")


# ---------------------------------------------------------------- utilidades
def ahora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def conectar() -> sqlite3.Connection:
    DATOS.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def iniciar_bd() -> None:
    con = conectar()
    try:
        con.executescript(SCHEMA)
    finally:
        con.close()


def auditar(con, usuario_id, accion, detalle) -> None:
    con.execute(
        "INSERT INTO auditoria(fecha, usuario_id, accion, detalle) VALUES (?,?,?,?)",
        (ahora(), usuario_id, accion, json.dumps(detalle, ensure_ascii=False)),
    )


def hash_clave(clave: str, sal: str | None = None) -> tuple[str, str]:
    sal = sal or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", clave.encode(), bytes.fromhex(sal), 240_000).hex()
    return h, sal


def leer_config() -> dict:
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
    if os.environ.get("ANTHROPIC_API_KEY"):
        cfg["anthropic_api_key"] = os.environ["ANTHROPIC_API_KEY"]
    if os.environ.get("ANTHROPIC_MODEL"):
        cfg["anthropic_model"] = os.environ["ANTHROPIC_MODEL"]
    return cfg


def reportlab_disponible() -> bool:
    try:
        import reportlab  # noqa: F401
        return True
    except ImportError:
        return False


def generar_hoja_pdf(producto: dict) -> bytes:
    """Genera la hoja de recepción rellenable de un producto y devuelve el PDF."""
    import tempfile
    sys.path.insert(0, str(BASE))
    from herramientas.generar_hoja import generar
    with tempfile.TemporaryDirectory() as tmp:
        ruta = Path(tmp) / "hoja.pdf"
        generar(producto["codigo"], producto["nombre"], [p["nombre"] for p in producto["proveedores"]], str(ruta))
        return ruta.read_bytes()


def pypdf_disponible() -> bool:
    try:
        import pypdf  # noqa: F401
        return True
    except ImportError:
        return False


# ------------------------------------------------- lógica de trazabilidad
def norm(s) -> str:
    """Normaliza lotes y proveedores para compararlos (mayúsculas, sin espacios ni asteriscos)."""
    return re.sub(r"\s+", "", str(s or "").replace("*", "")).upper()


def clave_orden(r: dict):
    fiv = re.sub(r"\D", "", str(r.get("fiv") or ""))
    return (r.get("fecha") or "", int(fiv) if fiv else 0)


def comparar(anterior: dict, actual: dict) -> list[str]:
    """Tipos de cambio entre dos recepciones seguidas del mismo producto."""
    tipos = []
    if norm(actual["lote"]) != norm(anterior["lote"]):
        tipos.append("lote")
    if actual.get("proveedor") and anterior.get("proveedor") and norm(actual["proveedor"]) != norm(anterior["proveedor"]):
        tipos.append("proveedor")
    if "lote" not in tipos and actual.get("caducidad") and anterior.get("caducidad") and actual["caducidad"] != anterior["caducidad"]:
        tipos.append("caducidad")
    return tipos


def detectar_cambios(recepciones: list[dict]) -> list[dict]:
    """Recorre las recepciones válidas de un producto en orden y devuelve cada cambio."""
    rs = sorted((r for r in recepciones if not r.get("anulada")), key=clave_orden)
    cambios = []
    for anterior, actual in zip(rs, rs[1:]):
        tipos = comparar(anterior, actual)
        if tipos:
            cambios.append({"id": actual["id"], "tipos": tipos, "anterior": anterior, "actual": actual})
    return cambios


def detectar_anomalias(recepciones: list[dict]) -> list[dict]:
    rs = sorted((r for r in recepciones if not r.get("anulada")), key=clave_orden)
    out, vistos = [], set()
    for i, r in enumerate(rs):
        if r.get("dudosa"):
            out.append({"id": r["id"], "fecha": r["fecha"], "mensaje": "Lectura dudosa" + (f": {r['notas']}" if r.get("notas") else "")})
        elif not r.get("fiv"):
            out.append({"id": r["id"], "fecha": r["fecha"], "mensaje": "Sin nº FIV"})
        k = (r["fecha"], norm(r["lote"]))
        if k in vistos and not r.get("dudosa"):
            out.append({"id": r["id"], "fecha": r["fecha"], "mensaje": "Posible duplicado: mismo día y mismo lote que otra recepción"})
        vistos.add(k)
        if i:
            pf, f = clave_orden(rs[i - 1])[1], clave_orden(r)[1]
            if pf and f and f < pf:
                out.append({"id": r["id"], "fecha": r["fecha"],
                            "mensaje": f"El nº FIV ({r['fiv']}) es menor que el de la recepción anterior ({rs[i - 1]['fiv']}). Revisa la fecha."})
    return out


def fin_caducidad(c: str) -> date | None:
    if not c:
        return None
    try:
        p = [int(x) for x in c.split("-")]
        if len(p) == 2:
            nxt = date(p[0] + (p[1] == 12), p[1] % 12 + 1, 1)
            return date.fromordinal(nxt.toordinal() - 1)
        return date(p[0], p[1], p[2])
    except ValueError:
        return None


# ---------------------------------------------------------- consultas
def fila(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in ("dudosa", "anulada"):
        if k in d:
            d[k] = bool(d[k])
    return d


def estado(con) -> dict:
    usuarios = {r["id"]: r["nombre"] for r in con.execute("SELECT id, nombre FROM usuarios")}
    revisiones = {r["recepcion_id"]: fila(r) for r in con.execute("SELECT * FROM revisiones")}
    recs: dict[str, list] = {}
    for r in con.execute("SELECT * FROM recepciones"):
        d = fila(r)
        d["confirmado_por_nombre"] = usuarios.get(d["confirmado_por"]) if d["confirmado_por"] else None
        d["anulada_por_nombre"] = usuarios.get(d["anulada_por"]) if d["anulada_por"] else None
        recs.setdefault(d["producto"], []).append(d)
    hoy = date.today()
    productos = []
    for p in con.execute("SELECT * FROM productos ORDER BY codigo"):
        lista = sorted(recs.get(p["codigo"], []), key=clave_orden)
        validas = [r for r in lista if not r["anulada"]]
        cambios = []
        for ch in detectar_cambios(lista):
            rev = revisiones.get(ch["id"])
            if rev:
                rev = dict(rev, revisado_por_nombre=usuarios.get(rev["revisado_por"]) if rev["revisado_por"] else None)
            cambios.append({
                "id": ch["id"], "tipos": ch["tipos"], "fecha": ch["actual"]["fecha"], "fiv": ch["actual"]["fiv"],
                "antes": {k: ch["anterior"][k] for k in ("lote", "proveedor", "caducidad", "fecha")},
                "despues": {k: ch["actual"][k] for k in ("lote", "proveedor", "caducidad", "fecha")},
                "revision": rev,
            })
        ultima = validas[-1] if validas else None
        dias = None
        if ultima and fin_caducidad(ultima["caducidad"]):
            dias = (fin_caducidad(ultima["caducidad"]) - hoy).days
        productos.append({
            "codigo": p["codigo"], "nombre": p["nombre"], "proveedores": json.loads(p["proveedores"] or "[]"),
            "recepciones": lista, "cambios": cambios, "anomalias": detectar_anomalias(lista),
            "ultima": ultima, "dias_caducidad": dias,
        })
    return {"productos": productos, "version": VERSION}


# ------------------------------------------------------- historico
def cargar_un_historico(con, datos: dict, usuario_id, archivo: str) -> int:
    """Carga un histórico (producto + recepciones). Si el producto ya existe, no hace nada."""
    try:
        p = datos["producto"]
        if not CODIGO_RE.match(str(p["codigo"])) or not p.get("nombre"):
            raise KeyError
        recs = datos["recepciones"]
        for r in recs:
            if not FECHA_RE.match(str(r["fecha"])) or not r.get("lote") or not r.get("id"):
                raise KeyError
    except (KeyError, TypeError):
        raise ErrorUsuario(400, "El archivo no tiene el formato de histórico (producto y recepciones con id, fecha y lote).")
    if con.execute("SELECT 1 FROM productos WHERE codigo=?", (p["codigo"],)).fetchone():
        return 0
    n = 0
    if True:
        con.execute("INSERT INTO productos(codigo, nombre, proveedores, creado, creado_por) VALUES (?,?,?,?,?)",
                    (p["codigo"], p["nombre"], json.dumps(p.get("proveedores", []), ensure_ascii=False), ahora(), usuario_id))
        for r in datos["recepciones"]:
            con.execute(
                "INSERT INTO recepciones(id, producto, fecha, proveedor, cajas, lote, caducidad, fiv, firma, notas, dudosa, origen, confirmado) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["id"], p["codigo"], r["fecha"], r.get("proveedor", ""), r.get("cajas", ""), r["lote"], r.get("caducidad", ""),
                 r.get("fiv", ""), r.get("firma", ""), r.get("notas", ""), int(bool(r.get("dudosa"))), "historico", ahora()))
            n += 1
        for rid in datos.get("cambios_revisados", []):
            con.execute("INSERT OR IGNORE INTO revisiones(recepcion_id, revisado_por, fecha, nota) VALUES (?,?,?,?)",
                        (rid, usuario_id, ahora(), datos.get("nota_revision", "Carga inicial del histórico")))
        auditar(con, usuario_id, "cargar_historico", {"producto": p["codigo"], "archivo": archivo, "recepciones": len(datos["recepciones"])})
    return n


def cargar_historico(con, usuario_id=None, solo_si_vacio=False) -> int:
    """Carga los históricos de la carpeta historico/ del programa y de datos/historico/."""
    if solo_si_vacio and con.execute("SELECT COUNT(*) FROM productos").fetchone()[0]:
        return 0
    n = 0
    for carpeta in (HISTORICO, DATOS / "historico"):
        for f in sorted(carpeta.glob("*.json")) if carpeta.is_dir() else []:
            n += cargar_un_historico(con, json.loads(f.read_text(encoding="utf-8")), usuario_id, f.name)
    return n


# ------------------------------------------------- lectura de hojas
PROMPT_HOJA = """La imagen (o el PDF escaneado, que puede tener varias páginas) es una hoja manuscrita de registro de recepción de material fungible de un laboratorio de FIV. Puede ser de dos tipos:
(a) Plantilla nueva: cabecera con el código de producto impreso junto al QR (p. ej. "PROD 327-26") y una tabla con una fila por recepción: Fecha (DD/MM/AA), Proveedor (escrito a mano en una línea), Cajas, Nº lote (una cifra por casilla), Caducidad (DD/MM/AA), Nº FIV, Firma y casilla "Cambio".
(b) Hoja antigua: bloques verticales con "Data Recepció", "Nº Caixes", "Nº Lot", "Caducitat" y "NºFIV", en varias columnas que se leen de arriba abajo y luego de izquierda a derecha. Junto a la fecha puede haber el nombre del proveedor y una letra (la inicial de quien recibió).
{contexto}
Transcribe TODAS las recepciones de la hoja. Responde solo con JSON con esta forma:
{{"product_code":"327-26 o null","rows":[{{"date":"YYYY-MM-DD","supplier":"nombre tal como aparece, o vacío","boxes":"2","lot":"241218446","expiry":"YYYY-MM-DD o vacío","fiv":"225568","initial":"V","change_marked":false,"doubtful":["lot"],"note":"breve, en español"}}]}}
Reglas: los años de dos cifras son 20AA. Si la caducidad solo tiene mes y año, usa el último día del mes y añade "expiry" a doubtful. No inventes nada: si un campo no se lee con seguridad, déjalo vacío o pon tu mejor lectura y añade el campo a doubtful. Incluye también las entradas tachadas, con note "entrada tachada". El lote se copia exactamente, sin espacios."""


def leer_hoja_ia(imagen_b64: str, media_type: str, producto: dict | None, productos: list[dict]) -> dict:
    cfg = leer_config()
    clave = cfg.get("anthropic_api_key")
    if not clave:
        raise ErrorUsuario(501, "La lectura automática no está configurada. Añade la clave de la API de Anthropic (ver README).")
    if producto:
        contexto = (f"El producto es {producto['codigo']} ({producto['nombre']}). Proveedores admitidos: "
                    + ", ".join(f"{x['codigo']}={x['nombre']}" for x in producto["proveedores"]) + ".")
    else:
        contexto = "Productos conocidos: " + "; ".join(f"{p['codigo']} ({p['nombre']})" for p in productos)
    cuerpo = {
        "model": cfg.get("anthropic_model", "claude-sonnet-5-5"),
        "max_tokens": 8000,
        "messages": [{"role": "user", "content": [
            ({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": imagen_b64}}
             if media_type == "application/pdf" else
             {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": imagen_b64}}),
            {"type": "text", "text": PROMPT_HOJA.format(contexto=contexto)},
        ]}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=json.dumps(cuerpo).encode(),
        headers={"x-api-key": clave, "anthropic-version": "2023-06-01", "content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            res = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detalle = e.read().decode(errors="replace")[:300]
        raise ErrorUsuario(502, f"La API de Anthropic respondió con un error ({e.code}). {detalle}")
    except urllib.error.URLError as e:
        raise ErrorUsuario(502, f"No se pudo conectar con la API de Anthropic: {e.reason}")
    texto = "".join(b.get("text", "") for b in res.get("content", []) if b.get("type") == "text")
    ini, fin = texto.find("{"), texto.rfind("}")
    if ini < 0 or fin < 0:
        raise ErrorUsuario(502, "No se pudo interpretar la lectura. Vuelve a intentarlo o añade las filas a mano.")
    try:
        datos = json.loads(texto[ini:fin + 1])
    except ValueError:
        raise ErrorUsuario(502, "No se pudo interpretar la lectura. Vuelve a intentarlo o añade las filas a mano.")
    filas = []
    for r in datos.get("rows") or []:
        filas.append({
            "fecha": str(r.get("date") or ""), "proveedor": str(r.get("supplier") or ""), "cajas": str(r.get("boxes") or ""),
            "lote": re.sub(r"\s+", "", str(r.get("lot") or "")), "caducidad": str(r.get("expiry") or ""),
            "fiv": str(r.get("fiv") or ""), "firma": str(r.get("initial") or ""),
            "dudas": [str(x) for x in (r.get("doubtful") or [])], "nota": str(r.get("note") or ""), "origen": "escaneo",
        })
    codigo = str(datos.get("product_code") or "").replace("PROD", "").strip() or None
    return {"producto": codigo, "filas": filas}


def importar_pdf(pdf_b64: str, producto: dict | None = None, productos: list | None = None) -> dict:
    """PDF rellenado en el ordenador: lee sus campos. PDF escaneado: lo lee con la lectura automática."""
    try:
        return leer_campos_pdf(pdf_b64)
    except PdfSinCampos:
        if leer_config().get("anthropic_api_key"):
            return leer_hoja_ia(pdf_b64, "application/pdf", producto, productos or [])
        raise ErrorUsuario(501, "Este PDF es una hoja escaneada. Para leerlo hace falta activar la lectura automática "
                                "(ver README); mientras tanto, añade las filas a mano.")


class PdfSinCampos(Exception):
    pass


def leer_campos_pdf(pdf_b64: str) -> dict:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise PdfSinCampos()
    try:
        lector = PdfReader(io.BytesIO(base64.b64decode(pdf_b64)))
        campos = lector.get_fields() or {}
    except Exception:
        raise ErrorUsuario(400, "No se pudo abrir el PDF. Comprueba que es la hoja rellenable.")
    if not campos:
        raise PdfSinCampos()
    v = {k: (str(c.get("/V") or "").strip() if isinstance(c, dict) else "") for k, c in campos.items()}
    v = {k: ("" if x in ("/Off", "None") else x) for k, x in v.items()}
    codigo = v.get("producto") or None
    if not codigo:
        kw = (lector.metadata or {}).get("/Keywords", "") if lector.metadata else ""
        m = re.search(r"PROD\s+(\S+)", str(kw))
        codigo = m.group(1) if m else None

    def fecha(prefijo):
        d, m, a = v.get(f"{prefijo}_d", ""), v.get(f"{prefijo}_m", ""), v.get(f"{prefijo}_a", "")
        if not (d or m or a):
            return "", False
        try:
            return date(2000 + int(a), int(m), int(d)).isoformat(), False
        except ValueError:
            return "", True

    filas = []
    for n in range(1, 100):
        p = f"r{n:02d}"
        if f"{p}_lote" not in v:
            break
        f, mal_f = fecha(f"{p}_fecha")
        c, mal_c = fecha(f"{p}_caducidad")
        valores = [f, v.get(f"{p}_proveedor"), v.get(f"{p}_cajas"), v.get(f"{p}_lote"), c, v.get(f"{p}_fiv")]
        if not any(valores) and not (mal_f or mal_c):
            continue
        dudas = (["fecha"] if mal_f or not f else []) + (["caducidad"] if mal_c else [])
        filas.append({
            "fecha": f, "proveedor": v.get(f"{p}_proveedor", ""), "cajas": v.get(f"{p}_cajas", ""),
            "lote": re.sub(r"\s+", "", v.get(f"{p}_lote", "")).upper(), "caducidad": c, "fiv": v.get(f"{p}_fiv", ""),
            "firma": v.get(f"{p}_firma", ""), "dudas": dudas, "origen": "pdf",
            "nota": ("Marcada como cambio en la hoja. " if v.get(f"{p}_cambio") else "") + ("Fecha no válida en el PDF." if dudas else ""),
        })
    if not filas:
        # La hoja rellenable se imprimió, se escribió a mano y se escaneó: los campos están vacíos
        raise PdfSinCampos()
    return {"producto": codigo, "filas": filas, "hoja": v.get("hoja_num", "")}


# ------------------------------------------------------------ servidor
class ErrorUsuario(Exception):
    def __init__(self, estado: int, mensaje: str):
        super().__init__(mensaje)
        self.estado = estado
        self.mensaje = mensaje


def texto_limpio(x, maximo=200) -> str:
    return str(x if x is not None else "").strip()[:maximo]


def validar_recepcion(r: dict) -> dict:
    fecha = texto_limpio(r.get("fecha"), 10)
    if not FECHA_RE.match(fecha):
        raise ErrorUsuario(400, "Cada fila necesita una fecha de recepción válida.")
    lote = re.sub(r"\s+", "", texto_limpio(r.get("lote"), 40)).upper()
    if not lote:
        raise ErrorUsuario(400, "Cada fila necesita un nº de lote.")
    cad = texto_limpio(r.get("caducidad"), 10)
    if not CADUCIDAD_RE.match(cad):
        raise ErrorUsuario(400, f"La caducidad «{cad}» no es válida.")
    return {
        "fecha": fecha, "lote": lote, "caducidad": cad,
        "proveedor": texto_limpio(r.get("proveedor"), 80), "cajas": texto_limpio(r.get("cajas"), 40),
        "fiv": texto_limpio(r.get("fiv"), 20), "firma": texto_limpio(r.get("firma"), 10),
        "notas": texto_limpio(r.get("nota") or r.get("notas"), 400),
        "origen": r.get("origen") if r.get("origen") in ("manual", "escaneo", "pdf") else "manual",
    }


class Manejador(BaseHTTPRequestHandler):
    server_version = f"TrazabilidadFIV/{VERSION}"

    def log_message(self, fmt, *args):  # registro breve en consola
        sys.stderr.write(f"[{ahora()}] {self.address_string()} {fmt % args}\n")

    # ---- respuestas
    def enviar(self, estado: int, cuerpo: bytes, tipo: str, extra: dict | None = None):
        # Guardar en disco ANTES de responder: así la siguiente petición ya ve los cambios
        con = getattr(self, "_con", None)
        if con is not None and con.in_transaction:
            if 200 <= estado < 300:
                con.commit()
            else:
                con.rollback()
        self.send_response(estado)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(cuerpo)

    def json(self, estado: int, datos, extra=None):
        self.enviar(estado, json.dumps(datos, ensure_ascii=False).encode(), "application/json; charset=utf-8", extra)

    def csv(self, nombre: str, filas: list[list]):
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerows(filas)
        self.enviar(200, ("﻿" + buf.getvalue()).encode("utf-8"), "text/csv; charset=utf-8",
                    {"Content-Disposition": f'attachment; filename="{nombre}"'})

    def cuerpo(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_CUERPO:
            raise ErrorUsuario(413, "El archivo es demasiado grande (máximo 20 MB).")
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            raise ErrorUsuario(400, "Petición no válida.")

    # ---- sesión
    def usuario(self, con):
        c = cookies.SimpleCookie(self.headers.get("Cookie") or "")
        if COOKIE not in c:
            return None
        r = con.execute(
            "SELECT u.* FROM sesiones s JOIN usuarios u ON u.id=s.usuario_id WHERE s.token=? AND s.caduca>? AND u.activo=1",
            (c[COOKIE].value, time.time())).fetchone()
        return dict(r) if r else None

    def requiere(self, con, admin=False):
        u = self.usuario(con)
        if not u:
            raise ErrorUsuario(401, "Inicia sesión para continuar.")
        if admin and not u["admin"]:
            raise ErrorUsuario(403, "Solo un administrador puede hacer esto.")
        return u

    def cookie_sesion(self, token: str, segundos: int) -> dict:
        return {"Set-Cookie": f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={segundos}"}

    # ---- enrutado
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self.atender("GET")

    def do_POST(self):
        self.atender("POST")

    def do_DELETE(self):
        self.atender("DELETE")

    def atender(self, metodo: str):
        ruta = urlparse(self.path).path
        try:
            if not ruta.startswith("/api/"):
                return self.estatico(ruta)
            con = conectar()
            self._con = con
            try:
                with con:
                    return self.api(con, metodo, ruta)
            finally:
                self._con = None
                con.close()
        except ErrorUsuario as e:
            self._con = None
            self.json(e.estado, {"error": e.mensaje})
        except sqlite3.IntegrityError:
            self.json(409, {"error": "Ese registro ya existe o hace referencia a un producto que no existe."})
        except Exception as e:  # pragma: no cover
            self.log_message("ERROR %r", e)
            self.json(500, {"error": "Error interno del servidor. Revisa la consola donde se ejecuta el programa."})

    def estatico(self, ruta: str):
        if ruta in ("/", ""):
            ruta = "/index.html"
        destino = (STATIC / ruta.lstrip("/")).resolve()
        if STATIC not in destino.parents or not destino.is_file():
            return self.enviar(404, b"No encontrado", "text/plain; charset=utf-8")
        tipo = mimetypes.guess_type(destino.name)[0] or "application/octet-stream"
        if tipo.startswith("text/") or tipo in ("application/javascript",):
            tipo += "; charset=utf-8"
        self.enviar(200, destino.read_bytes(), tipo)

    def api(self, con, metodo: str, ruta: str):
        partes = ruta.strip("/").split("/")[1:]  # sin «api»

        # -------- sesión y usuarios
        if partes == ["inicio"] and metodo == "GET":
            hay = con.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
            u = self.usuario(con)
            return self.json(200, {
                "hay_usuarios": bool(hay), "version": VERSION,
                "usuario": {"id": u["id"], "nombre": u["nombre"], "usuario": u["usuario"], "admin": bool(u["admin"])} if u else None,
                "lectura_ia": bool(leer_config().get("anthropic_api_key")), "importar_pdf": pypdf_disponible(),
                "generar_hojas": reportlab_disponible(),
            })

        if partes == ["primer-usuario"] and metodo == "POST":
            if con.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]:
                raise ErrorUsuario(403, "Ya hay usuarios. Pide a un administrador que te dé de alta.")
            b = self.cuerpo()
            uid = self.crear_usuario(con, b, admin=True, creador=None)
            cargar_historico(con, uid, solo_si_vacio=True)
            return self.abrir_sesion(con, uid)

        if partes == ["entrar"] and metodo == "POST":
            b = self.cuerpo()
            r = con.execute("SELECT * FROM usuarios WHERE usuario=? AND activo=1", (texto_limpio(b.get("usuario"), 40).lower(),)).fetchone()
            if not r or not hmac.compare_digest(hash_clave(str(b.get("clave") or ""), r["sal"])[0], r["hash"]):
                time.sleep(0.8)
                raise ErrorUsuario(401, "Usuario o contraseña incorrectos.")
            return self.abrir_sesion(con, r["id"])

        if partes == ["salir"] and metodo == "POST":
            c = cookies.SimpleCookie(self.headers.get("Cookie") or "")
            if COOKIE in c:
                con.execute("DELETE FROM sesiones WHERE token=?", (c[COOKIE].value,))
            return self.json(200, {"ok": True}, self.cookie_sesion("", 0))

        u = self.requiere(con)

        if partes == ["usuarios"] and metodo == "GET":
            self.requiere(con, admin=True)
            filas = [{"id": r["id"], "usuario": r["usuario"], "nombre": r["nombre"], "admin": bool(r["admin"]), "activo": bool(r["activo"]), "creado": r["creado"]}
                     for r in con.execute("SELECT * FROM usuarios ORDER BY nombre")]
            return self.json(200, {"usuarios": filas})

        if partes == ["usuarios"] and metodo == "POST":
            self.requiere(con, admin=True)
            b = self.cuerpo()
            uid = self.crear_usuario(con, b, admin=bool(b.get("admin")), creador=u["id"])
            return self.json(200, {"id": uid})

        if len(partes) == 3 and partes[0] == "usuarios" and partes[2] in ("desactivar", "activar", "clave") and metodo == "POST":
            self.requiere(con, admin=True)
            uid = int(partes[1]) if partes[1].isdigit() else -1
            if not con.execute("SELECT 1 FROM usuarios WHERE id=?", (uid,)).fetchone():
                raise ErrorUsuario(404, "Ese usuario no existe.")
            if partes[2] == "clave":
                clave = str(self.cuerpo().get("clave") or "")
                if len(clave) < 8:
                    raise ErrorUsuario(400, "La contraseña debe tener al menos 8 caracteres.")
                h, s = hash_clave(clave)
                con.execute("UPDATE usuarios SET hash=?, sal=? WHERE id=?", (h, s, uid))
                con.execute("DELETE FROM sesiones WHERE usuario_id=?", (uid,))
            else:
                if uid == u["id"]:
                    raise ErrorUsuario(400, "No puedes desactivar tu propio usuario.")
                con.execute("UPDATE usuarios SET activo=? WHERE id=?", (int(partes[2] == "activar"), uid))
                con.execute("DELETE FROM sesiones WHERE usuario_id=?", (uid,))
            auditar(con, u["id"], f"usuario_{partes[2]}", {"usuario_id": uid})
            return self.json(200, {"ok": True})

        # -------- datos
        if partes == ["estado"] and metodo == "GET":
            return self.json(200, estado(con))

        if partes == ["productos"] and metodo == "POST":
            b = self.cuerpo()
            codigo = texto_limpio(b.get("codigo"), 40)
            nombre = texto_limpio(b.get("nombre"), 160)
            if not CODIGO_RE.match(codigo):
                raise ErrorUsuario(400, "El código solo puede llevar letras, números, puntos y guiones.")
            if not nombre:
                raise ErrorUsuario(400, "Escribe la descripción del producto.")
            provs = [{"codigo": texto_limpio(x.get("codigo"), 20), "nombre": texto_limpio(x.get("nombre"), 120)}
                     for x in (b.get("proveedores") or []) if isinstance(x, dict) and x.get("nombre")]
            con.execute("INSERT INTO productos(codigo, nombre, proveedores, creado, creado_por) VALUES (?,?,?,?,?)",
                        (codigo, nombre, json.dumps(provs, ensure_ascii=False), ahora(), u["id"]))
            auditar(con, u["id"], "crear_producto", {"codigo": codigo, "nombre": nombre, "proveedores": provs})
            return self.json(200, {"ok": True})

        if partes == ["recepciones"] and metodo == "POST":
            b = self.cuerpo()
            codigo = texto_limpio(b.get("producto"), 40)
            if not con.execute("SELECT 1 FROM productos WHERE codigo=?", (codigo,)).fetchone():
                raise ErrorUsuario(400, "Elige un producto existente.")
            filas = [validar_recepcion(r) for r in (b.get("filas") or [])]
            if not filas:
                raise ErrorUsuario(400, "No hay filas que guardar.")
            ids = []
            for r in filas:
                rid = secrets.token_hex(8)
                con.execute(
                    "INSERT INTO recepciones(id, producto, fecha, proveedor, cajas, lote, caducidad, fiv, firma, notas, origen, confirmado_por, confirmado) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, codigo, r["fecha"], r["proveedor"], r["cajas"], r["lote"], r["caducidad"], r["fiv"], r["firma"], r["notas"], r["origen"], u["id"], ahora()))
                ids.append(rid)
            auditar(con, u["id"], "crear_recepciones", {"producto": codigo, "ids": ids, "filas": filas, "hoja": texto_limpio(b.get("hoja"), 20)})
            return self.json(200, {"ids": ids})

        if len(partes) == 3 and partes[0] == "recepciones" and partes[2] == "anular" and metodo == "POST":
            motivo = texto_limpio(self.cuerpo().get("motivo"), 300)
            if not motivo:
                raise ErrorUsuario(400, "Indica el motivo de la anulación.")
            r = con.execute("SELECT * FROM recepciones WHERE id=? AND anulada=0", (partes[1],)).fetchone()
            if not r:
                raise ErrorUsuario(404, "Esa recepción no existe o ya está anulada.")
            con.execute("UPDATE recepciones SET anulada=1, anulada_por=?, anulada_fecha=?, motivo_anulacion=? WHERE id=?",
                        (u["id"], ahora(), motivo, partes[1]))
            auditar(con, u["id"], "anular_recepcion", {"id": partes[1], "motivo": motivo, "datos": dict(r)})
            return self.json(200, {"ok": True})

        if len(partes) == 2 and partes[0] == "revisiones" and metodo == "POST":
            if not con.execute("SELECT 1 FROM recepciones WHERE id=?", (partes[1],)).fetchone():
                raise ErrorUsuario(404, "Ese cambio no existe.")
            nota = texto_limpio(self.cuerpo().get("nota"), 300)
            con.execute("INSERT OR REPLACE INTO revisiones(recepcion_id, revisado_por, fecha, nota) VALUES (?,?,?,?)",
                        (partes[1], u["id"], ahora(), nota))
            auditar(con, u["id"], "revisar_cambio", {"recepcion_id": partes[1], "nota": nota})
            return self.json(200, {"ok": True})

        if len(partes) == 2 and partes[0] == "revisiones" and metodo == "DELETE":
            con.execute("DELETE FROM revisiones WHERE recepcion_id=?", (partes[1],))
            auditar(con, u["id"], "deshacer_revision", {"recepcion_id": partes[1]})
            return self.json(200, {"ok": True})

        if partes == ["leer-hoja"] and metodo == "POST":
            b = self.cuerpo()
            mt = b.get("tipo") if b.get("tipo") in ("image/jpeg", "image/png", "image/webp", "image/gif") else None
            if not mt or not b.get("imagen"):
                raise ErrorUsuario(400, "Sube una imagen JPG, PNG o WebP.")
            est = estado(con)["productos"]
            prod = next((p for p in est if p["codigo"] == b.get("producto")), None)
            res = leer_hoja_ia(b["imagen"], mt, prod, est)
            auditar(con, u["id"], "leer_hoja", {"producto": b.get("producto"), "filas": len(res["filas"])})
            return self.json(200, res)

        if partes == ["historico"] and metodo == "POST":
            self.requiere(con, admin=True)
            b = self.cuerpo()
            datos = b.get("datos")
            if not isinstance(datos, dict):
                raise ErrorUsuario(400, "Sube el archivo .json del histórico.")
            codigo = str((datos.get("producto") or {}).get("codigo", ""))
            if codigo and con.execute("SELECT 1 FROM productos WHERE codigo=?", (codigo,)).fetchone():
                raise ErrorUsuario(409, f"El producto {codigo} ya existe: su histórico no se ha vuelto a cargar.")
            n = cargar_un_historico(con, datos, u["id"], texto_limpio(b.get("archivo"), 120))
            return self.json(200, {"producto": codigo, "recepciones": n})

        if len(partes) == 2 and partes[0] == "hoja" and partes[1].endswith(".pdf") and metodo == "GET":
            if not reportlab_disponible():
                raise ErrorUsuario(501, "Para generar hojas hay que instalar el paquete reportlab.")
            codigo = partes[1][:-4]
            p = con.execute("SELECT * FROM productos WHERE codigo=?", (codigo,)).fetchone()
            if not p:
                raise ErrorUsuario(404, "Ese producto no existe.")
            pdf = generar_hoja_pdf({"codigo": p["codigo"], "nombre": p["nombre"], "proveedores": json.loads(p["proveedores"] or "[]")})
            return self.enviar(200, pdf, "application/pdf",
                               {"Content-Disposition": f'attachment; filename="Hoja_recepcion_{codigo}.pdf"'})

        if partes == ["importar-pdf"] and metodo == "POST":
            b = self.cuerpo()
            if not b.get("pdf"):
                raise ErrorUsuario(400, "Sube el PDF de la hoja.")
            est = estado(con)["productos"]
            prod = next((p for p in est if p["codigo"] == b.get("producto")), None)
            res = importar_pdf(b["pdf"], prod, est)
            auditar(con, u["id"], "leer_pdf", {"producto": b.get("producto"), "filas": len(res["filas"]), "origen": res["filas"][0]["origen"] if res["filas"] else ""})
            return self.json(200, res)

        if len(partes) == 2 and partes[0] == "exportar" and metodo == "GET":
            return self.exportar(con, partes[1], u)

        raise ErrorUsuario(404, "Ruta no encontrada.")

    # ---- ayudantes de la API
    def crear_usuario(self, con, b: dict, admin: bool, creador) -> int:
        usuario = texto_limpio(b.get("usuario"), 40).lower()
        nombre = texto_limpio(b.get("nombre"), 80)
        clave = str(b.get("clave") or "")
        if not re.match(r"^[a-z0-9._-]{3,40}$", usuario):
            raise ErrorUsuario(400, "El usuario debe tener entre 3 y 40 letras minúsculas, números, puntos o guiones.")
        if not nombre:
            raise ErrorUsuario(400, "Escribe el nombre completo.")
        if len(clave) < 8:
            raise ErrorUsuario(400, "La contraseña debe tener al menos 8 caracteres.")
        if con.execute("SELECT 1 FROM usuarios WHERE usuario=?", (usuario,)).fetchone():
            raise ErrorUsuario(409, "Ese nombre de usuario ya existe.")
        h, s = hash_clave(clave)
        cur = con.execute("INSERT INTO usuarios(usuario, nombre, hash, sal, admin, creado) VALUES (?,?,?,?,?,?)",
                          (usuario, nombre, h, s, int(admin), ahora()))
        auditar(con, creador, "crear_usuario", {"usuario": usuario, "nombre": nombre, "admin": admin})
        return cur.lastrowid

    def abrir_sesion(self, con, uid: int):
        token = secrets.token_urlsafe(32)
        con.execute("DELETE FROM sesiones WHERE caduca<?", (time.time(),))
        con.execute("INSERT INTO sesiones(token, usuario_id, caduca) VALUES (?,?,?)", (token, uid, time.time() + SESION_HORAS * 3600))
        auditar(con, uid, "entrar", {})
        return self.json(200, {"ok": True}, self.cookie_sesion(token, SESION_HORAS * 3600))

    def exportar(self, con, que: str, u: dict):
        hoy = date.today().isoformat()
        if que == "cambios.csv":
            filas = [["Fecha", "Producto", "Descripción", "Tipo de cambio", "Lote anterior", "Lote nuevo", "Proveedor anterior",
                      "Proveedor nuevo", "Caducidad anterior", "Caducidad nueva", "Nº FIV desde", "Revisado", "Revisado por", "Fecha revisión", "Nota"]]
            for p in estado(con)["productos"]:
                for c in p["cambios"]:
                    r = c["revision"] or {}
                    filas.append([c["fecha"], p["codigo"], p["nombre"], " y ".join(c["tipos"]), c["antes"]["lote"], c["despues"]["lote"],
                                  c["antes"]["proveedor"], c["despues"]["proveedor"], c["antes"]["caducidad"], c["despues"]["caducidad"],
                                  c["fiv"], "Sí" if r else "No", r.get("revisado_por_nombre") or "", r.get("fecha", ""), r.get("nota", "")])
            return self.csv(f"cambios_lote_{hoy}.csv", filas)
        if que == "recepciones.csv":
            filas = [["Producto", "Fecha", "Proveedor", "Cajas", "Lote", "Caducidad", "Nº FIV", "Firma", "Notas", "Origen", "Confirmado por",
                      "Confirmado", "Anulada", "Motivo anulación"]]
            for p in estado(con)["productos"]:
                for r in p["recepciones"]:
                    filas.append([p["codigo"], r["fecha"], r["proveedor"], r["cajas"], r["lote"], r["caducidad"], r["fiv"], r["firma"], r["notas"],
                                  r["origen"], r["confirmado_por_nombre"] or "", r["confirmado"] or "", "Sí" if r["anulada"] else "No", r["motivo_anulacion"]])
            return self.csv(f"recepciones_{hoy}.csv", filas)
        if que == "auditoria.csv":
            self.requiere(con, admin=True)
            nombres = {r["id"]: r["nombre"] for r in con.execute("SELECT id, nombre FROM usuarios")}
            filas = [["Fecha", "Usuario", "Acción", "Detalle"]]
            for r in con.execute("SELECT * FROM auditoria ORDER BY id"):
                filas.append([r["fecha"], nombres.get(r["usuario_id"], "Sistema"), r["accion"], r["detalle"]])
            return self.csv(f"auditoria_{hoy}.csv", filas)
        raise ErrorUsuario(404, "Exportación no encontrada.")


# --------------------------------------------------------------- consola
def cmd_servidor(args):
    iniciar_bd()
    with conectar() as con:
        if con.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]:
            n = cargar_historico(con, None, solo_si_vacio=True)
            if n:
                print(f"Cargadas {n} recepciones del histórico.")
    srv = ThreadingHTTPServer((args.host, args.puerto), Manejador)
    print(f"Trazabilidad FIV {VERSION} en marcha.")
    print(f"  En este equipo:     http://localhost:{args.puerto}")
    print(f"  Desde otros PCs:    http://<nombre-o-IP-de-este-PC>:{args.puerto}")
    print(f"  Datos guardados en: {DB_PATH}")
    print("Pulsa Ctrl+C o cierra esta ventana para detenerlo.")
    if EMPAQUETADO and not os.environ.get("TRAZ_SIN_NAVEGADOR"):
        import threading
        import webbrowser
        threading.Timer(1.5, webbrowser.open, (f"http://localhost:{args.puerto}",)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nDetenido.")


def cmd_crear_usuario(args):
    iniciar_bd()
    usuario = input("Usuario (minúsculas, sin espacios): ").strip().lower()
    nombre = input("Nombre completo: ").strip()
    clave = getpass.getpass("Contraseña (mínimo 8 caracteres): ")
    if getpass.getpass("Repite la contraseña: ") != clave:
        sys.exit("Las contraseñas no coinciden.")
    with conectar() as con:
        try:
            Manejador.crear_usuario(Manejador.__new__(Manejador), con, {"usuario": usuario, "nombre": nombre, "clave": clave}, args.admin, None)
        except ErrorUsuario as e:
            sys.exit(e.mensaje)
    print(f"Usuario «{usuario}» creado{' como administrador' if args.admin else ''}.")


def cmd_historico(args):
    iniciar_bd()
    with conectar() as con:
        n = cargar_historico(con, None)
    print(f"Cargadas {n} recepciones nuevas del histórico.")


def cmd_copia(args):
    iniciar_bd()
    destino_dir = Path(args.destino).resolve() if args.destino else DATOS / "copias"
    destino_dir.mkdir(parents=True, exist_ok=True)
    destino = destino_dir / f"trazabilidad-{datetime.now():%Y%m%d-%H%M%S}.db"
    with conectar() as con, sqlite3.connect(destino) as dst:
        con.backup(dst)
    print(f"Copia guardada en {destino}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Trazabilidad de material fungible — Laboratorio de FIV")
    sub = ap.add_subparsers(dest="orden")
    s = sub.add_parser("servidor", help="arranca el servidor (por defecto)")
    for p in (ap, s):
        p.add_argument("--host", default=os.environ.get("TRAZ_HOST", "0.0.0.0"))
        p.add_argument("--puerto", type=int, default=int(os.environ.get("TRAZ_PUERTO", "8000")))
    c = sub.add_parser("crear-usuario", help="crea un usuario desde la consola")
    c.add_argument("--admin", action="store_true", help="con permisos de administrador")
    sub.add_parser("cargar-historico", help="carga los archivos de la carpeta historico/")
    k = sub.add_parser("copia", help="copia de seguridad de la base de datos")
    k.add_argument("--destino", help="carpeta donde guardar la copia (por defecto datos/copias)")
    args = ap.parse_args(argv)
    {"crear-usuario": cmd_crear_usuario, "cargar-historico": cmd_historico, "copia": cmd_copia}.get(args.orden, cmd_servidor)(args)


if __name__ == "__main__":
    main()
