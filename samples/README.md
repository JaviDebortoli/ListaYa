# Datos de ejemplo

Datos **ficticios** de una ferretería de demostración y dos proveedores inventados, en pesos argentinos. No corresponden a ningún comercio ni proveedor real; sirven para probar ListaYa de punta a punta hasta tener listas reales anonimizadas.

| Archivo | Qué es | Qué ejercita |
|---|---|---|
| `ferreteria/catalogo.csv` | Catálogo del comerciante: 25 productos con costo y precio vigente, sin IVA | Catálogo en CSV |
| `ferreteria/lista_bulonera_sur.csv` | Lista de un proveedor en planilla, separada por `;`, precios sin IVA | Ruta de código (sin modelo): encabezado debajo de un título, filas de sección, subas, alertas de margen, una línea ambigua ("TORNILLO AUTOPERFORANTE" sin medida) y un producto nuevo (tarugo de 12 mm) |
| `ferreteria/lista_distribuidora_norte.txt` | Lista pegada como texto, precios con IVA del 21% incluido | Ruta del modelo de texto: formato desprolijo, IVA a descontar, separador de miles (`$ 189.000`), un precio "consultar" y un producto nuevo (amoladora de banco) |

Para probar la extracción con estas listas:

```bash
python -m listaya.extraction_check samples/ferreteria/lista_bulonera_sur.csv
python -m listaya.extraction_check samples/ferreteria/lista_distribuidora_norte.txt
```

Aprobar una lista en el bot reescribe el archivo del catálogo: usá una copia si querés conservar el original.
