"""
Pruebas de la lógica de trazabilidad y de la API.

Ejecutar desde la carpeta del proyecto:
    python -m unittest discover -s pruebas -v

Sirven como evidencia para la validación del programa: comprueban que se
detectan los cambios de lote, proveedor y caducidad, las anomalías del
registro, la anulación y la carga de históricos.
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
_tmp = tempfile.mkdtemp(prefix="traz-pruebas-")
os.environ["TRAZ_DATOS"] = _tmp
os.environ.pop("ANTHROPIC_API_KEY", None)
import app  # noqa: E402

# Histórico de prueba (datos inventados): 4 recepciones, 2 cambios de lote, 1 ya revisado
_hist = Path(_tmp) / "historico"
_hist.mkdir(parents=True, exist_ok=True)
(_hist / "PRUEBA-1.json").write_text(json.dumps({
    "producto": {"codigo": "PRUEBA-1", "nombre": "Producto de prueba", "proveedores": [{"codigo": "", "nombre": "Proveedor A"}]},
    "recepciones": [
        {"id": "t1", "fecha": "2024-01-10", "proveedor": "Proveedor A", "cajas": "2", "lote": "AAA111", "caducidad": "2027-01-31", "fiv": "100"},
        {"id": "t2", "fecha": "2024-03-10", "proveedor": "Proveedor A", "cajas": "1", "lote": "AAA111", "caducidad": "2027-01-31", "fiv": "120"},
        {"id": "t3", "fecha": "2024-06-10", "proveedor": "Proveedor A", "cajas": "2", "lote": "BBB222", "caducidad": "2028-06-30", "fiv": "150"},
        {"id": "t4", "fecha": "2024-09-10", "proveedor": "Proveedor B", "cajas": "2", "lote": "CCC333", "caducidad": "2029-06-30", "fiv": "180"},
    ],
    "cambios_revisados": ["t3"],
}), encoding="utf-8")


def rec(i, fecha, lote, proveedor="", caducidad="", fiv="", **kw):
    d = dict(id=i, fecha=fecha, lote=lote, proveedor=proveedor, caducidad=caducidad, fiv=fiv, notas="", dudosa=False, anulada=False)
    d.update(kw)
    return d


class Logica(unittest.TestCase):
    def test_cambio_de_lote(self):
        cs = app.detectar_cambios([rec("a", "2024-01-01", "111"), rec("b", "2024-02-01", "222")])
        self.assertEqual([(c["id"], c["tipos"]) for c in cs], [("b", ["lote"])])

    def test_mismo_lote_sin_cambio(self):
        self.assertEqual(app.detectar_cambios([rec("a", "2024-01-01", "111"), rec("b", "2024-02-01", " 111 ")]), [])

    def test_cambio_de_proveedor_y_lote(self):
        cs = app.detectar_cambios([rec("a", "2023-03-22", "220618534", "Concile"), rec("b", "2023-06-23", "263089", "Neptune")])
        self.assertEqual(cs[0]["tipos"], ["lote", "proveedor"])

    def test_proveedor_vacio_no_cuenta(self):
        cs = app.detectar_cambios([rec("a", "2024-01-01", "1", ""), rec("b", "2024-02-01", "1", "Concile")])
        self.assertEqual(cs, [])

    def test_cambio_de_caducidad_mismo_lote(self):
        cs = app.detectar_cambios([rec("a", "2024-01-01", "1", caducidad="2027-01-31"), rec("b", "2024-02-01", "1", caducidad="2027-06-30")])
        self.assertEqual(cs[0]["tipos"], ["caducidad"])

    def test_orden_por_fecha_y_fiv(self):
        cs = app.detectar_cambios([rec("b", "2024-02-01", "222", fiv="200"), rec("a", "2024-01-01", "111", fiv="100")])
        self.assertEqual(cs[0]["id"], "b")

    def test_anuladas_no_cuentan(self):
        rs = [rec("a", "2024-01-01", "111"), rec("x", "2024-01-15", "999", anulada=True), rec("b", "2024-02-01", "111")]
        rs[1]["anulada"] = True
        self.assertEqual(app.detectar_cambios(rs), [])

    def test_anomalias(self):
        rs = [rec("a", "2025-09-11", "241218446", fiv="225568"), rec("b", "2025-09-11", "241218446", fiv=""),
              rec("c", "2025-10-01", "241218446", fiv="225000")]
        msgs = [a["mensaje"] for a in app.detectar_anomalias(rs)]
        self.assertTrue(any("Sin nº FIV" in m for m in msgs))
        self.assertTrue(any("duplicado" in m for m in msgs))
        self.assertTrue(any("menor" in m for m in msgs))

    def test_fin_caducidad_mes(self):
        self.assertEqual(str(app.fin_caducidad("2015-02")), "2015-02-28")
        self.assertEqual(str(app.fin_caducidad("2015-12")), "2015-12-31")


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.iniciar_bd()
        cls.srv = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Manejador)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}/api/"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.cookie = ""  # la sesión se gestiona a mano: igual en todas las versiones de Python

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def llamar(self, ruta, cuerpo=None, metodo=None):
        cab = {"Content-Type": "application/json"}
        if Api.cookie:
            cab["Cookie"] = Api.cookie
        req = urllib.request.Request(self.url + ruta, data=json.dumps(cuerpo).encode() if cuerpo is not None else None,
                                     headers=cab, method=metodo or ("POST" if cuerpo is not None else "GET"))
        try:
            with urllib.request.urlopen(req) as r:
                galleta = r.headers.get("Set-Cookie")
                if galleta:
                    Api.cookie = galleta.split(";", 1)[0]
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_flujo_completo(self):
        st, d = self.llamar("estado")
        self.assertEqual(st, 401)
        st, d = self.llamar("inicio")
        self.assertFalse(d["hay_usuarios"])
        st, d = self.llamar("primer-usuario", {"usuario": "admin", "nombre": "Administración", "clave": "corta"})
        self.assertEqual(st, 400)
        st, d = self.llamar("primer-usuario", {"usuario": "admin", "nombre": "Administración", "clave": "clave-segura-1"})
        self.assertEqual(st, 200)

        st, d = self.llamar("estado")
        p = next(x for x in d["productos"] if x["codigo"] == "PRUEBA-1")
        self.assertEqual(len(p["recepciones"]), 4, "se cargan las 4 recepciones del histórico")
        self.assertEqual([c["tipos"] for c in p["cambios"]], [["lote"], ["lote", "proveedor"]])
        pendientes = [c for c in p["cambios"] if not c["revision"]]
        self.assertEqual(len(pendientes), 1, "el cambio ya revisado en el histórico no queda pendiente")
        self.assertEqual(p["ultima"]["lote"], "CCC333")

        st, d = self.llamar("recepciones", {"producto": "PRUEBA-1", "filas": [
            {"fecha": "2026-10-20", "proveedor": "Proveedor B", "cajas": "2", "lote": "250999001", "caducidad": "2029-12-31", "fiv": "226900", "origen": "manual"}]})
        self.assertEqual(st, 200)
        nuevo = d["ids"][0]
        st, d = self.llamar("estado")
        p = next(x for x in d["productos"] if x["codigo"] == "PRUEBA-1")
        c = next(x for x in p["cambios"] if x["id"] == nuevo)
        self.assertEqual(c["tipos"], ["lote"])
        self.assertEqual(c["antes"]["lote"], "CCC333")

        st, _ = self.llamar(f"revisiones/{nuevo}", {"nota": "Comunicado a Calidad"})
        self.assertEqual(st, 200)
        st, _ = self.llamar(f"recepciones/{nuevo}/anular", {"motivo": ""})
        self.assertEqual(st, 400)
        st, _ = self.llamar(f"recepciones/{nuevo}/anular", {"motivo": "Prueba"})
        self.assertEqual(st, 200)
        st, d = self.llamar("estado")
        p = next(x for x in d["productos"] if x["codigo"] == "PRUEBA-1")
        self.assertFalse(any(x["id"] == nuevo for x in p["cambios"]))
        self.assertEqual(p["ultima"]["lote"], "CCC333")

        st, _ = self.llamar("historico", {"datos": {"producto": {"codigo": "PRUEBA-1", "nombre": "x"}, "recepciones": []}})
        self.assertEqual(st, 409, "no se vuelve a cargar un producto existente")
        st, d = self.llamar("historico", {"datos": {"producto": {"codigo": "PRUEBA-2", "nombre": "Otro"},
                                                    "recepciones": [{"id": "u1", "fecha": "2025-01-01", "lote": "L1"}]}})
        self.assertEqual((st, d["recepciones"]), (200, 1))

        st, _ = self.llamar("recepciones", {"producto": "PRUEBA-1", "filas": [{"fecha": "20/10/2026", "lote": "1"}]})
        self.assertEqual(st, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
