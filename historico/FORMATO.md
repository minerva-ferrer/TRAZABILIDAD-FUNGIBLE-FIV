# Archivos de histórico

Los históricos reales **no se guardan en este repositorio**, porque es público y contienen nº de lote y nº FIV del laboratorio.

Se cargan en la aplicación desde **Usuarios → Cargar histórico de un producto** (solo administradores), o dejando el archivo en la carpeta `datos/historico/` del PC donde corre el programa antes de arrancarlo.

Formato (un archivo .json por producto):

```json
{
  "producto": {
    "codigo": "000-00",
    "nombre": "Descripción del producto",
    "proveedores": [{"codigo": "", "nombre": "Proveedor A"}]
  },
  "recepciones": [
    {"id": "h000-01", "fecha": "2024-01-10", "proveedor": "Proveedor A", "cajas": "2",
     "lote": "AAA111", "caducidad": "2027-01-31", "fiv": "100", "firma": "V",
     "notas": "", "dudosa": false}
  ],
  "cambios_revisados": [],
  "nota_revision": "Carga inicial del histórico"
}
```

- `fecha`: AAAA-MM-DD. `caducidad`: AAAA-MM-DD o AAAA-MM (solo mes).
- `id`: identificador único de cada recepción en todo el histórico.
- `cambios_revisados`: ids de las recepciones cuyo cambio de lote ya se dio por revisado.
- `dudosa`: true si la lectura del papel no es segura; aparece como aviso en la ficha.
