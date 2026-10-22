#!/usr/bin/env python3
"""
Genera la hoja de recepción de un producto como PDF rellenable (A4 vertical).

Ejemplos:
  python herramientas/generar_hoja.py --codigo 327-26 --nombre "Rack puntas largas 1–200 µl · 91 mm" \
      --proveedores "Concile TF-200-HS (Embriotest); Neptune; Nordic Cell 33012 (180 µl)"
  python herramientas/generar_hoja.py --todos        # una hoja por cada producto de la base de datos

Necesita el paquete reportlab (pip install reportlab).
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor, white, black
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.graphics import renderPDF

RAIZ = Path(__file__).resolve().parent.parent
FUENTES = RAIZ / "herramientas" / "fuentes"
LOGO = str(RAIZ / "static" / "logo-crea.jpg")

for name in ["Montserrat-Bold", "OpenSans-Regular", "OpenSans-Bold"]:
    pdfmetrics.registerFont(TTFont(name, str(FUENTES / f"{name}.ttf")))

AZUL = HexColor("#033953")
NARANJA = HexColor("#f7b086")
NARANJA_SUAVE = HexColor("#fdebe0")
GRIS_LINEA = HexColor("#b7c6d0")
GRIS_FILA = HexColor("#f2f5f7")
SUB = HexColor("#cfdde6")
NOTA = HexColor("#3d5a6b")
TINTA = HexColor("#2a56c6")

def generar(CODIGO, NOMBRE, PROVEEDORES, OUT):
    W, H = A4
    M = 30  # margen
    c = canvas.Canvas(OUT, pagesize=A4)
    c.setTitle(f"Registro de recepción · {CODIGO}")
    c.setAuthor("CREA · Laboratorio de FIV")
    c.setSubject("Formulario 7PC-75-15 v2")
    c.setKeywords(f"PROD {CODIGO}")
    form = c.acroForm


    def Y(top):
        """Convierte una distancia desde el borde superior en coordenada PDF."""
        return H - top


    def text(x, top, s, font="OpenSans-Regular", size=9, color=AZUL, anchor="left", spacing=0):
        c.setFillColor(color)
        if spacing:
            t = c.beginText()
            t.setFont(font, size)
            t.setCharSpace(spacing)
            w = pdfmetrics.stringWidth(s, font, size) + spacing * (len(s) - 1)
            x0 = x - w if anchor == "right" else x
            t.setTextOrigin(x0, Y(top))
            t.textOut(s)
            t.setCharSpace(0)
            c.drawText(t)
            return
        c.setFont(font, size)
        if anchor == "right":
            c.drawRightString(x, Y(top), s)
        elif anchor == "center":
            c.drawCentredString(x, Y(top), s)
        else:
            c.drawString(x, Y(top), s)


    def field(name, x, top, w, h, maxlen=0, comb=False, size=11, tooltip=""):
        flags = "comb doNotScroll" if comb else "doNotScroll"
        form.textfield(name=name, tooltip=tooltip or name, x=x, y=Y(top) - h, width=w, height=h,
                       maxlen=maxlen or None, fieldFlags=flags, borderWidth=0,
                       fillColor=None, borderColor=None, textColor=TINTA,
                       fontName="Helvetica", fontSize=size, forceBorder=False)


    CELL_W, CELL_H, CELL_GAP, SEP = 11.25, 18, 0.75, 3.75


    def cells(x, top, n, name, tooltip):
        """Dibuja n casillas y pone encima un campo de peine con una cifra por casilla."""
        c.setStrokeColor(AZUL)
        c.setLineWidth(0.75)
        c.setFillColor(white)
        for i in range(n):
            c.rect(x + i * (CELL_W + CELL_GAP), Y(top) - CELL_H, CELL_W, CELL_H, stroke=1, fill=1)
        w = n * CELL_W + (n - 1) * CELL_GAP
        field(name, x, top, w, CELL_H, maxlen=n, comb=True, tooltip=tooltip)
        return w


    def date_cells(x, top, prefix, tooltip):
        parts = [("d", "día"), ("m", "mes"), ("a", "año (2 cifras)")]
        cx = x
        for i, (k, label) in enumerate(parts):
            cx += cells(cx, top, 2, f"{prefix}_{k}", f"{tooltip}: {label}")
            if i < 2:
                text(cx + CELL_GAP + SEP / 2, top + 13, "/", size=9, anchor="center")
                cx += CELL_GAP + SEP + CELL_GAP


    # ---------- marco y marcas de escaneo ----------
    c.setFillColor(NARANJA)
    c.rect(0, H - 6, W, 6, stroke=0, fill=1)
    c.setFillColor(black)
    for fx, fy in [(12, H - 12 - 10.5), (W - 12 - 10.5, H - 12 - 10.5), (12, 12), (W - 12 - 10.5, 12)]:
        c.rect(fx, fy, 10.5, 10.5, stroke=0, fill=1)

    # ---------- cabecera ----------
    c.drawImage(LOGO, M, Y(32) - 34, width=34 * 449 / 155, height=34, mask="auto")
    text(W - M, 44, "LABORATORIO DE FIV", "Montserrat-Bold", 9, spacing=1.2, anchor="right")
    text(W - M, 58, "Registro de recepción de material fungible", size=9.75, anchor="right")
    c.setStrokeColor(NARANJA)
    c.setLineWidth(1.5)
    c.line(M, Y(73), W - M, Y(73))

    # producto
    ancho_titulo = W - 2 * M - 75 - 90
    tam = 16.5
    while tam > 10 and pdfmetrics.stringWidth(NOMBRE, "Montserrat-Bold", tam) > ancho_titulo:
        tam -= 0.5
    text(M, 100, NOMBRE, "Montserrat-Bold", tam)
    c.setStrokeColor(AZUL)
    c.setFillColor(NARANJA_SUAVE)
    c.setLineWidth(1.5)
    c.roundRect(M, Y(146), 84, 36, 9, stroke=1, fill=1)
    text(M + 10, 123, "Código producto", size=9)
    c.setFillColor(AZUL)
    tc = 16
    while tc > 9 and pdfmetrics.stringWidth(CODIGO, "Courier-Bold", tc) > 64:
        tc -= 1
    c.setFont("Courier-Bold", tc)
    c.drawString(M + 10, Y(140), CODIGO)
    rx = M + 84 + 12
    text(rx, 120, "Proveedores admitidos", "Montserrat-Bold", 9)
    lineas, actual = [], ""
    for prov in PROVEEDORES or ["(sin definir)"]:
        prueba = (actual + " · " + prov) if actual else prov
        if actual and pdfmetrics.stringWidth(prueba, "OpenSans-Regular", 9) > ancho_titulo - 96:
            lineas.append(actual)
            actual = prov
        else:
            actual = prueba
    lineas.append(actual)
    for i, linea in enumerate(lineas[:2]):
        text(rx, 132 + 12 * i, linea, size=9)
    text(M, 163, "Si llega otra referencia, anótala en Proveedor y avisa a Calidad.", size=9, color=NOTA)

    # QR del producto
    qx, qtop, qs = W - M - 75, 82, 75
    c.setStrokeColor(NARANJA)
    c.setLineWidth(0.75)
    c.roundRect(qx, Y(qtop + qs + 12), qs, qs + 12, 7.5, stroke=1, fill=0)
    qr = QrCodeWidget(f"TRZ1|PROD|{CODIGO}", barLevel="M")
    b = qr.getBounds()
    d = Drawing(60, 60, transform=[60 / (b[2] - b[0]), 0, 0, 60 / (b[3] - b[1]), 0, 0])
    d.add(qr)
    renderPDF.draw(d, c, qx + (qs - 60) / 2, Y(qtop + 66))
    c.setFillColor(black)
    c.setFont("Courier", 9)
    c.drawCentredString(qx + qs / 2, Y(qtop + qs + 6), f"PROD {CODIGO}")
    form.textfield(name="producto", value=CODIGO, x=qx, y=Y(qtop + qs + 12), width=1, height=1, fieldFlags="readOnly", borderWidth=0, fillColor=None, borderColor=None, textColor=white, fontSize=1, tooltip="Código de producto")

    # hoja nº
    hx = qx - 12
    text(hx, 96, "Hoja nº", "OpenSans-Bold", 9, anchor="right")
    hw = 3 * CELL_W + 2 * CELL_GAP
    cells(hx - hw, 102, 3, "hoja_num", "Número de hoja")
    text(hx, 135, "Continúa de", size=9, anchor="right")
    text(hx - 30, 149, "hoja nº", size=9, anchor="right")
    c.setStrokeColor(AZUL)
    c.setLineWidth(0.75)
    c.line(hx - 27, Y(151), hx, Y(151))
    field("hoja_anterior", hx - 27, 138, 27, 14, maxlen=3, size=9, tooltip="Continúa de la hoja nº")

    # ---------- tabla ----------
    COLS = [81, 67.5, 27, 120, 81, 72, 28.5, 34.5]
    GAP = 2.25
    TX, TW = M, W - 2 * M
    PAD = (TW - sum(COLS) - GAP * (len(COLS) - 1)) / 2
    xs = []
    x = TX + PAD
    for w in COLS:
        xs.append(x)
        x += w + GAP

    T_TOP, HEAD_H, ROW_H, NROWS = 176, 31.5, 28.5, 18
    t_bottom = T_TOP + HEAD_H + ROW_H * NROWS

    clip = c.beginPath()
    clip.roundRect(TX, Y(t_bottom), TW, t_bottom - T_TOP, 7.5)
    c.saveState()
    c.clipPath(clip, stroke=0, fill=0)
    c.setFillColor(AZUL)
    c.rect(TX, Y(T_TOP + HEAD_H), TW, HEAD_H, stroke=0, fill=1)
    for r in range(NROWS):
        top = T_TOP + HEAD_H + r * ROW_H
        c.setFillColor(GRIS_FILA if r % 2 else white)
        c.rect(TX, Y(top + ROW_H), TW, ROW_H, stroke=0, fill=1)
        if r:
            c.setStrokeColor(GRIS_LINEA)
            c.setLineWidth(0.75)
            c.line(TX, Y(top), TX + TW, Y(top))
    c.restoreState()
    c.setStrokeColor(AZUL)
    c.setLineWidth(1.1)
    c.roundRect(TX, Y(t_bottom), TW, t_bottom - T_TOP, 7.5, stroke=1, fill=0)

    heads = [("Fecha", "DD/MM/AA"), ("Proveedor", ""), ("Cajas", ""), ("Nº lote", "desde la izquierda"),
             ("Caducidad", "DD/MM/AA"), ("Nº FIV", ""), ("Firma", ""), ("Cambio", "")]
    for (h, sub), hx_ in zip(heads, xs):
        if sub:
            text(hx_, T_TOP + 14, h, "OpenSans-Bold", 9, color=white)
            text(hx_, T_TOP + 25, sub, size=9, color=SUB)
        else:
            text(hx_, T_TOP + 25, h, "OpenSans-Bold", 9, color=white)

    for r in range(NROWS):
        n = f"r{r + 1:02d}"
        top = T_TOP + HEAD_H + r * ROW_H
        ctop = top + (ROW_H - CELL_H) / 2
        date_cells(xs[0], ctop, f"{n}_fecha", f"Fila {r + 1} · Fecha de recepción")
        # proveedor: línea para escribir
        c.setStrokeColor(AZUL)
        c.setLineWidth(0.75)
        c.line(xs[1], Y(ctop + CELL_H), xs[1] + COLS[1], Y(ctop + CELL_H))
        field(f"{n}_proveedor", xs[1], ctop + 1, COLS[1], CELL_H - 2, size=10, tooltip=f"Fila {r + 1} · Proveedor")
        cells(xs[2], ctop, 2, f"{n}_cajas", f"Fila {r + 1} · Nº de cajas")
        cells(xs[3], ctop, 10, f"{n}_lote", f"Fila {r + 1} · Nº de lote")
        date_cells(xs[4], ctop, f"{n}_caducidad", f"Fila {r + 1} · Caducidad")
        cells(xs[5], ctop, 6, f"{n}_fiv", f"Fila {r + 1} · Nº FIV")
        c.setStrokeColor(AZUL)
        c.line(xs[6], Y(ctop + CELL_H), xs[6] + COLS[6] - 3, Y(ctop + CELL_H))
        field(f"{n}_firma", xs[6], ctop + 1, COLS[6] - 3, CELL_H - 2, maxlen=4, size=10, tooltip=f"Fila {r + 1} · Firma (iniciales)")
        bs = 13.5
        form.checkbox(name=f"{n}_cambio", tooltip=f"Fila {r + 1} · Cambio de lote o proveedor",
                      x=xs[7] + (COLS[7] - bs) / 2, y=Y(top + (ROW_H + bs) / 2), size=bs,
                      buttonStyle="cross", borderColor=AZUL, fillColor=white, textColor=TINTA,
                      borderWidth=1.5, forceBorder=True)

    # ---------- pie ----------
    ft = t_bottom + 18
    text(M, ft, "Cómo rellenar", "Montserrat-Bold", 9)
    lines = [
        "Una cifra o letra por casilla, en mayúsculas. En papel, con bolígrafo azul o negro; en pantalla, escribe y pasa de campo con Tab.",
        "Si te equivocas en papel, tacha la fila con una sola línea y escríbela de nuevo en la siguiente fila libre. No sobrescribas.",
        "Marca «Cambio» si el lote o el proveedor no coinciden con la fila anterior, y avisa a Calidad.",
    ]
    for i, s in enumerate(lines):
        text(M, ft + 13 + i * 12, s, size=9)
    lt = ft + 13 + 3 * 12 + 4
    c.setStrokeColor(NARANJA)
    c.setLineWidth(1.5)
    c.line(M, Y(lt), W - M, Y(lt))
    bt = lt + 18
    text(M, bt, "Revisado (Calidad):", size=9)
    rw = pdfmetrics.stringWidth("Revisado (Calidad):", "OpenSans-Regular", 9) + 4
    c.setStrokeColor(AZUL)
    c.setLineWidth(0.75)
    c.line(M + rw, Y(bt + 2), M + rw + 150, Y(bt + 2))
    field("revisado_por", M + rw, bt - 11, 150, 14, size=9, tooltip="Revisado por (Calidad)")
    fx = M + rw + 168
    text(fx, bt, "Fecha:", size=9)
    fw = pdfmetrics.stringWidth("Fecha:", "OpenSans-Regular", 9) + 4
    c.line(fx + fw, Y(bt + 2), fx + fw + 70, Y(bt + 2))
    field("revisado_fecha", fx + fw, bt - 11, 70, 14, maxlen=10, size=9, tooltip="Fecha de revisión (DD/MM/AAAA)")
    c.setFillColor(AZUL)
    c.setFont("Courier", 9)
    c.drawRightString(W - M, Y(bt), "Form. 7PC-75-15 · v2")

    c.showPage()
    c.save()
    return OUT


def main():
    ap = argparse.ArgumentParser(description="Genera hojas de recepción rellenables")
    ap.add_argument("--codigo")
    ap.add_argument("--nombre")
    ap.add_argument("--proveedores", default="", help="separados por punto y coma")
    ap.add_argument("--todos", action="store_true", help="una hoja por cada producto de la base de datos")
    ap.add_argument("--carpeta", default=str(RAIZ / "plantillas"), help="carpeta de salida")
    a = ap.parse_args()
    out = Path(a.carpeta)
    out.mkdir(parents=True, exist_ok=True)
    trabajos = []
    if a.todos:
        db = RAIZ / "datos" / "trazabilidad.db"
        if not db.exists():
            sys.exit("No hay base de datos todavía. Arranca el programa y crea un producto primero.")
        with sqlite3.connect(db) as con:
            for codigo, nombre, provs in con.execute("SELECT codigo, nombre, proveedores FROM productos ORDER BY codigo"):
                trabajos.append((codigo, nombre, [p["nombre"] for p in json.loads(provs or "[]")]))
    elif a.codigo and a.nombre:
        trabajos.append((a.codigo, a.nombre, [p.strip() for p in a.proveedores.split(";") if p.strip()]))
    else:
        ap.error("indica --codigo y --nombre, o --todos")
    for codigo, nombre, provs in trabajos:
        f = generar(codigo, nombre, provs, str(out / f"Hoja_recepcion_{codigo}.pdf"))
        print("Generada", f)


if __name__ == "__main__":
    main()
